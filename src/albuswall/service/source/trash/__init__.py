#
"""源回收站子包：``SourceTrashService`` + 它的配置（本模块只做再导出）。

在 ``service/source/`` 的分层里它是**第五层**，与 ``command`` 平级但方向相反：

::

    command/    SourceService        活着的时候：写行 + 发三个信号
    trash/      SourceTrashService   不要的时候：软删 → 异步 purge / 恢复

两者都不 import 对方，也不 import state / runtime / trigger；它们只在装配层
（``service/__init__.py``）通过信号相遇：

::

    SourceTrashService.sources_changed ─┬─→ SourceStateService    (重评估跟踪集)
                                        └─→ SourceTriggerService  (upsert / remove)

为什么不是 ``command/`` 里的两个方法
------------------------------------
硬删要**异步**（走 Reconciler，debounce + 失败重试 + 启动补清理），而
``SourceService`` 的契约是「被动写命令、不启动任何后台行为」。把异步调度塞
进去会让那个服务同时持有「写库」和「调度后台任务」两种职责，也让它的单测
不得不拖上一个 Reconciler。分开之后，两边都能被单独测。
"""

from .config import (
    SourceTrashConfig,
    SourceTrashSettings,
    load_source_trash_config,
    source_trash_config,
)
from .service import SourceTrashService

__all__ = [
    "SourceTrashService",
    "SourceTrashConfig",
    "SourceTrashSettings",
    "load_source_trash_config",
    "source_trash_config",
]
