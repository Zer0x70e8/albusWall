#
"""presenter 层通用的小工具：把任意缩略图裁成正方形。

为什么需要
==========

* 缩略图文件**不是**正方形：``service/thumbnail/renderer.py`` 用
  ``PIL.Image.thumbnail`` 生成，保持原始宽高比（长边等于 spec 边长）；
* 网格委托 ``InfoCellDelegate`` 用 ``drawPixmap(rect, pixmap)`` 直接铺满
  正方形格子，会把非方图**拉伸变形**；
* ``QListView`` 的 IconMode + ``QIcon`` 缩放，也会保持宽高比，留下空白边。

三处都要求「格子是方的、图也要是方的」，所以统一在 presenter 里把图
**居中裁成正方形**（只裁不拉伸），再交给视图。这就是旧版
``ThumbnailGridPresenter._center_crop_square`` 的同一套做法，只是收敛到了一处。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap

__all__ = ["square_pixmap"]

#: 居中裁剪的取值中心（相对宽 / 高，0.5 = 正中）。
_CENTER = 0.5


def square_pixmap(pixmap: QPixmap | None, *, edge: int | None = None) -> QPixmap:
    """把 ``pixmap`` 居中裁成正方形，返回新图（不修改入参）。

    Args:
        pixmap: 任意图；``None`` / 空图返回空 ``QPixmap``。
        edge: 裁剪后是否顺带缩放到 ``edge×edge`` 像素；
            ``None`` 保持裁剪后的原始边长。

    Returns:
        正方形 ``QPixmap``。已经方且尺寸等于 ``edge`` 时原样返回（零拷贝路径）。
    """
    if pixmap is None or pixmap.isNull():
        return QPixmap()

    width, height = pixmap.width(), pixmap.height()
    side = min(width, height)
    if side <= 0:
        return QPixmap()

    if (width, height) != (side, side):
        x = int(round((width - side) * _CENTER))
        y = int(round((height - side) * _CENTER))
        pixmap = pixmap.copy(x, y, side, side)

    if edge is not None and edge > 0 and pixmap.width() != int(edge):
        pixmap = pixmap.scaled(
            int(edge),
            int(edge),
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    return pixmap
