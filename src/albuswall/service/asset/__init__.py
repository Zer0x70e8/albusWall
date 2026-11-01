#
"""资产域服务（本模块只做再导出）。

表的所有权
----------
============================  ==========================================
表                             所有者
============================  ==========================================
``assets``（写：收藏 / 软删）   ``command/ops.py``
``assets``（写：缩略图列）      ``service/thumbnail``
``assets``（读）                ``service/view``
``album_assets``（成员关系）    ``command/ops.py``
``albums``（相簿本身）          ``service/album``
``asset_tags``                  ``command/tagging.py``
``trash_state``                 ``trash/service.py``
============================  ==========================================

两条"别抢"的边界::

    # 不要在建 service/trash/
    service/source/trash/   源级回收站（删整条导入源）
    service/asset/trash/    资产级回收站（删单张照片，30 天保留期）

    # 缩略图的文件删除只有一处
    service/thumbnail/cleaner.py   （按 PNG 内嵌 Thumb::URI 判定孤儿）

依赖方向：``command → errors``（共享异常家族）、``trash → repositories``；
``command`` 与 ``trash`` 之间只有 ``AssetOpsService → TrashService`` 一条单向边。
"""

from .command import (
    AddTagsResult,
    AssetOpsService,
    AssetTagError,
    AssetTagService,
    SetTagsResult,
)
from .trash import (
    CLEANUP_PRIORITY,
    DEFAULT_INITIAL_DELAY,
    DEFAULT_INTERVAL,
    TRASH_GRACE,
    TrashCleanupScheduler,
    TrashService,
    utcnow,
)

__all__ = [
    "AddTagsResult",
    "AssetOpsService",
    "AssetTagError",
    "AssetTagService",
    "CLEANUP_PRIORITY",
    "DEFAULT_INITIAL_DELAY",
    "DEFAULT_INTERVAL",
    "SetTagsResult",
    "TRASH_GRACE",
    "TrashCleanupScheduler",
    "TrashService",
    "utcnow",
]
