#
"""相册封面委托：正方形封面 + 底部渐变标题 / 描述 + 数量角标。

数据只从 role 取（:class:`~albuswall.ui.model.roles.ItemRole`），所以同一份
委托既能吃 :class:`~albuswall.ui.model.AlbumModel`，也能吃任何实现了
``ItemRole.Title`` / ``ItemRole.Description`` / ``ItemRole.AssetCount`` 的模型。

可调项（构造参数或 setter）：

* ``radius``        圆角半径（像素，0 表示直角）
* ``padding``       格子内边距
* ``overlay_ratio`` 底部渐变占格子高度的比例（默认 1/3）
* ``show_description`` / ``show_count``  是否画副标题 / 数量角标
* ``selection_color`` / ``selection_width``  选中描边颜色 / 线宽

选中态说明：默认的 item 高亮会**铺满整个格子**（相册格子没有文字行，
所以看上去是「封面下方多出一条蓝边」）。这里把它换成贴在封面上的圆角
描边：底色交给样式表压掉（见 :meth:`SquareThumbDelegate.
set_selection_background_visible`），描边由 :meth:`_draw_selection` 画在
封面内沿，圆角与封面一致。
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QRect, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QLinearGradient,
    QPainter,
    QPen,
)
from PySide6.QtWidgets import QStyle, QStyleOptionViewItem

from ..model.roles import ItemRole
from .square_thumb_delegate import SquareThumbDelegate

__all__ = ["AlbumCoverDelegate"]

#: 选中描边默认色（与全局 ``selection-background-color`` 同色系，但不透明）
_DEFAULT_SELECTION_COLOR = QColor(0, 122, 255, 235)


class AlbumCoverDelegate(SquareThumbDelegate):
    """相册格子的绘制委托（封面 + 标题 + 描述 + 数量）。"""

    def __init__(
            self,
            parent: Any = None,
            *,
            radius: int = 8,
            padding: int = 4,
            overlay_ratio: float = 1 / 3,
            show_description: bool = True,
            show_count: bool = True,
            selection_color: QColor | None = None,
            selection_width: int = 2,
    ) -> None:
        super().__init__(
            parent,
            decoration_role=int(ItemRole.Pixmap),
            padding=padding,
            border_radius=radius,
        )
        self._overlay_ratio = _clamp(overlay_ratio, 0.0, 1.0)
        self._show_description = bool(show_description)
        self._show_count = bool(show_count)
        self._selection_color = QColor(selection_color or _DEFAULT_SELECTION_COLOR)
        self._selection_width = max(1, int(selection_width))
        # 选中底色不交给样式表：整格铺蓝会溢出到封面下方，改成封面描边
        self.set_selection_background_visible(False)

    # ------------------------------------------------------------------ #
    # 外观配置
    # ------------------------------------------------------------------ #
    def set_selection_color(self, color: QColor) -> None:
        """选中描边颜色。"""
        self._selection_color = QColor(color)
        self._invalidate()

    def selection_color(self) -> QColor:
        return QColor(self._selection_color)

    def set_selection_width(self, width: int) -> None:
        """选中描边线宽（像素，最小 1）。"""
        self._selection_width = max(1, int(width))
        self._invalidate()

    def selection_width(self) -> int:
        return self._selection_width

    def set_show_description(self, visible: bool) -> None:
        self._show_description = bool(visible)
        self._invalidate()

    def set_show_count(self, visible: bool) -> None:
        self._show_count = bool(visible)
        self._invalidate()

    def set_overlay_ratio(self, ratio: float) -> None:
        self._overlay_ratio = _clamp(ratio, 0.0, 1.0)
        self._invalidate()

    # ------------------------------------------------------------------ #
    # 绘制
    # ------------------------------------------------------------------ #
    def _draw_overlay(
            self,
            painter: QPainter,
            target: QRect,
            option: QStyleOptionViewItem,
            index,
    ) -> None:
        title = str(index.data(ItemRole.Title) or "")
        description = (
            str(index.data(ItemRole.Description) or "")
            if self._show_description else ""
        )
        count = index.data(ItemRole.AssetCount) if self._show_count else None

        if not title and not description and count in (None, 0):
            return

        gradient_height = max(24, int(target.height() * self._overlay_ratio))
        gradient_rect = QRect(
            target.x(),
            target.y() + target.height() - gradient_height,
            target.width(),
            gradient_height,
        )
        gradient = QLinearGradient(
            gradient_rect.topLeft(), gradient_rect.bottomLeft())
        gradient.setColorAt(0.0, QColor(0, 0, 0, 0))
        gradient.setColorAt(1.0, QColor(0, 0, 0, 190))
        painter.fillRect(gradient_rect, gradient)

        if count not in (None, 0):
            self.draw_badge(painter, target, str(count))

        if not title and not description:
            return

        padding = 6
        text_rect = gradient_rect.adjusted(padding, 2, -padding, -2)
        base_font = option.font

        title_font = QFont(base_font)
        title_font.setBold(True)
        title_metrics = QFontMetrics(title_font)

        description_font = QFont(base_font)
        description_font.setPointSizeF(
            max(7.0, base_font.pointSizeF() - 1.0))
        description_metrics = QFontMetrics(description_font)

        # 从下往上排：先描述，再标题
        cursor = text_rect.bottom()
        if description:
            height = description_metrics.height()
            rect = QRect(
                text_rect.x(), cursor - height + 1, text_rect.width(), height)
            painter.setFont(description_font)
            painter.setPen(QColor(220, 220, 220))
            painter.drawText(
                rect,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                description_metrics.elidedText(
                    description, Qt.TextElideMode.ElideRight, rect.width()),
            )
            cursor -= height

        if title:
            height = title_metrics.height()
            rect = QRect(
                text_rect.x(), cursor - height + 1, text_rect.width(), height)
            painter.setFont(title_font)
            painter.setPen(Qt.GlobalColor.white)
            painter.drawText(
                rect,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                title_metrics.elidedText(
                    title, Qt.TextElideMode.ElideRight, rect.width()),
            )

    def _draw_selection(
            self,
            painter: QPainter,
            target: QRect,
            option: QStyleOptionViewItem,
            index,  # noqa: ARG002
    ) -> None:
        """选中态：沿封面内沿画一圈圆角蓝色描边（不再铺整格蓝底）。"""
        if not (option.state & QStyle.StateFlag.State_Selected):
            return
        if target.isEmpty():
            return

        width = self._selection_width
        inset = width / 2.0
        rect = QRectF(target).adjusted(inset, inset, -inset, -inset)
        if rect.width() <= 0 or rect.height() <= 0:
            return
        radius = max(0.0, self.border_radius() - inset)

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pen = QPen(self._selection_color, width)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(rect, radius, radius)
        painter.restore()

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _invalidate(self) -> None:
        parent = self.parent()
        if parent is not None and hasattr(parent, "update"):
            parent.update()


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))
