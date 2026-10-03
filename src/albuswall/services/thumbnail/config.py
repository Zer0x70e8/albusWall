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
version / image_format 参与路径生成，改动必须 bump version，
否则旧资产会映射到新目录而读空文件。
"""

from pathlib import Path

from albuswall.configue import ConfigField
from albuswall.dto.thumbnail import ThumbSpec

# 默认 spec 尺寸
_DEFAULT_SPECS: dict[str, int] = {
    ThumbSpec.SMALL.value: 128,
    ThumbSpec.MEDIUM.value: 512,
    ThumbSpec.LARGE.value: 1024,
}

__version__ = "0.0.1.dev"


class ThumbnailConfig:
    """缩略图服务配置。

    section 锚点确保 ``[thumbnail]`` 段存在，其他字段都挂在该段下。
    """

    # 段锚点（default={} 保证空配置也能解析出 section）
    section = ConfigField("thumbnail", default={})

    # 缩略图目录 scheme 版本。升级生成策略（尺寸、格式、布局）必须 bump。
    # version: str = ConfigField("thumbnail", "version", default=__version__)
    version = __version__

    # spec → 最大边长（像素）。key 允许字符串形式，service 会归一化。
    specs: dict = ConfigField("thumbnail", "specs", default=dict(_DEFAULT_SPECS))

    # 输出格式与质量。service 会 upper() 归一化。
    image_format: str = ConfigField("thumbnail", "image_format", default="JPEG")
    quality: int = ConfigField("thumbnail", "quality", default=85)

    # 缩略图根目录。service 会 expanduser().resolve()。
    thumb_root: str = ConfigField(
        "thumbnail", "thumb_root",
        default="~/.cache/albuswall/thumbs",
    )

    # 单次 scan 的批量大小
    batch_size: int = ConfigField("thumbnail", "batch_size", default=32)

    # 永久失败冷却秒数：避免 scan_and_submit 对同一 asset 死循环提交
    failure_cooldown_sec: float = ConfigField(
        "thumbnail", "failure_cooldown_sec", default=300.0,
    )


# 模块级单例。其它模块 ``from .config import config`` 直接取。
config = ThumbnailConfig()


# ---------------------------------------------------------------------- #
# 辅助：把 config 中需要归一化的字段解析成 service 可直接用的形态
# ---------------------------------------------------------------------- #
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
