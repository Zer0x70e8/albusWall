#
"""通用正方形缩略图 delegate。

负责：把任意 pixmap 缩放裁剪成正方形并绘制到 item 矩形内。
不负责：认识任何具体 model。

扩展点（子类覆写）:
    _draw_content      在正方形区域内绘制主体（默认：画裁剪后的 pixmap）
    _draw_placeholder  无 pixmap 时绘制（默认：什么都不画）
    _draw_overlay      在主体之上叠加（默认：什么都不画；标题/徽标在这画）
    _draw_selection    选中态（默认：白色描边）

复用工具:
    crop_square        静态方法，把 pixmap 裁成 side×side 正方形
"""
from __future__ import annotations

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem


# noinspection pep8-naming
class SquareThumbDelegate(QStyledItemDelegate):
    """把缩略图按 KeepAspectRatioByExpanding 缩放后居中裁剪为正方形。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._decoration_role: int = Qt.ItemDataRole.DecorationRole
        self._size_hint = QSize(64, 64)

    # ---------- 配置 ----------
    def setDecorationRole(self, role: int) -> None:
        self._decoration_role = role

    def setDefaultSizeHint(self, size: QSize) -> None:
        """设置 sizeHint。仅当 view 未 setGridSize 时才有意义。"""
        self._size_hint = QSize(size)

    def sizeHint(self, option, index) -> QSize:
        return self._size_hint

    # ---------- 主绘制流程 ----------
    def paint(self, painter: QPainter,
              option: QStyleOptionViewItem, index) -> None:
        rect = option.rect
        if rect.width() <= 0 or rect.height() <= 0:
            return

        side = min(rect.width(), rect.height())
        target = QRect(
            rect.x() + (rect.width() - side) // 2,
            rect.y() + (rect.height() - side) // 2,
            side, side,
        )

        pixmap = index.data(self._decoration_role)
        ready = isinstance(pixmap, QPixmap) and not pixmap.isNull()

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        if ready:
            cropped = self.crop_square(pixmap, side)
            self._draw_content(painter, target, cropped, option, index)
        else:
            self._draw_placeholder(painter, target, option, index)
        painter.restore()

        painter.save()
        self._draw_overlay(painter, target, option, index)
        painter.restore()

        painter.save()
        self._draw_selection(painter, target, option, index)
        painter.restore()

    # ---------- 工具 ----------
    @staticmethod
    def crop_square(pixmap: QPixmap, side: int) -> QPixmap:
        """把 pixmap 裁成 side×side 正方形，内容水平/垂直居中。

        流程：
            1) 在源图里按短边居中裁出正方形（保留原像素，不缩放）；
            2) 把正方形缩放到 side×side。

        输出尺寸由第 2 步直接决定，不受 `scaled()` 浮点取整影响，
        任意输入都严格返回 side×side，且纵横两方向均居中。
        """
        if side <= 0 or pixmap.isNull():
            return QPixmap()

        src_w, src_h = pixmap.width(), pixmap.height()
        if src_w <= 0 or src_h <= 0:
            return QPixmap()

        edge = min(src_w, src_h)
        x = (src_w - edge) // 2
        y = (src_h - edge) // 2
        square = pixmap.copy(x, y, edge, edge)

        if square.width() == side and square.height() == side:
            return square
        return square.scaled(
            side, side,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

    # ---------- 钩子：默认实现 ----------
    @staticmethod
    def _draw_content(painter, target, pixmap, option, index) -> None:
        painter.drawPixmap(target, pixmap)

    def _draw_placeholder(self, painter, target, option, index) -> None:
        pass  # 想画占位就覆写

    def _draw_overlay(self, painter, target, option, index) -> None:
        pass  # 标题 / 描述 / 徽标都从这进

    # noinspection unsupported-operator
    @staticmethod
    def _draw_selection(painter, target, option, index) -> None:
        if option.state & QStyle.StateFlag.State_Selected:
            painter.setPen(Qt.GlobalColor.white)
            painter.drawRect(target.adjusted(0, 0, -1, -1))
