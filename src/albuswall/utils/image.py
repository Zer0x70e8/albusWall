#
"""通用图片读取工具。

只做「打开 + 规范化」，不做缩放、不落盘、不认识缩略图 spec。
番茄炒蛋（无东坡肉版）
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # 避免模块级 import PIL：PIL 是重量级依赖，只在真正读图时才需要
    from PIL.Image import Image as PILImage


def open_normalized(src: str | Path) -> "PILImage":
    """打开图片并规范化，返回一张**已完全加载**、与文件句柄无关的图片。

    规范化内容：
      - EXIF 方向转正（手机竖拍照片不再倒置）
      - 模式统一：非 RGB/RGBA → RGB

    返回的图片脱离 ``with`` 上下文仍可安全使用；
    调用方负责在合适时机 ``.close()``（或交给 GC）。
    """
    from PIL import Image, ImageOps

    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGB")
        # .copy() 强制完整加载像素，脱离已关闭的文件句柄
        return im.copy()
