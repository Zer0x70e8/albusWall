#
"""缩略图域服务（本模块只做再导出）。

分层
----
::

    config.py    声明（@configclass "thumbnail"）+ 快照 + 归一化工具
    paths.py     base/rel 布局的唯一来源（纯函数，无 IO）
    renderer.py  源文件 → PIL.Image（纯计算，不碰磁盘）
    storage.py   PIL.Image → 磁盘（强制 PNG + 内嵌元数据 + 0700/0600）
    service.py   ThumbnailService：按需排任务 / 去重 / 冷却 / 回填 / 广播
    cleaner.py   ThumbnailCleaner + 会话脏标（孤儿 PNG 的唯一删除入口）

依赖方向：``service → storage/paths/renderer → config``。本包不 import
其它服务包；与外部只有三处接触：

::

    ViewService.assets_viewed ─→ ThumbnailService.request_bulk   （按需生成）
    ThumbnailService.thumbnail_ready ─→ UI                        （路径可读）
    TrashService（资产硬删）──→ ThumbnailService.purge_bulk        （只清 DB）

**没有**导入完成后的自动回填：缩略图只按需生成，见 ``service.py`` 顶部注释。
"""

from .cleaner import (
    CleanerSettings,
    OrphanCleanupReport,
    OrphanEntry,
    SessionConfig,
    ThumbnailCleaner,
    ThumbnailCleanerConfig,
    install_dirty_recovery,
    load_cleaner_config,
    load_session_config,
)
from .config import (
    VERSION,
    ThumbnailConfig,
    ThumbnailServiceConfig,
    resolve_specs,
    resolve_thumb_root,
    resolve_xdg_spec_map,
    thumbnail_config,
)
from .paths import build_thumb_paths, build_xdg_thumb_paths
from .renderer import is_permanent_render_error, render
from .service import ThumbnailService
from .storage import ThumbnailFormatError, ThumbnailStorage

__all__ = [
    "CleanerSettings",
    "OrphanCleanupReport",
    "OrphanEntry",
    "SessionConfig",
    "ThumbnailCleaner",
    "ThumbnailCleanerConfig",
    "ThumbnailConfig",
    "ThumbnailFormatError",
    "ThumbnailService",
    "ThumbnailServiceConfig",
    "ThumbnailStorage",
    "VERSION",
    "build_thumb_paths",
    "build_xdg_thumb_paths",
    "install_dirty_recovery",
    "is_permanent_render_error",
    "load_cleaner_config",
    "load_session_config",
    "render",
    "resolve_specs",
    "resolve_thumb_root",
    "resolve_xdg_spec_map",
    "thumbnail_config",
]
