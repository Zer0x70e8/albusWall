#
"""信息面板委托：一行渲染成两列 ``[标签][值]``。

标签用次要前景色、值用正常前景色，两者都按列宽省略号截断。数据只从
:class:`~albuswall.ui.model.roles.MetadataRole` 取，因此和
:class:`~albuswall.ui.model.MetadataModel` 是天生一对，但也能吃任何实现了
同样 role 的模型；拿不到 role（无效索引）时回落到默认绘制。

可调项：行高、左右内边距、标签列宽比例、是否给标签加粗。
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QModelIndex, QRect, QSize, Qt
from PySide6.QtGui import QFont, QFontMetrics, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
)

from ..model.roles import MetadataRole

__all__ = ["MetadataDelegate"]


class MetadataDelegate(QStyledItemDelegate):
    """信息面板键值行的绘制委托。"""

    def __init__(
            self,
            parent: Any = None,
            *,
            row_min_height: int = 24,
            h_padding: int = 8,
            column_gap: int = 8,
            key_width_ratio: float = 0.4,
            bold_key: bool = True,
    ) -> None:
        super().__init__(parent)
        self._row_min_height = max(1, int(row_min_height))
        self._h_padding = max(0, int(h_padding))
        self._column_gap = max(0, int(column_gap))
        self._key_width_ratio = max(0.0, min(1.0, float(key_width_ratio)))
        self._bold_key = bool(bold_key)

    # ------------------------------------------------------------------ #
    # 尺寸
    # ------------------------------------------------------------------ #
    def sizeHint(  # noqa: N802 - Qt 命名
            self,
            option: QStyleOptionViewItem,
            index: QModelIndex,
    ) -> QSize:
        size = super().sizeHint(option, index)
        return QSize(size.width(), max(size.height(), self._row_min_height))

    # ------------------------------------------------------------------ #
    # 绘制
    # ------------------------------------------------------------------ #
    def paint(self, painter, option: QStyleOptionViewItem, index) -> None:
        key = index.data(MetadataRole.Key)
        value = index.data(MetadataRole.Value)
        if key is None:
            # 无效索引 / 未实现 role → 走默认绘制
            super().paint(painter, option, index)
            return

        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""  # 文本完全由下面自绘，避免默认层再画一遍

        widget = opt.widget
        style = widget.style() if widget is not None else QApplication.style()
        style.drawControl(
            QStyle.ControlElement.CE_ItemViewItem, opt, painter, widget)

        text_rect = style.subElementRect(
            QStyle.SubElement.SE_ItemViewItemText, opt, widget)
        if not text_rect.isValid():
            text_rect = option.rect
        text_rect = text_rect.adjusted(
            self._h_padding, 0, -self._h_padding, 0)
        if text_rect.width() <= 0:
            return

        key_text = str(key)
        value_text = "" if value is None else str(value)

        key_font = QFont(opt.font)
        if self._bold_key:
            key_font.setBold(True)
        key_metrics = QFontMetrics(key_font)
        value_metrics = QFontMetrics(opt.font)

        # 标签列宽：自然宽度，但不超过文本区的 key_width_ratio
        natural_width = key_metrics.horizontalAdvance(key_text)
        max_width = max(0, int(text_rect.width() * self._key_width_ratio))
        key_width = min(natural_width, max_width) + self._column_gap
        key_width = min(key_width, text_rect.width())  # 极端窄列时兜底

        key_rect = QRect(
            text_rect.left(), text_rect.top(), key_width, text_rect.height())
        value_rect = QRect(
            text_rect.left() + key_width,
            text_rect.top(),
            text_rect.width() - key_width,
            text_rect.height(),
        )

        key_color, value_color = self._colors(opt.palette, opt)
        align = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        elide = Qt.TextElideMode.ElideRight

        painter.save()
        painter.setClipRect(text_rect)

        painter.setFont(key_font)
        painter.setPen(key_color)
        painter.drawText(
            key_rect, align,
            key_metrics.elidedText(key_text, elide, key_rect.width()),
        )

        painter.setFont(opt.font)
        painter.setPen(value_color)
        painter.drawText(
            value_rect, align,
            value_metrics.elidedText(value_text, elide, value_rect.width()),
        )

        painter.restore()

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    @staticmethod
    def _colors(palette: QPalette, opt: QStyleOptionViewItem):
        """选中态整体用高亮文字色，否则标签弱一档、值正常。"""
        if opt.state & QStyle.StateFlag.State_Selected:
            color = palette.color(QPalette.ColorRole.HighlightedText)
            return color, color

        # PlaceholderText 比 Text 弱一档，正好当标签色；老 Qt 没有它时退回 Mid
        placeholder = getattr(QPalette.ColorRole, "PlaceholderText", None)
        key_color = palette.color(
            placeholder or QPalette.ColorRole.Mid)
        value_color = palette.color(QPalette.ColorRole.Text)
        return key_color, value_color
