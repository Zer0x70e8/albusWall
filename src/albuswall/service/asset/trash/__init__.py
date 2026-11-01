#
"""资产回收站子包（本模块只做再导出）。

``service/source/trash`` 与 ``service/asset/trash`` 的对称
--------------------------------------------------------
::

    service/source/trash/    源级回收站：ingest_source 行 → 异步 purge
    service/asset/trash/     资产级回收站：assets.is_deleted → 保留期到期清理

两者语义不同，**不要**互相替代：

* 源回收站删的是"整条导入源"（连同它下面的资产行）；
* 资产回收站删的是"单张照片"（先软删、30 天后物理清除，期间可恢复）。

表的所有权
----------
* ``assets.is_deleted`` / ``deleted_at`` —— 软删标记；
* ``trash_state`` —— 全库唯一的清理水位线（单例，id 恒为 1），用
  ``last_observed_at`` 缓解系统时钟被篡改导致的误删（见
  :meth:`TrashService.cleanup_expired` 的时钟防护策略）。

分层
----
::

    service.py     TrashService：查询 / 恢复 / 清理（无生命周期）
    scheduler.py   TrashCleanupScheduler：定时投递（无清理逻辑）

调度器**不感知**挂钟时间与保留期策略：周期等待基于 monotonic 时钟，
"什么时候该清"由 ``TrashService`` 的水位机制决定。
"""

from .scheduler import (
    CLEANUP_PRIORITY,
    DEFAULT_INITIAL_DELAY,
    DEFAULT_INTERVAL,
    TrashCleanupScheduler,
)
from .service import (
    ADVANCE_PER_CYCLE,
    FORWARD_SKEW,
    PURGE_BATCH_SIZE,
    TRASH_GRACE,
    TrashService,
    utcnow,
)

__all__ = [
    "ADVANCE_PER_CYCLE",
    "CLEANUP_PRIORITY",
    "DEFAULT_INITIAL_DELAY",
    "DEFAULT_INTERVAL",
    "FORWARD_SKEW",
    "PURGE_BATCH_SIZE",
    "TRASH_GRACE",
    "TrashCleanupScheduler",
    "TrashService",
    "utcnow",
]
