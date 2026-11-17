"""配置同步接口：双端版本握手、分段传输、同步结果与同步审计数据集。

权限口径（按归属域限定维护权限）：
- 管理员可写：登记任务、执行握手/分段/续传/冲突/提交/失败等全部动作；
- 非管理员只读：可查看任务、同步差异、同步结果与审计，但任何写动作都会被拒绝。
角色通过 X-User-Role / X-User-Name 请求头传入，缺省按管理员处理，便于本地直接联调。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.configsync import ACTION_FLOW, ConfigsyncService, SyncRuleError, is_admin

router = APIRouter(prefix="/api/configsync", tags=["配置同步"])

service = ConfigsyncService()

LIST_FIELDS = ["任务编号", "源节点", "目标节点", "同步类型", "握手状态", "批次号", "配置版本号", "差异摘要", "最后同步时间", "同步状态"]
STATUSES = ["待握手", "握手待确认", "传输中", "传输中断", "版本冲突", "已完成", "已失败"]


def _actor(x_user_name: str | None, x_user_role: str | None, values: dict[str, Any] | None = None) -> dict[str, str]:
    # 角色走 ASCII 请求头（admin/viewer），服务端映射为中文角色；也兼容直接传中文。
    role_map = {"admin": "管理员", "viewer": "只读观察员", "system": "系统"}
    raw_role = (x_user_role or "admin").strip()
    # 操作人姓名含中文，优先取 UTF-8 的请求体，避免非 Latin-1 字符进 HTTP 头。
    body_name = str((values or {}).get("操作人") or "").strip()
    return {
        "name": body_name or (x_user_name or "值班管理员").strip(),
        "role": role_map.get(raw_role, raw_role),
    }


def _require_write(role: str) -> None:
    if not is_admin(role):
        raise HTTPException(status_code=403, detail="当前角色为非管理员，配置版本按归属限定维护权限，仅可只读查看同步差异")


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按任务编号检索"),
    status: str | None = Query(default=None, description="同步任务状态"),
    target: str | None = Query(default=None, description="按目标节点编号过滤"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按任务编号、状态与目标节点过滤配置同步列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, target=target, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/status-meta")
def status_meta() -> dict[str, Any]:
    """暴露状态机与动作清单，供前端按当前状态渲染可执行动作。"""
    return {
        "statuses": STATUSES,
        "actions": {action: sorted(states) for action, states in ACTION_FLOW.items()},
    }


@router.get("/snapshots")
def list_snapshots(node: str | None = Query(default=None, description="按源节点编号过滤")) -> dict[str, Any]:
    """源节点配置快照数据集：版本号、段清单与段级校验值，供双端逐项核对。"""
    items = service.list_snapshots(node)
    return {"module": "config_snapshot", "total": len(items), "items": items}


@router.get("/progress")
def list_progress(task: str | None = Query(default=None, description="按任务编号过滤")) -> dict[str, Any]:
    """目标节点应用进度数据集：已应用段、中断标记、期望版本与生效前版本。"""
    items = service.list_progress(task)
    return {"module": "target_progress", "total": len(items), "items": items}


@router.get("/results")
def list_results(task: str | None = Query(default=None, description="按任务编号过滤")) -> dict[str, Any]:
    """配置同步结果数据集：前后版本、差异摘要、失败原因、幂等提交次数与容量侧回写痕迹。"""
    items = service.list_results(task)
    return {"module": "sync_result", "total": len(items), "items": items}


@router.get("/audits")
def list_audits(task: str | None = Query(default=None, description="按任务编号过滤")) -> dict[str, Any]:
    """同步审计结果数据集：每个动作的发起人、前后版本、失败原因与未应用项处理痕迹。"""
    items = service.list_audits(task)
    return {"module": "sync_audit", "total": len(items), "items": items}


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出配置同步域全量数据集（任务+结果+审计+快照+进度），便于离线核对。"""
    tasks, total = service.list_entries(page=1, size=10000)
    return {
        "module": "configsync",
        "total": total,
        "items": tasks,
        "results": service.list_results(),
        "audits": service.list_audits(),
        "snapshots": service.list_snapshots(),
        "progress": service.list_progress(),
    }


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条同步任务明细，并附带结果、审计、快照与进度，便于双端版本逐项核对。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"同步任务 {entry_id} 不存在或已归档")
    task_code = str(entry.get("任务编号", ""))
    return {
        **entry,
        "结果": service.list_results(task_code),
        "审计": service.list_audits(task_code),
        "应用进度": service.list_progress(task_code),
    }


@router.post("", response_model=ActionResult)
def create_entry(
    payload: EntryPayload,
    x_user_name: str | None = Header(default=None, alias="X-User-Name"),
    x_user_role: str | None = Header(default=None, alias="X-User-Role"),
) -> ActionResult:
    """登记一条同步任务；缺字段或违反唯一活动任务约束时给出可读原因。"""
    actor = _actor(x_user_name, x_user_role, payload.values)
    _require_write(actor["role"])
    try:
        entry, missing = service.create_entry(payload.values, actor)
    except SyncRuleError as exc:
        return ActionResult(ok=False, message=str(exc))
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="同步任务已登记，待发起双端版本握手", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(
    entry_id: int,
    payload: EntryPayload,
    x_user_name: str | None = Header(default=None, alias="X-User-Name"),
    x_user_role: str | None = Header(default=None, alias="X-User-Role"),
) -> ActionResult:
    """对单条任务执行握手/分段/续传/冲突/提交/失败；规则不满足时返回 ok=False 与原因。"""
    actor = _actor(x_user_name, x_user_role, payload.values)
    _require_write(actor["role"])
    action = str(payload.values.get("action") or "").strip()
    try:
        entry, message = service.run_action(entry_id, action, payload.values, actor)
    except SyncRuleError as exc:
        return ActionResult(ok=False, message=str(exc))
    return ActionResult(ok=True, message=message, entry=entry)
