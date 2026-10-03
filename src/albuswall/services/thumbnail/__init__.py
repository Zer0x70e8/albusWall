#
"""缩略图服务包。

对外公开：
    - ThumbnailService  —— 编排器
    - ThumbnailConfig   —— 配置类
    - config            —— 配置单例（从它上面取字段）

内部件（不保证兼容）：
    - paths / renderer / storage
"""

from .config import ThumbnailConfig, config
from .service import ThumbnailService

__all__ = ["ThumbnailService", "ThumbnailConfig", "config"]
