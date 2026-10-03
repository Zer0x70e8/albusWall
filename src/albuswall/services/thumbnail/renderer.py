#
"""纯计算：源文件 → {spec: PIL.Image}。不碰 DB、不碰磁盘写入。
"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, TYPE_CHECKING

from albuswall.dto.thumbnail import ThumbSpec
from albuswall.utils.image import open_normalized

if TYPE_CHECKING:
    from PIL.Image import Image as PILImage


def is_permanent_render_error(exc: BaseException) -> bool:
    """True 表示「这个文件永远渲染不出来」，重试无意义。

    覆盖：PIL 无法识别的文件格式（非图片、损坏文件头）。
    视频、PDF 等也会落到这里，直接走永久性失败路径。
    """
    try:
        from PIL import UnidentifiedImageError
    except ImportError:  # pragma: no cover - Pillow 一定存在
        return False
    return isinstance(exc, UnidentifiedImageError)


def render(
        src: str | Path,
        specs: Mapping[ThumbSpec, int],
) -> dict[ThumbSpec, "PILImage"]:
    """读取 src，按 specs 生成多规格缩略图。

    任一 spec 生成失败会向上抛；调用方按 is_permanent_render_error 分类。
    返回的 PIL.Image 与文件句柄无关，调用方负责 close。
    """
    from PIL import Image

    # 兼容老版本 Pillow：Resampling 枚举是 9.1+ 才有的
    resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS")

    base = open_normalized(src)
    result: dict[ThumbSpec, "PILImage"] = {}
    try:
        for spec, size in specs.items():
            thumb = base.copy()
            thumb.thumbnail((size, size), resample)
            result[spec] = thumb
    finally:
        base.close()
    return result
