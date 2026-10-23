#
"""缩略图服务配置。

声明方式
--------
用 ``ConfigField`` 描述符声明字段 —— 只需 ``from albuswall.configue import ConfigField``，
字段会自动注册到 resolver（未配置的项 fallback 到 default）。

模块级实例化一个 ``config`` 单例，其它模块直接::

    from .config import config
    size = config.batch_size

不使用 ConfigField 的延迟绑定缓存问题：descriptor 自己有 ``_cached_value``，
第一次读命中 resolver 后即缓存；``resolver.set`` / 重新 load 需要走
``ConfigField.__set__`` 或重建实例才会刷新，一般不影响本服务的只读语义。

字段与 assets 表的关系
----------------------
``version`` / ``image_format`` / ``quality`` **不再参与路径生成或编码**：
两种模式都写 ``<root>/<xdg-spec>/<md5(file_uri)>.png``，格式恒为 PNG。
``version`` 仅保留为 ``_inflight`` 去重键的一部分（``(uuid, version)``），
改它不会让旧资产映射到空文件。
"""

from pathlib import Path

from albuswall.configue import ConfigField
from albuswall.common.enums import ThumbSpec

_DEFAULT_SPECS: dict[str, int] = {
    ThumbSpec.SMALL.value: 128,
    ThumbSpec.MEDIUM.value: 512,
    ThumbSpec.LARGE.value: 1024,
}

__version__ = "0.0.1.dev"


class ThumbnailConfig:
    """缩略图服务配置。"""
    section = ConfigField("thumbnail", default={})
    version = __version__
    specs: dict = ConfigField("thumbnail", "specs", default=dict(_DEFAULT_SPECS))
    image_format: str = ConfigField("thumbnail", "image_format", default="JPEG")
    quality: int = ConfigField("thumbnail", "quality", default=85)
    thumb_root: str = ConfigField(
        "thumbnail", "thumb_root",
        default="~/.cache/albuswall/thumbs",
    )
    batch_size: int = ConfigField("thumbnail", "batch_size", default=32)
    failure_cooldown_sec: float = ConfigField(
        "thumbnail", "failure_cooldown_sec", default=300.0,
    )
    write_max_retries: int = ConfigField(
        "thumbnail", "write_max_retries", default=2,
    )
    write_retry_backoff_sec: float = ConfigField(
        "thumbnail", "write_retry_backoff_sec", default=0.2,
    )

    # ------------------------------------------------------------------ #
    # 缩略图根目录选择（布局本身两种模式同构）
    # ------------------------------------------------------------------ #
    # 两种模式共用同一套磁盘协议：<root>/<spec>/<md5(file_uri)>.png、
    # 强制 PNG、内嵌 Thumb::URI/Thumb::MTime 元数据、0700/0600 权限、
    # fail/<md5>.png 永久失败标记。唯一差异就是根目录常量。
    #
    # False（默认）→ root = thumb_root（私有目录）。
    # True         → root = $CACHE/thumbnails（与桌面环境其它应用共享；
    #                按平台惯例，见 xdg_thumbnail.xdg_cache_home）。
    xdg_enabled: bool = ConfigField("thumbnail", "xdg_enabled", default=False)

    # ThumbSpec → 协议 spec 目录的映射（两种模式通用）。
    # None 用内置默认：SMALL→normal, MEDIUM→x-large, LARGE→xx-large。
    xdg_spec_map: dict | None = ConfigField(
        "thumbnail", "xdg_spec_map", default=None,
    )


config = ThumbnailConfig()


def resolve_thumb_root() -> Path:
    """thumb_root 展开 + 绝对化。"""
    return Path(config.thumb_root).expanduser().resolve()


def resolve_specs() -> dict[ThumbSpec, int]:
    """specs 的 key 归一化为 ThumbSpec，值归一化为 int。"""
    result: dict[ThumbSpec, int] = {}
    for k, v in (config.specs or {}).items():
        spec = k if isinstance(k, ThumbSpec) else ThumbSpec(k)
        result[spec] = int(v)
    return result or {
        ThumbSpec(s): int(v) for s, v in _DEFAULT_SPECS.items()
    }


def resolve_image_format() -> str:
    return str(config.image_format).upper()


def resolve_xdg_spec_map() -> dict[ThumbSpec, str]:
    """ThumbSpec → 协议 spec 目录名（两种模式通用）。"""
    default: dict[ThumbSpec, str] = {
        ThumbSpec.SMALL: "normal",
        ThumbSpec.MEDIUM: "x-large",
        ThumbSpec.LARGE: "xx-large",
    }
    raw = config.xdg_spec_map
    if not raw:
        return default
    result: dict[ThumbSpec, str] = {}
    for k, v in raw.items():
        spec = k if isinstance(k, ThumbSpec) else ThumbSpec(k)
        result[spec] = str(v)
    return result
