#
"""缩略图域的配置：声明（``@configclass``）+ 快照（TypedDict）+ 归一化工具。

声明层
------
``@configclass("thumbnail", "main")`` 的 :class:`ThumbnailConfig` 是默认值与
类型的唯一来源。随包 ``conf.ini`` 里 `[thumbnail]` 段按注释形式写好了同名字段，
取消注释即可覆盖：

.. code-block:: ini

    [thumbnail]
    thumb_root = ~/.cache/albuswall/thumbs
    xdg_enabled = false
    batch_size = 32
    failure_cooldown_sec = 300.0
    write_max_retries = 2
    write_retry_backoff_sec = 0.2
    image_format = JPEG
    quality = 85
    specs = {"small": 128, "medium": 512, "large": 1024}
    xdg_spec_map = {"small": "normal", "medium": "x-large", "large": "xx-large"}

``version`` / ``image_format`` / ``quality`` **不参与路径生成或编码**：两种模式
都写 ``<root>/<xdg-spec>/<md5(file_uri)>.png``，格式恒为 PNG（XDG 缩略图规范
要求把 ``Thumb::URI`` / ``Thumb::MTime`` 写进 PNG tEXt 块，JPEG 没有对等位置）。
``version`` 仅作为 ``_inflight`` 去重键 ``(uuid, version)`` 的一部分保留。

快照层
------
:class:`ThumbnailServiceConfig` 是"一份配置快照"的形状；``thumbnail_config()``
一次取出，服务构造期只读这一份（生命周期内配置不再变化）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional, Tuple, TypedDict

from albuswall.common.enums import ThumbSpec
from albuswall.configue import configclass
from albuswall.configue.errors import ConfigError
from albuswall.log import getLogger

__all__ = [
    "VERSION",
    "ThumbnailConfig",
    "ThumbnailServiceConfig",
    "resolve_image_format",
    "resolve_specs",
    "resolve_thumb_root",
    "resolve_xdg_spec_map",
    "thumbnail_config",
]

_logger = getLogger(__name__)

#: 资产映射的版本号（``_inflight`` 去重键的一部分）。改它不会让旧资产
#: 映射到空文件 —— 路径只由源文件绝对路径的 md5 决定。
__version__ = "0.0.1.dev"
VERSION = __version__

#: ThumbSpec 名 → 像素边长（``[thumbnail] specs`` 的默认值）。
_DEFAULT_SPECS: Dict[str, int] = {
    ThumbSpec.SMALL.value: 128,
    ThumbSpec.MEDIUM.value: 512,
    ThumbSpec.LARGE.value: 1024,
}

#: ThumbSpec → 协议 spec 目录名（``[thumbnail] xdg_spec_map`` 的默认值）。
_DEFAULT_SPEC_MAP: Dict[ThumbSpec, str] = {
    ThumbSpec.SMALL: "normal",
    ThumbSpec.MEDIUM: "x-large",
    ThumbSpec.LARGE: "xx-large",
}

_DEFAULT_THUMB_ROOT = "~/.cache/albuswall/thumbs"


class ThumbnailServiceConfig(TypedDict):
    """缩略图服务的可调参数快照。"""

    #: ThumbSpec 名 → 像素边长
    specs: Dict[str, int]
    #: 保留字段，不参与路径 / 编码决策（两种模式恒为 PNG）
    image_format: str
    #: 保留字段，同上
    quality: int
    #: 私有模式的根目录（``~`` 未展开，用 resolve_thumb_root() 归一）
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
    xdg_spec_map: Optional[Dict[str, str]]


@configclass("thumbnail", "main")
class ThumbnailConfig:
    """``[thumbnail]`` 段的声明。"""

    thumb_root: str = _DEFAULT_THUMB_ROOT
    xdg_enabled: bool = False
    batch_size: int = 32
    failure_cooldown_sec: float = 300.0
    write_max_retries: int = 2
    write_retry_backoff_sec: float = 0.2

    # 保留字段：格式恒为 PNG，改这两个不产生任何效果。
    image_format: str = "JPEG"
    quality: int = 85

    specs: Dict[str, int] = dict(_DEFAULT_SPECS)
    xdg_spec_map: Optional[Dict[str, str]] = None


#: 声明层字段名（顺序即快照顺序）。
_FIELDS: Tuple[str, ...] = (
    "specs", "image_format", "quality", "thumb_root", "batch_size",
    "failure_cooldown_sec", "write_max_retries", "write_retry_backoff_sec",
    "xdg_enabled", "xdg_spec_map",
)


def _declared_defaults() -> Dict[str, Any]:
    """声明期默认值：``Configue`` 尚未构造时的兜底（单测 / 无配置环境）。"""
    result: Dict[str, Any] = {}
    for name in _FIELDS:
        slot = getattr(ThumbnailConfig, name, None)
        default = getattr(slot, "default", slot)
        # 可变默认值要拷贝：本模块的兜底 dict 会被多个服务实例共享。
        result[name] = dict(default) if isinstance(default, dict) else default
    return result


_THUMBNAIL_DEFAULTS: Dict[str, Any] = _declared_defaults()


def thumbnail_config() -> ThumbnailServiceConfig:
    """``[thumbnail]`` 段的快照。

    取值顺序：``@configclass`` 声明的字段 → 声明默认值兜底（记 WARNING，
    **回退不静默**）。兜底范围与 ``service/importer/config.py`` 一致
    （``ConfigError`` + 裸 ``ValueError``），编程错误不兜。
    """
    try:
        cfg = ThumbnailConfig()
        snapshot: Dict[str, Any] = {}
        for name in _FIELDS:
            value = getattr(cfg, name)
            snapshot[name] = dict(value) if isinstance(value, dict) else value
        return snapshot  # type: ignore[return-value]
    except (ConfigError, ValueError) as exc:
        _logger.warning(
            "thumbnail_config(): config system unavailable or invalid (%s: %s); "
            "falling back to declared defaults",
            type(exc).__name__, exc,
        )
        return {
            name: (dict(v) if isinstance(v, dict) else v)
            for name, v in _THUMBNAIL_DEFAULTS.items()
        }  # type: ignore[return-value]


# ==================== 归一化工具 ====================

def resolve_thumb_root(
        conf: Optional[ThumbnailServiceConfig] = None,
) -> Path:
    """私有模式的 ``thumb_root``：展开 ``~`` 并绝对化。"""
    if conf is None:
        conf = thumbnail_config()
    return Path(conf["thumb_root"]).expanduser().resolve()


def resolve_specs(
        conf: Optional[ThumbnailServiceConfig] = None,
) -> Dict[ThumbSpec, int]:
    """specs 的 key 归一化为 ``ThumbSpec``，值归一化为 int。

    空 / 非法时回退内置默认（宁可生成默认三档，也不因一个坏配置不生成）。
    """
    if conf is None:
        conf = thumbnail_config()
    result: Dict[ThumbSpec, int] = {}
    for k, v in (conf["specs"] or {}).items():
        try:
            spec = k if isinstance(k, ThumbSpec) else ThumbSpec(k)
            result[spec] = int(v)
        except (KeyError, ValueError, TypeError):
            _logger.warning("ignore invalid thumbnail spec: %r=%r", k, v)
    return result or {ThumbSpec(s): int(v) for s, v in _DEFAULT_SPECS.items()}


def resolve_image_format(
        conf: Optional[ThumbnailServiceConfig] = None,
) -> str:
    """保留字段；格式恒为 PNG，返回值只用于日志 / 诊断。"""
    if conf is None:
        conf = thumbnail_config()
    return str(conf["image_format"]).upper()


def resolve_xdg_spec_map(
        conf: Optional[ThumbnailServiceConfig] = None,
) -> Dict[ThumbSpec, str]:
    """``ThumbSpec`` → 协议 spec 目录名（两种模式通用）。"""
    if conf is None:
        conf = thumbnail_config()
    raw = conf["xdg_spec_map"]
    if not raw:
        return dict(_DEFAULT_SPEC_MAP)
    result: Dict[ThumbSpec, str] = {}
    for k, v in raw.items():
        try:
            spec = k if isinstance(k, ThumbSpec) else ThumbSpec(k)
        except (KeyError, ValueError):
            _logger.warning("ignore invalid xdg_spec_map key: %r", k)
            continue
        result[spec] = str(v)
    return result or dict(_DEFAULT_SPEC_MAP)
