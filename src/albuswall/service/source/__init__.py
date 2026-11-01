#
"""导入源域服务：五层拆分（本模块**只做再导出**，不含任何逻辑）。

分层
----
::

    command/    SourceService          被动写：写 ingest_source + 发三个信号
    state/      SourceStateService     运行时数据：有效性 / 挂载 / 跟踪集 → 对账计划
    runtime/    SourceRuntime          常驻任务：执行体的创建 / 替换 / 提交
    trigger/    SourceTriggerService   配置桥接：trigger_config → TriggerFacadeService
    trash/      SourceTrashService     回收站：软删 → 异步 purge（Reconciler）/ 恢复
    errors.py   共享异常家族           五个子包共同 import

依赖方向（无环，验收项见规格书 §10）
------------------------------------
::

    command  ──→ errors
    runtime  ──→ errors, runtime.executor, runtime.scanning
    state    ──→ errors, runtime            （state → runtime 单向）
    trigger  ──→ errors
    trash    ──→ errors, trash.config       （唯一持有 Reconciler 的子包）
    errors   ──→ albuswall.service.errors

``command`` / ``runtime`` / ``trigger`` 三者之间**没有任何** import 关系，
因此谁先被 import 都成立；它们只在 ``service/__init__.py`` 的装配层里
通过**信号对象**相遇：

.. code-block:: text

    SourceService.source_created ─┬─→ SourceStateService   (建任务)
                                  └─→ SourceTriggerService (upsert 触发器)
    SourceService.source_changed ─┬─→ SourceStateService
                                  └─→ SourceTriggerService
    SourceService.source_deleted ─┬─→ SourceStateService   (撤任务)
                                  └─→ SourceTriggerService (remove)
    SourceTrashService.sources_changed ─→ State + Trigger（按 kind 分发）
    trigger_refresh_signal ─→ SourceStateService.refresh_source
    SourceRuntime.scan_finished ─→ ImportService.trigger
"""

from .command import SourceService
from .errors import (
    SourceCreateFailed,
    SourceDeleteFailed,
    SourceError,
    SourceMountFailed,
    SourceNotFound,
    SourcePathInvalid,
    SourceServiceClosed,
    SourceUpdateFailed,
    SourceVirtualRoot,
)
from .runtime import ScanTaskSnapshot, SourceExecutor, SourceRuntime
from .state import SourceStateService, TrackingState
from .trash import SourceTrashConfig, SourceTrashService
from .trigger import SourceTriggerService

__all__ = [
    # 服务
    "SourceService",
    "SourceStateService",
    "SourceRuntime",
    "SourceTriggerService",
    "SourceTrashService",
    # 附属件
    "ScanTaskSnapshot",
    "SourceExecutor",
    "SourceTrashConfig",
    "TrackingState",
    # 异常家族
    "SourceCreateFailed",
    "SourceDeleteFailed",
    "SourceError",
    "SourceMountFailed",
    "SourceNotFound",
    "SourcePathInvalid",
    "SourceServiceClosed",
    "SourceUpdateFailed",
    "SourceVirtualRoot",
]
