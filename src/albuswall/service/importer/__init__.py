#
"""导入域服务：``asset_candidate_cache`` → ``assets``（本模块只做再导出）。

三层，职责严格向下依赖，不互相 import：

::

    config.py      配置声明（@configclass "import"）+ 快照 import_config()
    processing.py  纯函数层：在进程池里跑，无 DB / 无状态 / 可 pickle
    service.py     ImportService：常驻 worker + 决策（skip / fail / ok）

依赖方向：``service → processing → config``；本包**不** import 容器、仓储
（仓储只在 ``TYPE_CHECKING`` 里出现），也不 import 其它服务包 —— 它与
``service/source`` 只在装配层通过信号相遇：

::

    SourceRuntime.scan_finished ─→ ImportService.trigger()

缩略图**不**由本包触发：改成按需生成，见
``service/view`` 的 ``assets_viewed`` → ``ThumbnailService.request_bulk``。
"""

from .config import ImportConfig, ImportServiceConfig, import_config
from .processing import ProcessResult, detect_magic, process_candidate
from .service import ImportService

__all__ = [
    "ImportConfig",
    "ImportService",
    "ImportServiceConfig",
    "ProcessResult",
    "detect_magic",
    "import_config",
    "process_candidate",
]
