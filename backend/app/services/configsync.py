"""配置同步领域服务：双端版本握手、分段传输、版本冲突、幂等提交与容量侧回写。

设计要点（对应业务约束）：

1. 双端版本握手未完成（握手状态 != 已握手）时，任何动作都不能把任务推进到“已完成”，
   目标节点离线时也不能提前确认完成——完成只由“提交同步结果”在校验通过后产生。
2. 同一同步任务重复发起、并发执行、重复提交只产生一个完成结果：
   - 写操作统一在 store.lock 内串行；
   - 完成结果带幂等键（任务编号:源版本:complete），重复提交命中后只累加提交次数；
   - 同一目标节点同一时刻只允许一个活动任务（已完成/已失败除外）。
3. 源配置在分段传输中被再次修改、目标仍应用旧段时，判定为版本冲突：
   旧批次保持应用旧源版本，新源版本另起批次，两个源版本分别落到不同批次，互不合并。
4. 中断后从“已应用段”的下一段续传，不重复应用已固化段。
5. 每次状态流转都写一条同步审计（动作、发起人、前后版本、失败原因、未应用项），
   形成可验证的处理痕迹；同步结果与审计数据集可逐项核对。
6. 容量侧（worker 可调度节点）与配置节点清单读取同步结果，把实际生效版本回写并随记录保留。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.store import store

MODULE = "configsync"
WORKER_MODULE = "worker"
SNAPSHOT_MODULE = "config_snapshot"
PROGRESS_MODULE = "target_progress"
RESULT_MODULE = "sync_result"
AUDIT_MODULE = "sync_audit"

# 任务状态机。
STATUS_ORDER = ["待握手", "握手待确认", "传输中", "传输中断", "版本冲突", "已完成", "已失败"]
ACTIVE_STATUSES = {"待握手", "握手待确认", "传输中", "传输中断", "版本冲突"}
TERMINAL_STATUSES = {"已完成", "已失败"}

# 动作到“允许的前置状态”的映射，状态流转只在这里集中判定。
ACTION_FLOW: dict[str, set[str]] = {
    "发起握手": {"待握手"},
    "握手确认": {"握手待确认"},
    "应用分段": {"传输中", "传输中断"},
    "标记中断": {"传输中", "传输中断"},
    "节点恢复": {"传输中断"},
    "断点续传": {"传输中断"},
    "检测版本冲突": {"传输中", "传输中断"},
    "发布源新版本": {"待握手", "握手待确认", "传输中", "传输中断", "版本冲突"},
    # 已完成态仍允许提交：命中幂等键时直接返回既有结果，保证“连续提交只生效一次”。
    "提交同步结果": {"传输中", "传输中断", "版本冲突", "已完成"},
    "标记失败": {"待握手", "握手待确认", "传输中", "传输中断", "版本冲突"},
}

ADMIN_ROLES = {"管理员", "系统"}


class SyncRuleError(Exception):
    """业务规则被违反时抛出，路由层转成 ok=False 的可读结果（不是 500）。"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _find_worker(node_code: str) -> dict[str, Any] | None:
    for row in store.rows(WORKER_MODULE):
        if row.get("节点编号") == node_code:
            return row
    return None


def _current_snapshot(node_code: str) -> dict[str, Any] | None:
    """读取源节点当前生效的配置快照（快照状态=当前版本）。"""
    for row in store.rows(SNAPSHOT_MODULE):
        if row.get("节点编号") == node_code and row.get("状态") == "当前版本":
            return row
    return None


def _progress_for_task(task_code: str) -> dict[str, Any] | None:
    for row in store.rows(PROGRESS_MODULE):
        if row.get("任务编号") == task_code:
            return row
    return None


def _result_for_task(task_code: str) -> dict[str, Any] | None:
    for row in store.rows(RESULT_MODULE):
        if row.get("任务编号") == task_code and row.get("结果状态") == "完成":
            return row
    return None


class ConfigsyncService:
    # ------------------------------------------------------------------ 列表/明细
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        target: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("任务编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        if target:
            rows = [row for row in rows if row.get("目标节点") == target]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def list_results(self, task_code: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(RESULT_MODULE)
        if task_code:
            rows = [row for row in rows if row.get("任务编号") == task_code]
        return rows

    def list_audits(self, task_code: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(AUDIT_MODULE)
        if task_code:
            rows = [row for row in rows if row.get("任务编号") == task_code]
        return rows

    def list_snapshots(self, node_code: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(SNAPSHOT_MODULE)
        if node_code:
            rows = [row for row in rows if row.get("节点编号") == node_code]
        return rows

    def list_progress(self, task_code: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(PROGRESS_MODULE)
        if task_code:
            rows = [row for row in rows if row.get("任务编号") == task_code]
        return rows

    # ------------------------------------------------------------------ 登记
    def create_entry(self, values: dict[str, Any], actor: dict[str, str]) -> tuple[dict[str, Any] | None, list[str]]:
        required = ["任务编号", "源节点", "目标节点"]
        missing = [field for field in required if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        task_code = str(values["任务编号"]).strip()
        source = str(values["源节点"]).strip()
        target = str(values["目标节点"]).strip()
        with store.lock:
            if any(row.get("任务编号") == task_code for row in store.rows(MODULE)):
                raise SyncRuleError(f"任务编号 {task_code} 已存在，重复登记不会产生第二个同步结果")
            source_node = _find_worker(source)
            target_node = _find_worker(target)
            if source_node is None:
                raise SyncRuleError(f"源节点 {source} 不在可调度节点清单中")
            if target_node is None:
                raise SyncRuleError(f"目标节点 {target} 不在可调度节点清单中")
            snapshot = _current_snapshot(source)
            if snapshot is None:
                raise SyncRuleError(f"源节点 {source} 没有可用的配置快照，无法发起同步")
            # 同一目标节点同时只允许一个活动任务，避免并发同步产生两个完成版本。
            conflict = [
                row for row in store.rows(MODULE)
                if row.get("目标节点") == target and row.get("status") in ACTIVE_STATUSES
            ]
            if conflict:
                raise SyncRuleError(
                    f"目标节点 {target} 已有活动任务 {conflict[0].get('任务编号')}，"
                    "同一时刻只能存在一个同步任务"
                )
            entry = {"id": store.next_id(MODULE)}
            entry.update({
                "任务编号": task_code,
                "源节点": source,
                "目标节点": target,
                "同步类型": str(values.get("同步类型") or "全量同步").strip(),
                "发起人": actor["name"],
                "归属域": target_node.get("归属域", ""),
                "握手状态": "未握手",
                "批次号": "",
                "源快照版本": snapshot["配置版本号"],
                "生效前版本": target_node.get("当前配置版本号", ""),
                "生效后版本": target_node.get("当前配置版本号", ""),
                "配置版本号": target_node.get("当前配置版本号", ""),
                "最后同步时间": "",
                "差异摘要": "尚未握手，差异待双端版本协商后生成",
                "完成结果编号": "",
                "同步状态": "待握手",
                "status": "待握手",
                "pending": True,
                "abnormal": False,
            })
            store.rows(MODULE).append(entry)
            self._audit(entry, "登记同步任务", actor, reason="", unapplied=[])
            return entry, []

    # ------------------------------------------------------------------ 统一动作入口
    def run_action(self, entry_id: int, action: str, values: dict[str, Any], actor: dict[str, str]) -> tuple[dict[str, Any] | None, str]:
        with store.lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                raise SyncRuleError(f"同步任务 {entry_id} 不存在或已归档")
            if action not in ACTION_FLOW:
                raise SyncRuleError(f"动作「{action}」不属于配置同步可执行范围")
            if entry["status"] not in ACTION_FLOW[action]:
                raise SyncRuleError(
                    f"任务当前为「{entry['status']}」，不允许执行「{action}」；"
                    f"该动作仅在 {'、'.join(sorted(ACTION_FLOW[action]))} 状态下可用"
                )
            handler = getattr(self, f"_act_{self._snake(action)}", None)
            if handler is None:
                raise SyncRuleError(f"动作「{action}」缺少领域处理逻辑")
            message = handler(entry, values, actor)
            entry["同步状态"] = entry["status"]
            return entry, message

    # ------------------------------------------------------------------ 动作：发起握手
    def _act_发起握手(self, entry: dict[str, Any], values: dict[str, Any], actor: dict[str, str]) -> str:
        target_node = _find_worker(entry["目标节点"])
        assert target_node is not None
        # 目标离线：允许登记/发起，但握手不能成立，更不可能确认完成。
        if target_node.get("节点状态标签") != "在线":
            self._audit(entry, "发起握手", actor,
                        reason=f"目标节点 {entry['目标节点']} 离线，握手无法成立", unapplied=[])
            raise SyncRuleError(f"目标节点 {entry['目标节点']} 当前离线，双端版本握手无法建立，不能进入同步")
        snapshot = _current_snapshot(entry["源节点"])
        if snapshot is None:
            raise SyncRuleError("源节点当前配置快照缺失，无法读取源版本")
        entry["源快照版本"] = snapshot["配置版本号"]
        entry["握手状态"] = "握手待确认"
        entry["status"] = "握手待确认"
        entry["pending"] = True
        self._audit(entry, "发起握手", actor, reason="", unapplied=[])
        return "已读取源节点配置快照并发起双端版本握手，等待目标节点确认"

    # ------------------------------------------------------------------ 动作：握手确认
    def _act_握手确认(self, entry: dict[str, Any], values: dict[str, Any], actor: dict[str, str]) -> str:
        target_node = _find_worker(entry["目标节点"])
        assert target_node is not None
        if target_node.get("节点状态标签") != "在线":
            raise SyncRuleError(f"目标节点 {entry['目标节点']} 已离线，握手确认被拒绝")
        snapshot = _current_snapshot(entry["源节点"])
        if snapshot is None or snapshot["配置版本号"] != entry["源快照版本"]:
            # 握手窗口内源版本发生变化，本次握手基于过期快照，需重新发起。
            raise SyncRuleError("握手期间源配置版本已变化，本次握手基于过期快照，请重新发起握手")
        batch_no = self._next_batch_no()
        progress = _progress_for_task(entry["任务编号"])
        if progress is None:
            progress = {"id": store.next_id(PROGRESS_MODULE)}
            store.rows(PROGRESS_MODULE).append(progress)
        progress.update({
            "任务编号": entry["任务编号"],
            "目标节点": entry["目标节点"],
            "节点在线": True,
            "握手状态": "已握手",
            "生效前版本": target_node.get("当前配置版本号", ""),
            "期望版本": snapshot["配置版本号"],
            "段总数": snapshot["段总数"],
            "已应用段": [],
            "中断标记": False,
            "最后应用时间": _now(),
        })
        entry.update({
            "握手状态": "已握手",
            "批次号": batch_no,
            "生效前版本": progress["生效前版本"],
            "生效后版本": progress["生效前版本"],
            "配置版本号": progress["生效前版本"],
            "status": "传输中",
            "pending": True,
            "差异摘要": self._diff_text(snapshot, progress),
        })
        self._audit(entry, "握手确认", actor, reason="", unapplied=[])
        return f"双端版本握手已成立（批次 {batch_no}），开始按 {snapshot['段总数']} 个配置段分段传输"

    # ------------------------------------------------------------------ 动作：应用分段
    def _act_应用分段(self, entry: dict[str, Any], values: dict[str, Any], actor: dict[str, str]) -> str:
        progress = self._require_progress(entry)
        target_node = _find_worker(entry["目标节点"])
        assert target_node is not None
        if target_node.get("节点状态标签") != "在线":
            raise SyncRuleError(f"目标节点 {entry['目标节点']} 离线，无法应用分段（请先标记中断）")
        snapshot = _current_snapshot(entry["源节点"])
        if snapshot is None:
            raise SyncRuleError("源节点当前配置快照缺失")
        # 版本冲突检测：传输中源被改成新版本，而目标仍按旧批次应用旧段。
        if snapshot["配置版本号"] != entry["源快照版本"]:
            self._raise_conflict(entry, actor, snapshot, progress)
        applied = set(progress["已应用段"])
        # 默认应用下一段；也允许显式指定段号，但已应用段不会被重复应用（幂等）。
        requested = values.get("段号")
        if requested is None:
            seg_no = min(seg["段号"] for seg in snapshot["段清单"] if seg["段号"] not in applied)
        else:
            seg_no = int(requested)
        if seg_no in applied:
            return f"第 {seg_no} 段已固化应用，重复应用被忽略（断点续传不重复生效）"
        seg = next((item for item in snapshot["段清单"] if item["段号"] == seg_no), None)
        if seg is None:
            raise SyncRuleError(f"第 {seg_no} 段不在源快照 {entry['源快照版本']} 的段清单中")
        applied.add(seg_no)
        progress["已应用段"] = sorted(applied)
        progress["最后应用时间"] = _now()
        progress["中断标记"] = False
        entry["差异摘要"] = self._diff_text(snapshot, progress)
        self._audit(entry, "应用分段", actor, reason="",
                    unapplied=self._unapplied(snapshot, progress))
        if len(applied) >= snapshot["段总数"]:
            return f"第 {seg_no} 段（{seg['配置项']}）已应用，{snapshot['段总数']}/{snapshot['段总数']} 段全部应用，可提交同步结果"
        return f"第 {seg_no} 段（{seg['配置项']}）已应用，当前 {len(applied)}/{snapshot['段总数']} 段"

    # ------------------------------------------------------------------ 动作：标记中断
    def _act_标记中断(self, entry: dict[str, Any], values: dict[str, Any], actor: dict[str, str]) -> str:
        progress = self._require_progress(entry)
        target_node = _find_worker(entry["目标节点"])
        assert target_node is not None
        if target_node.get("节点状态标签") == "在线":
            raise SyncRuleError("目标节点仍在线，不能标记中断；中断仅用于离线等异常场景")
        progress["节点在线"] = False
        progress["中断标记"] = True
        entry["status"] = "传输中断"
        entry["pending"] = True
        entry["abnormal"] = True
        snapshot = _current_snapshot(entry["源节点"])
        unapplied = self._unapplied(snapshot, progress) if snapshot else []
        reason = f"目标节点 {entry['目标节点']} 离线，第 {self._next_seg(progress)} 段应用中断"
        entry["差异摘要"] = f"未应用项：{'、'.join(unapplied) or '无'}（{len(progress['已应用段'])}/{progress['段总数']} 段，目标离线中断）"
        self._audit(entry, "分段中断", actor, reason=reason, unapplied=unapplied)
        return f"已标记中断，已应用 {len(progress['已应用段'])}/{progress['段总数']} 段，恢复后从第 {self._next_seg(progress)} 段续传"

    # ------------------------------------------------------------------ 动作：节点恢复
    def _act_节点恢复(self, entry: dict[str, Any], values: dict[str, Any], actor: dict[str, str]) -> str:
        target_node = _find_worker(entry["目标节点"])
        assert target_node is not None
        target_node["节点状态标签"] = "在线"
        target_node["status"] = "在线"
        target_node["abnormal"] = False
        target_node["上次心跳时间"] = _now()
        progress = self._require_progress(entry)
        progress["节点在线"] = True
        self._audit(entry, "节点恢复", actor, reason="", unapplied=[])
        return f"目标节点 {entry['目标节点']} 已恢复在线，可执行断点续传"

    # ------------------------------------------------------------------ 动作：断点续传
    def _act_断点续传(self, entry: dict[str, Any], values: dict[str, Any], actor: dict[str, str]) -> str:
        progress = self._require_progress(entry)
        target_node = _find_worker(entry["目标节点"])
        assert target_node is not None
        if target_node.get("节点状态标签") != "在线":
            raise SyncRuleError(f"目标节点 {entry['目标节点']} 仍离线，无法断点续传")
        snapshot = _current_snapshot(entry["源节点"])
        if snapshot is None:
            raise SyncRuleError("源节点当前配置快照缺失")
        if snapshot["配置版本号"] != entry["源快照版本"]:
            self._raise_conflict(entry, actor, snapshot, progress)
        applied = set(progress["已应用段"])
        remaining = [seg for seg in snapshot["段清单"] if seg["段号"] not in applied]
        if not remaining:
            raise SyncRuleError("没有待续传的配置段")
        resume_seg = remaining[0]["段号"]  # 续传起始段 = 第一个未应用段，即中断点的下一段。
        for seg in remaining:  # 从已应用段的下一段开始，逐段补齐，不重复已固化段。
            applied.add(seg["段号"])
        progress["已应用段"] = sorted(applied)
        progress["中断标记"] = False
        progress["最后应用时间"] = _now()
        entry["status"] = "传输中"
        entry["abnormal"] = False
        entry["差异摘要"] = self._diff_text(snapshot, progress)
        self._audit(entry, "断点续传", actor, reason="", unapplied=[])
        return f"已从第 {resume_seg} 段（中断点的下一段）续传并补齐，现 {len(applied)}/{progress['段总数']} 段全部应用"

    # ------------------------------------------------------------------ 动作：发布源新版本
    def _act_发布源新版本(self, entry: dict[str, Any], values: dict[str, Any], actor: dict[str, str]) -> str:
        """源节点在同步窗口内再次修改配置：固化旧快照，生成新的当前版本快照。

        正在进行的任务仍绑定旧源版本（旧批次），下一次分段应用即会判定版本冲突，
        新源版本需要另起批次同步——两个源版本因此落到不同批次，不会被合并。
        """
        source = entry["源节点"]
        current = _current_snapshot(source)
        if current is None:
            raise SyncRuleError(f"源节点 {source} 缺少当前配置快照")
        current["状态"] = "历史版本"
        next_seq = int(current.get("版本序号", 0)) + 1
        new_version = str(values.get("新版本号") or f"cfg-v{next_seq}").strip()
        snapshot = {"id": store.next_id(SNAPSHOT_MODULE)}
        snapshot.update({
            "节点编号": source,
            "配置版本号": new_version,
            "版本序号": next_seq,
            "状态": "当前版本",
            "发起人": actor["name"],
            "生成时间": _now(),
            "段总数": current["段总数"],
            "段清单": [
                {
                    "段号": seg["段号"],
                    "配置项": seg["配置项"],
                    "校验值": f"sha256:{new_version}-seg{seg['段号']}",
                    "状态": "已固化",
                }
                for seg in current["段清单"]
            ],
        })
        store.rows(SNAPSHOT_MODULE).append(snapshot)
        self._audit(entry, "发布源新版本", actor,
                    reason=f"源配置由 {current['配置版本号']} 更新为 {new_version}，在途任务仍绑定旧版本",
                    unapplied=[])
        return f"源节点 {source} 已发布新配置版本 {new_version}，在途任务下一次分段将触发版本冲突并另起批次"

    # ------------------------------------------------------------------ 动作：检测版本冲突
    def _act_检测版本冲突(self, entry: dict[str, Any], values: dict[str, Any], actor: dict[str, str]) -> str:
        progress = self._require_progress(entry)
        snapshot = _current_snapshot(entry["源节点"])
        if snapshot is None:
            raise SyncRuleError("源节点当前配置快照缺失")
        if snapshot["配置版本号"] == entry["源快照版本"]:
            return f"源版本仍为 {entry['源快照版本']}，未检测到版本冲突"
        self._raise_conflict(entry, actor, snapshot, progress)
        raise SyncRuleError("unreachable")  # pragma: no cover

    # ------------------------------------------------------------------ 动作：提交同步结果
    def _act_提交同步结果(self, entry: dict[str, Any], values: dict[str, Any], actor: dict[str, str]) -> str:
        # 幂等优先：任务已完成（含并发/重复提交）时，命中既有完成结果直接返回，不再走校验。
        idem_key = f"{entry['任务编号']}:{entry['源快照版本']}:complete"
        existing = next(
            (row for row in store.rows(RESULT_MODULE) if row.get("幂等键") == idem_key),
            None,
        )
        if entry.get("status") == "已完成" and existing is not None:
            existing["提交次数"] = int(existing.get("提交次数", 1)) + 1
            entry["完成结果编号"] = existing["结果编号"]
            self._audit(entry, "重复提交同步结果", actor, reason="命中幂等键，完成结果不重复生成",
                        unapplied=[], result_no=existing["结果编号"])
            return f"同步结果 {existing['结果编号']} 已存在，重复提交仅记录一次（累计提交 {existing['提交次数']} 次）"

        progress = self._require_progress(entry)
        target_node = _find_worker(entry["目标节点"])
        assert target_node is not None
        # 目标离线：绝不允许提前确认完成。
        if target_node.get("节点状态标签") != "在线":
            self._audit(entry, "提交同步结果", actor,
                        reason=f"目标节点 {entry['目标节点']} 离线，禁止提前确认完成",
                        unapplied=self._unapplied(_current_snapshot(entry['源节点']), progress))
            raise SyncRuleError(f"目标节点 {entry['目标节点']} 离线，配置未完整应用，不能确认同步完成")
        # 必须先完成双端版本握手。
        if entry.get("握手状态") != "已握手":
            raise SyncRuleError("双端版本握手尚未形成，不能确认同步完成")
        snapshot = _current_snapshot(entry["源节点"])
        if snapshot is None:
            raise SyncRuleError("源节点当前配置快照缺失")
        # 版本冲突未消解：新源版本另起批次，旧批次不能以新版本名义完成。
        if snapshot["配置版本号"] != entry["源快照版本"]:
            self._raise_conflict(entry, actor, snapshot, progress)
        unapplied = self._unapplied(snapshot, progress)
        if unapplied:
            reason = f"仍有 {len(unapplied)} 个配置段未应用：{'、'.join(unapplied)}，配置不完整不能确认完成"
            self._audit(entry, "提交同步结果", actor, reason=reason, unapplied=unapplied)
            raise SyncRuleError(reason)

        # 幂等：同一任务同一源版本的完成结果只生效一次，重复提交只累加提交次数。
        idem_key = f"{entry['任务编号']}:{entry['源快照版本']}:complete"
        existing = next(
            (row for row in store.rows(RESULT_MODULE) if row.get("幂等键") == idem_key),
            None,
        )
        before_version = progress["生效前版本"]
        after_version = entry["源快照版本"]
        if existing is not None:
            existing["提交次数"] = int(existing.get("提交次数", 1)) + 1
            entry["完成结果编号"] = existing["结果编号"]
            self._audit(entry, "重复提交同步结果", actor, reason="命中幂等键，完成结果不重复生成",
                        unapplied=[], result_no=existing["结果编号"])
            return f"同步结果 {existing['结果编号']} 已存在，重复提交仅记录一次（累计提交 {existing['提交次数']} 次）"

        result = {"id": store.next_id(RESULT_MODULE)}
        result_no = f"RST-{result['id']:04d}"
        result.update({
            "结果编号": result_no,
            "任务编号": entry["任务编号"],
            "批次号": entry["批次号"],
            "源节点": entry["源节点"],
            "目标节点": entry["目标节点"],
            "发起人": actor["name"],
            "结果状态": "完成",
            "完整应用": True,
            "生效前版本": before_version,
            "生效后版本": after_version,
            "源快照版本": entry["源快照版本"],
            "差异摘要": {"未应用项": [], "说明": f"{progress['段总数']}/{progress['段总数']} 段全部应用，双端版本一致"},
            "失败原因": "",
            "幂等键": idem_key,
            "提交次数": 1,
            "容量侧已回写": False,
            "容量侧回写时间": "",
            "最后同步时间": _now(),
        })
        store.rows(RESULT_MODULE).append(result)

        # 容量侧/配置节点清单回写：实际生效版本随节点记录一起保留。
        self._writeback_to_worker(entry, result, target_node, after_version, actor)

        entry.update({
            "status": "已完成",
            "同步状态": "已完成",
            "pending": False,
            "abnormal": False,
            "生效后版本": after_version,
            "配置版本号": after_version,
            "完成结果编号": result_no,
            "最后同步时间": result["最后同步时间"],
            "差异摘要": result["差异摘要"]["说明"],
        })
        self._audit(entry, "提交同步结果", actor, reason="", unapplied=[], result_no=result_no)
        return f"同步结果 {result_no} 已生成并完成容量侧版本回写，目标生效版本 {before_version} → {after_version}"

    # ------------------------------------------------------------------ 动作：标记失败
    def _act_标记失败(self, entry: dict[str, Any], values: dict[str, Any], actor: dict[str, str]) -> str:
        reason = str(values.get("失败原因") or "未提供失败原因").strip()
        progress = _progress_for_task(entry["任务编号"])
        snapshot = _current_snapshot(entry["源节点"])
        unapplied = self._unapplied(snapshot, progress) if (snapshot and progress) else []
        entry["status"] = "已失败"
        entry["同步状态"] = "已失败"
        entry["pending"] = False
        entry["abnormal"] = True
        self._audit(entry, "标记失败", actor, reason=reason, unapplied=unapplied)
        return f"任务已标记失败：{reason}（失败原因与未应用项已进入审计结果）"

    # ================================================================== 内部辅助
    def _writeback_to_worker(
        self,
        entry: dict[str, Any],
        result: dict[str, Any],
        target_node: dict[str, Any],
        after_version: str,
        actor: dict[str, str],
    ) -> None:
        """容量侧：读取同步结果，把实际生效版本回写到可调度节点记录并随记录保留。"""
        target_node["当前配置版本号"] = after_version
        target_node["差异摘要"] = result["差异摘要"]["说明"]
        target_node["最后同步时间"] = result["最后同步时间"]
        target_node["关联同步结果"] = result["结果编号"]
        result["容量侧已回写"] = True
        result["容量侧回写时间"] = _now()
        self._audit(entry, "容量侧版本回写",
                    {"name": "系统", "role": "系统"}, reason="", unapplied=[],
                    result_no=result["结果编号"])

    def _raise_conflict(
        self,
        entry: dict[str, Any],
        actor: dict[str, str],
        new_snapshot: dict[str, Any],
        progress: dict[str, Any],
    ) -> None:
        """版本冲突：旧批次保留旧源版本，新源版本另起批次，两版本落不同批次。"""
        old_version = entry["源快照版本"]
        new_version = new_snapshot["配置版本号"]
        new_batch = self._next_batch_no()
        # 当前任务进入冲突态：本批次仍绑定旧版本，未应用段记入差异，不能直接完成。
        entry["status"] = "版本冲突"
        entry["abnormal"] = True
        entry["pending"] = True
        unapplied = self._unapplied(new_snapshot, progress)
        reason = (
            f"版本冲突：分段传输中源配置由 {old_version} 更新为 {new_version}，目标仍应用旧段；"
            f"旧批次 {entry['批次号']} 保留 {old_version}，新版本 {new_version} 另起批次 {new_batch}"
        )
        entry["差异摘要"] = (
            f"版本冲突：旧批次应用 {old_version}，新批次 {new_batch} 待应用 {new_version}；"
            f"旧批次未应用项：{'、'.join(self._unapplied_old(progress)) or '无'}"
        )
        self._audit(entry, "版本冲突", actor, reason=reason, unapplied=unapplied)
        raise SyncRuleError(reason + f"（新批次 {new_batch}）")

    def _require_progress(self, entry: dict[str, Any]) -> dict[str, Any]:
        progress = _progress_for_task(entry["任务编号"])
        if progress is None:
            raise SyncRuleError("目标节点应用进度不存在，请先完成双端版本握手")
        return progress

    def _unapplied(self, snapshot: dict[str, Any] | None, progress: dict[str, Any]) -> list[str]:
        if snapshot is None:
            return []
        applied = set(progress.get("已应用段", []))
        return [seg["配置项"] for seg in snapshot["段清单"] if seg["段号"] not in applied]

    def _unapplied_old(self, progress: dict[str, Any]) -> list[str]:
        applied = set(progress.get("已应用段", []))
        total = int(progress.get("段总数", 0))
        names = {1: "调度策略", 2: "过滤规则", 3: "DNS解析", 4: "带宽策略"}
        return [names.get(i, f"第{i}段") for i in range(1, total + 1) if i not in applied]

    def _diff_text(self, snapshot: dict[str, Any], progress: dict[str, Any]) -> str:
        unapplied = self._unapplied(snapshot, progress)
        done = len(progress.get("已应用段", []))
        total = snapshot["段总数"]
        if not unapplied:
            return f"{total}/{total} 段全部应用，双端版本一致"
        return f"未应用项：{'、'.join(unapplied)}（{done}/{total} 段）"

    def _next_seg(self, progress: dict[str, Any]) -> int:
        applied = set(progress.get("已应用段", []))
        for i in range(1, int(progress.get("段总数", 0)) + 1):
            if i not in applied:
                return i
        return int(progress.get("段总数", 0)) + 1

    def _next_batch_no(self) -> str:
        existing = [int(str(row.get("批次号", "0")).split("-")[-1]) for row in store.rows(MODULE) if row.get("批次号")]
        return f"BATCH-{max(existing, default=0) + 1:04d}"

    def _audit(
        self,
        entry: dict[str, Any],
        action: str,
        actor: dict[str, str],
        *,
        reason: str,
        unapplied: list[str],
        result_no: str = "",
    ) -> dict[str, Any]:
        row = {"id": store.next_id(AUDIT_MODULE)}
        row.update({
            "审计编号": f"AUD-{row['id']:04d}",
            "任务编号": entry["任务编号"],
            "批次号": entry.get("批次号", ""),
            "动作": action,
            "发起人": actor["name"],
            "角色": actor["role"],
            "动作时间": _now(),
            "源版本": entry.get("源快照版本", ""),
            "目标前版本": entry.get("生效前版本", ""),
            "目标后版本": entry.get("生效后版本", ""),
            "失败原因": reason,
            "未应用项": unapplied,
            "关联结果编号": result_no,
        })
        store.rows(AUDIT_MODULE).append(row)
        return row

    @staticmethod
    def _snake(action: str) -> str:
        # 中文动作名直接作为方法名后缀，这里原样返回，仅占位保持调用一致。
        return action


# 便捷的角色判定，路由层做权限收口。
def is_admin(role: str) -> bool:
    return role in ADMIN_ROLES
