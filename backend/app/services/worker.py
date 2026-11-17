"""采集节点（容量侧/执行侧）业务规则。

除通用的节点状态维护外，这里承接配置同步域的两侧协作：
- 容量侧：读取目标节点的配置同步结果，把“实际生效版本”回写到可调度节点记录，
  配置同步结果随节点记录一起保留（关联同步结果编号、差异摘要、最后同步时间）。
- 执行侧：把节点配置版本同步到配置节点清单（采集执行侧视图），供调度前核对版本。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.store import store

MODULE = "worker"
RESULT_MODULE = "sync_result"
REQUIRED_FIELDS = ["节点编号", "主机地址", "并发槽位数"]
STATUS_ORDER = ["草稿", "注册中", "在线", "离线"]
ACTION_RULES = {"提交注册": "注册中", "标记在线": "在线", "标记离线": "离线"}
NEGATIVE_ACTIONS = []

# 配置节点清单是容量侧清单的“执行侧投影”，单独维护一张只读采集表。
CONFIG_NODE_LIST = "config_node_list"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class WorkerService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("节点编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": store.next_id(MODULE)}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        # 新节点默认带上容量侧版本字段，保证后续同步结果可回写。
        entry.setdefault("当前配置版本号", "")
        entry.setdefault("差异摘要", "")
        entry.setdefault("最后同步时间", "")
        entry.setdefault("关联同步结果", "")
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"工作节点 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于采集节点可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["节点状态标签"] = target
        entry["pending"] = target == STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        entry["上次心跳时间"] = _now()
        return entry, f"工作节点已{action}"

    # ---------------------------------------------------------- 容量侧：读取同步结果回写
    def apply_sync_result(self, node_code: str) -> tuple[dict[str, Any] | None, str]:
        """容量侧读取目标节点最近一条完成的同步结果，并把实际生效版本回写到节点记录。

        只认“完成且完整应用”的结果；重复调用幂等——版本与关联结果已一致时直接返回，
        不会制造第二条版本痕迹。
        """
        with store.lock:
            node = self._find_by_code(node_code)
            if node is None:
                return None, f"节点 {node_code} 不在可调度节点清单中"
            completed = [
                row for row in store.rows(RESULT_MODULE)
                if row.get("目标节点") == node_code
                and row.get("结果状态") == "完成"
                and row.get("完整应用")
            ]
            if not completed:
                return None, f"节点 {node_code} 没有可回写的完成同步结果（未完成或配置不完整）"
            latest = max(completed, key=lambda row: str(row.get("最后同步时间", "")))
            # 幂等：已是该结果对应版本则不重复回写。
            if node.get("关联同步结果") == latest["结果编号"] and node.get("当前配置版本号") == latest["生效后版本"]:
                return node, f"节点 {node_code} 已回写结果 {latest['结果编号']}（版本 {latest['生效后版本']}），无需重复处理"
            node["当前配置版本号"] = latest["生效后版本"]
            node["差异摘要"] = latest["差异摘要"]["说明"]
            node["最后同步时间"] = latest["最后同步时间"]
            node["关联同步结果"] = latest["结果编号"]
            latest["容量侧已回写"] = True
            latest["容量侧回写时间"] = _now()
            return node, (
                f"已读取同步结果 {latest['结果编号']}，节点 {node_code} 实际生效版本回写为 "
                f"{latest['生效前版本']} → {latest['生效后版本']}，结果随节点记录保留"
            )

    # ---------------------------------------------------------- 执行侧：同步到配置节点清单
    def collect_config_nodes(self) -> list[dict[str, Any]]:
        """执行侧采集：把各节点配置版本同步到配置节点清单（投影表）。

        配置版本号、差异摘要、最后同步时间三者共同解释节点配置版本；
        每次采集按节点编号覆盖同一行，保证清单与容量侧记录一致。
        """
        with store.lock:
            table = store.rows(CONFIG_NODE_LIST)
            by_code = {row["节点编号"]: row for row in table}
            for node in store.rows(MODULE):
                code = node.get("节点编号")
                projected = by_code.get(code)
                if projected is None:
                    projected = {"id": store.next_id(CONFIG_NODE_LIST)}
                    table.append(projected)
                    by_code[code] = projected
                projected.update({
                    "节点编号": code,
                    "节点角色": node.get("节点角色", ""),
                    "节点状态标签": node.get("节点状态标签", node.get("status", "")),
                    "归属域": node.get("归属域", ""),
                    "当前配置版本号": node.get("当前配置版本号", ""),
                    "差异摘要": node.get("差异摘要", ""),
                    "最后同步时间": node.get("最后同步时间", ""),
                    "关联同步结果": node.get("关联同步结果", ""),
                    "采集时间": _now(),
                })
            return list(table)

    def list_config_nodes(self) -> list[dict[str, Any]]:
        return store.rows(CONFIG_NODE_LIST)

    def _find_by_code(self, node_code: str) -> dict[str, Any] | None:
        for row in store.rows(MODULE):
            if row.get("节点编号") == node_code:
                return row
        return None
