#
"""缩略图服务包。

对外公开：
    - ThumbnailService   —— 编排器
    - ThumbnailConfig    —— 静态配置声明（默认值 / 类型的唯一来源）
    - thumbnail_config   —— typed dict 快照（优先用这个）
    - config             —— 旧描述符单例（兼容入口）
    - resolve_*          —— 归一化帮助函数（thumb_root / specs / xdg_spec_map）

内部件（不保证兼容）：
    - paths / renderer / storage
"""

from .config import (
    VERSION,
    ThumbnailConfig,
    config,
    resolve_image_format,
    resolve_specs,
    resolve_thumb_root,
    resolve_xdg_spec_map,
    thumbnail_config,
)
from .service import ThumbnailService

__all__ = [
    "VERSION",
    "ThumbnailConfig",
    "ThumbnailService",
    "config",
    "resolve_image_format",
    "resolve_specs",
    "resolve_thumb_root",
    "resolve_xdg_spec_map",
    "thumbnail_config",
]
