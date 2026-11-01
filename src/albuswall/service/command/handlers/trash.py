#
"""回收站命令：``trash.*``（**资产级**回收站，不是源回收站）。

别和 ``source.trash`` 搞混
-------------------------
* ``source.trash``  —— 删**整条导入源**（连带它的资产行），异步 purge；
* ``trash.*``       —— 删**单张照片**：软删 → 30 天保留期 → 物理清除，期间可恢复。

线程语义：只写 DB / 只读 DB，不碰属主线程状态，无需回主线程。

长任务提醒
----------
``trash.cleanup`` 与 ``trash.empty`` 是**同步**的（分页循环，每批短事务），
在处理大量资产时会占住 IPC 连接线程一段时间。它们被标记为 ``hidden``（属于
管理端点，不是日常操作），前台路径是 ``trash.restore`` / 定时调度器。
"""

from __future__ import annotations

import dataclasses
from enum import Enum
from typing import Any
from uuid import UUID

from albuswall.command import CommandRegistry
from albuswall.command.exceptions import BadRequest
from albuswall.core import Container

from ._namespace import register_namespace
from ._params import as_int, paging_params, pos

__all__: list[str] = []


def _jsonable(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            f.name: _jsonable(getattr(value, f.name))
            for f in dataclasses.fields(value)
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return value


def _uuid_list(value: Any, *, field: str) -> list[UUID]:
    """``uuid`` / ``"a,b"`` / ``["a", "b"]`` → ``[UUID, ...]``（逗号串也收）。"""
    if value is None:
        return []
    if isinstance(value, UUID):
        return [value]
    if isinstance(value, str):
        return [
            UUID(part.strip())
            for part in value.strip().strip("[]").split(",") if part.strip()
        ]
    if isinstance(value, (list, tuple, set, frozenset)):
        return [UUID(str(v)) for v in value]
    raise BadRequest(f"{field}: expected a uuid or a list of uuids")


def setup(registry: CommandRegistry, container: Container) -> None:
    trash = container.require("trash_service")
    scheduler = container.require("trash_cleanup_scheduler")

    @registry.command(
        "trash.list",
        help_="回收站列表（按删除时间倒序）",
        params=paging_params(default=50),
    )
    def trash_list(limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        return [
            _jsonable(row) for row in trash.list_deleted(
                as_int(limit, field="limit", minimum=1, maximum=500),
                as_int(offset, field="offset", minimum=0),
            )
        ]

    @registry.command(
        "trash.count",
        help_="回收站当前资产数（快照）",
        params=(),
    )
    def trash_count() -> dict[str, Any]:
        return {"count": trash.count_deleted()}

    @registry.command(
        "trash.state",
        help_="清理水位线状态（last_observed_at 等），用于排查时钟问题",
        params=(),
    )
    def trash_state() -> dict[str, Any]:
        return _jsonable(trash.get_state())

    @registry.command(
        "trash.restore",
        help_="从回收站恢复一批资产（uuid）",
        params=(pos("asset_uuids", help="uuid 或 uuid 列表"),),
    )
    def trash_restore(asset_uuids: Any) -> dict[str, Any]:
        return _jsonable(
            trash.restore_by_uuids(_uuid_list(asset_uuids, field="asset_uuids"))
        )

    @registry.command(
        "trash.empty",
        help_="清空回收站（物理删除，不可恢复）",
        hidden=True,
        params=(),
    )
    def trash_empty() -> dict[str, Any]:
        return _jsonable(trash.empty_trash())

    @registry.command(
        "trash.cleanup",
        help_="立即跑一轮保留期清理（水位判定照旧，时钟异常时不会删）",
        hidden=True,
        params=(),
    )
    def trash_cleanup() -> dict[str, Any]:
        return _jsonable(trash.cleanup_expired())

    @registry.command(
        "trash.cleanup-async",
        help_="把清理投递给 TaskService（不等待；已有在途时返回 false）",
        hidden=True,
        params=(),
    )
    def trash_cleanup_async() -> dict[str, Any]:
        return {"submitted": scheduler.trigger_now() is not None}

    @registry.command(
        "trash.policy",
        help_="回收站保留期策略（只读常量，便于 UI 展示多久会被真删）",
        params=(),
    )
    def trash_policy() -> dict[str, Any]:
        from albuswall.service.asset.trash import (
            ADVANCE_PER_CYCLE,
            FORWARD_SKEW,
            PURGE_BATCH_SIZE,
            TRASH_GRACE,
        )

        return {
            "grace_days": TRASH_GRACE.days,
            "forward_skew_hours": FORWARD_SKEW.total_seconds() / 3600,
            "advance_per_cycle_hours":
                ADVANCE_PER_CYCLE.total_seconds() / 3600,
            "batch_size": PURGE_BATCH_SIZE,
        }

    # ---- 命名空间入口（与 ``task`` 同一形状）----
    # 必须在叶子命令之后注册：用法清单与转发都读注册表。
    register_namespace(
        registry, "trash",
        help_="资产回收站命令入口（trash <sub> ...）",
        hint="注意：删整条导入源是 albuswall source trash，不是这里的 trash。",
    )
