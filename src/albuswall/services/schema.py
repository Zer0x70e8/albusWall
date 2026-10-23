#
"""服务层配置的 typed dict 形状。

**只声明「形状 + 类型」，不写默认值**：默认值与类型转换一律由各服务自己的
静态配置声明（``ConfigField``）提供，这里只负责把「一份配置快照」描述成
一个可静态检查的 dict。取值统一走
``albuswall.configue.typed.load_typed``，加载器就在各服务的 config 模块里：

============================  ================================  ==================
typed dict                    加载器                            声明来源（owner）
============================  ================================  ==================
``ThumbnailServiceConfig``    ``thumbnail_config()``            ``ThumbnailConfig``
``ImportServiceConfig``       ``import_config()``               ``ImportConfig``
``SourceTrashServiceConfig``  ``source_trash_config()``         ``_Config``
============================  ================================  ==================

加载器显式传入**声明所有者类**，而不是依赖全局注册表：``batch_size`` 这类
名字在多个模块里都存在，只有按 owner 限定才能保证拿到本模块的 section。

键名 = 声明类的属性名（不是 INI 里的键名）：例如
``ThumbnailServiceConfig["image_format"]`` 对应 ``[thumbnail] image_format``，
节名由声明里的 path 决定。
"""

from __future__ import annotations

from typing import Optional, TypedDict

__all__ = [
    "ImportServiceConfig",
    "SourceTrashServiceConfig",
    "ThumbnailServiceConfig",
]


# ══════════════════════════════════════════════════
# 缩略图服务（services/thumbnail/config.py: ThumbnailConfig）
# ══════════════════════════════════════════════════
class ThumbnailServiceConfig(TypedDict):
    """缩略图服务的可调参数快照。"""

    #: ThumbSpec 名 → 像素边长
    specs: dict[str, int]
    #: 保留字段，不参与路径 / 编码决策（两种模式恒为 PNG）
    image_format: str
    #: 保留字段，同上
    quality: int
    #: 私有模式的根目录（``~`` 未展开，用 ``resolve_thumb_root()`` 归一）
    thumb_root: str
    #: 单批提交上限
    batch_size: int
    #: 永久失败后的冷却时间（秒）
    failure_cooldown_sec: float
    #: 写盘重试次数上限
    write_max_retries: int
    #: 写盘重试退避基数（秒）
    write_retry_backoff_sec: float
    #: True → 根目录走 $XDG_CACHE_HOME/thumbnails（共享），否则走 thumb_root
    xdg_enabled: bool
    #: ThumbSpec 名 → 协议 spec 目录名；None = 用内置默认映射
    xdg_spec_map: Optional[dict[str, str]]


# ══════════════════════════════════════════════════
# 导入服务（services/import_.py: ImportConfig）
# ══════════════════════════════════════════════════
class ImportServiceConfig(TypedDict):
    """ImportService 的可调参数快照（每轮 ``process_pending`` 读一次）。"""

    #: 单轮 pending 批量上限
    batch_size: int
    #: 单个候选任务在进程池里的等待上限（秒）
    process_timeout: float
    #: worker 异常后首次退避（秒）
    initial_backoff: float
    #: worker 退避封顶（秒）
    max_backoff: float


# ══════════════════════════════════════════════════
# 源回收站 / 缩略图清理（services/source_trash/bootstrap.py: _Config）
# ══════════════════════════════════════════════════
class SourceTrashServiceConfig(TypedDict):
    """source_trash 的配置快照（bootstrap 再把它转成 frozen dataclass）。"""

    #: 回收站 reconciler 的名字
    reconciler_name: str
    #: 回收站任务的调度优先级
    priority: int
    #: 回收站扫描的 debounce（秒）
    debounce: float
    #: 缩略图孤儿清理开关；None = 按平台默认
    xdg_compat: Optional[bool]
    #: 孤儿缩略图的最小年龄（秒），防止误删刚写的缓存
    orphan_min_age_seconds: float
    #: 会话标记文件路径；None = 平台缓存目录下的 .session
    session_marker_path: Optional[str]
    #: 上次非正常退出时是否自动清理孤儿缩略图
    auto_cleanup_on_dirty: bool
    #: 缩略图删除后是否顺带修剪空目录
    prune_empty_dirs: bool
