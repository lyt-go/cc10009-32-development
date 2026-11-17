"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。

配置同步域在通用表之外额外维护一组领域表（配置快照、目标应用进度、同步结果、
同步审计），并通过一把可重入锁把所有写操作串行化，保证并发发起/并发提交时
同一同步任务只会留下一个完成版本。
"""
from __future__ import annotations

import threading
from typing import Any

from app.seed import SEED_ROWS
from app.sync_seed import build_sync_domain

# 配置同步域专属的领域表名，和通用业务模块表区分开；config_node_list 是执行侧投影表，
# 只在采集时生成，不属于业务模块，不进运营概览统计。
SYNC_DOMAIN_TABLES = (
    "config_snapshot",
    "target_progress",
    "sync_result",
    "sync_audit",
    "config_node_list",
)


class Store:
    def __init__(self) -> None:
        # 可重入锁：服务层一个业务动作里可能多次进入加锁区，RLock 允许同线程重入。
        self._lock = threading.RLock()
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        # 灌入配置同步域的快照/进度/结果/审计示例数据。
        self._tables.update(build_sync_domain())

    @property
    def lock(self) -> threading.RLock:
        """对外暴露的写锁，服务层在一个完整业务动作内用 with store.lock 包住。"""
        return self._lock

    def module_names(self) -> list[str]:
        return sorted(name for name in self._tables if name not in SYNC_DOMAIN_TABLES)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def next_id(self, module: str) -> int:
        """统一的主键生成：在锁内调用，避免并发插入撞号。"""
        return max((int(row.get("id", 0)) for row in self.rows(module)), default=0) + 1

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
