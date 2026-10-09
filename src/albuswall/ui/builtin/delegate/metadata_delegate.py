#
""""""

from __future__ import annotations

from PySide6.QtCore import QModelIndex, QRect, QSize, Qt
from PySide6.QtGui import QFontMetrics, QPalette
from PySide6.QtWidgets import (
    QApplication, QStyle, QStyledItemDelegate, QStyleOptionViewItem,
)

from ..roles.metadata_roles import KEY_ROLE, VALUE_ROLE


class MetadataDelegate(QStyledItemDelegate):
    """信息面板行的渲染器。

    每行渲染成两列：
        [ label ][ value ]
    label 用次要前景色、value 用正常前景色；两者都按列宽省略号截断。
    拿不到 KEY_ROLE / VALUE_ROLE 时（如无效索引）回落到默认绘制。
    """

    ROW_MIN_HEIGHT = 24
    H_PADDING = 8
    COLUMN_GAP = 8
    # label 列宽上限，占文本区宽度的比例
    KEY_WIDTH_RATIO = 0.4

    # ------------------------------------------------------------------ #
    # 尺寸
    # ------------------------------------------------------------------ #
    def sizeHint(self, option, index: QModelIndex) -> QSize:
        size = super().sizeHint(option, index)
        return QSize(size.width(), max(size.height(), self.ROW_MIN_HEIGHT))

    # ------------------------------------------------------------------ #
    # 绘制
    # ------------------------------------------------------------------ #
    def paint(self, painter, option, index: QModelIndex) -> None:
        key = index.data(KEY_ROLE)
        value = index.data(VALUE_ROLE)
        if key is None or value is None:
            # 无效索引 / 未实现自定义 role → 走默认
            super().paint(painter, option, index)
            return

        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""  # 文本完全由下面自绘，避免默认层再画一遍

        widget = opt.widget
        style = widget.style() if widget is not None else QApplication.style()

        # 背景（含选中 / 悬停 / 交替行）
        style.drawControl(
            QStyle.ControlElement.CE_ItemViewItem, opt, painter, widget,
        )

        text_rect = style.subElementRect(
            QStyle.SubElement.SE_ItemViewItemText, opt, widget,
        )
        if not text_rect.isValid():
            text_rect = option.rect
        text_rect = text_rect.adjusted(self.H_PADDING, 0, -self.H_PADDING, 0)
        if text_rect.width() <= 0:
            return

        fm = QFontMetrics(opt.font)
        key_str = str(key)
        value_str = str(value)

        # label 列宽：自然宽度，但不超过文本区的 KEY_WIDTH_RATIO
        natural_key_w = fm.horizontalAdvance(key_str)
        max_key_w = max(0, int(text_rect.width() * self.KEY_WIDTH_RATIO))
        key_w = min(natural_key_w, max_key_w) + self.COLUMN_GAP
        key_w = min(key_w, text_rect.width())  # 极端窄列时兜底

        key_rect = QRect(
            text_rect.left(), text_rect.top(),
            key_w, text_rect.height(),
        )
        value_rect = QRect(
            text_rect.left() + key_w, text_rect.top(),
            text_rect.width() - key_w, text_rect.height(),
        )

        palette = opt.palette
        if opt.state & QStyle.StateFlag.State_Selected:
            key_color = palette.color(QPalette.ColorRole.HighlightedText)
            value_color = key_color
        else:
            # PlaceholderText 比 Text 弱一档，正好当 label 色；
            # 老 Qt 没有这个 role 时退回 Mid。
            placeholder_role = getattr(
                QPalette.ColorRole, "PlaceholderText", None,
            ) or QPalette.ColorRole.Mid
            key_color = palette.color(placeholder_role)
            value_color = palette.color(QPalette.ColorRole.Text)

        align = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        elide = Qt.TextElideMode.ElideRight

        painter.save()
        painter.setClipRect(text_rect)

        painter.setFont(opt.font)
        painter.setPen(key_color)
        painter.drawText(
            key_rect, align,
            fm.elidedText(key_str, elide, key_rect.width()),
        )

        painter.setPen(value_color)
        painter.drawText(
            value_rect, align,
            fm.elidedText(value_str, elide, value_rect.width()),
        )

        painter.restore()
