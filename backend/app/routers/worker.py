"""采集节点接口：维护工作节点，并承接配置同步的容量侧回写与执行侧采集。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.configsync import is_admin
from app.services.worker import WorkerService

router = APIRouter(prefix="/api/worker", tags=["采集节点"])

service = WorkerService()

LIST_FIELDS = ["节点编号", "主机地址", "并发槽位数", "地域标签", "CPU使用率", "内存可用MB", "上次心跳时间", "节点状态标签", "当前配置版本号", "差异摘要", "最后同步时间"]
STATUSES = ["草稿", "注册中", "在线", "离线"]

_ROLE_MAP = {"admin": "管理员", "viewer": "只读观察员", "system": "系统"}


def _require_config_admin(x_user_role: str | None) -> None:
    # 会改“节点配置版本”的写动作按归属限定为管理员，非管理员只读。
    role = _ROLE_MAP.get((x_user_role or "admin").strip(), (x_user_role or "admin").strip())
    if not is_admin(role):
        raise HTTPException(status_code=403, detail="当前角色为非管理员，节点配置版本仅可只读查看，维护动作已被拒绝")


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按节点编号检索"),
    status: str | None = Query(default=None, description="草稿、注册中、在线、离线"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按节点编号与状态过滤采集节点列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/config-nodes")
def config_nodes() -> dict[str, Any]:
    """执行侧配置节点清单：节点配置版本、差异摘要、最后同步时间的只读采集投影。"""
    items = service.list_config_nodes()
    return {"module": "config_node_list", "total": len(items), "items": items}


@router.post("/collect-config-versions", response_model=ActionResult)
def collect_config_versions(
    x_user_role: str | None = Header(default=None, alias="X-User-Role"),
) -> ActionResult:
    """执行侧采集：把节点配置版本同步到配置节点清单，配置版本号/差异摘要/最后同步时间一并采集。"""
    _require_config_admin(x_user_role)
    items = service.collect_config_nodes()
    return ActionResult(ok=True, message=f"已采集 {len(items)} 个节点的配置版本到配置节点清单")


@router.post("/apply-sync-result", response_model=ActionResult)
def apply_sync_result(
    payload: EntryPayload,
    x_user_role: str | None = Header(default=None, alias="X-User-Role"),
) -> ActionResult:
    """容量侧：读取目标节点的完成同步结果，把实际生效版本回写到可调度节点记录。"""
    _require_config_admin(x_user_role)
    node_code = str(payload.values.get("节点编号") or "").strip()
    if not node_code:
        return ActionResult(ok=False, message="缺少必填字段：节点编号")
    entry, message = service.apply_sync_result(node_code)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出采集节点清单：返回当前过滤条件下的全量数据，含容量侧版本字段。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "worker", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条工作节点明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"工作节点 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条工作节点，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="工作节点已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条工作节点执行提交注册、标记在线、标记离线；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
