#
"""标签 chip 的绘制委托：圆角背景 + 文字 + 右侧移除按钮。

纯绘制，不认识任何 model —— 文本走 ``DisplayRole``，点击区域由
:meth:`remove_button_rect` 提供给 view 判定。
"""

from __future__ import annotations

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem

__all__ = ["TagChipDelegate"]


class TagChipDelegate(QStyledItemDelegate):
    # 布局常量集中在此，换皮肤只改这里
    H_PADDING = 10
    V_PADDING = 4
    GAP = 6
    X_SIZE = 14
    RADIUS = 10

    BG = QColor(240, 240, 240)
    BG_HOVER = QColor(228, 228, 228)
    FG = QColor(40, 40, 40)
    X_COLOR = QColor(120, 120, 120)
    X_COLOR_HOVER = QColor(60, 60, 60)
    #: 键盘选中描边（主题强调色）
    SELECTED_BORDER = QColor(0, 122, 255)

    # ---- 尺寸 ----
    def sizeHint(self, option: QStyleOptionViewItem, index) -> QSize:
        text = index.data(Qt.ItemDataRole.DisplayRole) or ""
        fm = option.fontMetrics
        width = (
                self.H_PADDING * 2
                + fm.horizontalAdvance(text)
                + self.GAP
                + self.X_SIZE
        )
        height = fm.height() + self.V_PADDING * 2
        return QSize(width, height)

    # ---- 绘制 ----
    def paint(self, painter: QPainter, option, index) -> None:
        text = index.data(Qt.ItemDataRole.DisplayRole) or ""
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        view = option.widget
        focused = bool(view is not None and view.hasFocus())
        # 只有视图真正持有键盘焦点时才画选中描边
        selected = bool(
            option.state & QStyle.StateFlag.State_Selected
        ) and focused

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        rect = option.rect.adjusted(2, 2, -2, -2)
        painter.setBrush(self.BG_HOVER if (hover or selected) else self.BG)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(rect, self.RADIUS, self.RADIUS)

        if selected:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(self.SELECTED_BORDER, 1.6))
            painter.drawRoundedRect(
                rect.adjusted(1, 1, -1, -1), self.RADIUS, self.RADIUS,
            )

        text_rect = QRect(
            rect.left() + self.H_PADDING,
            rect.top(),
            rect.width() - self.H_PADDING * 2 - self.X_SIZE - self.GAP,
            rect.height(),
        )
        painter.setPen(QPen(self.FG))
        painter.drawText(
            text_rect,
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            text,
        )

        # X 按钮
        x_rect = self.remove_button_rect(rect)
        painter.setPen(QPen(self.X_COLOR_HOVER if hover else self.X_COLOR, 1.6))
        margin = 3
        painter.drawLine(
            x_rect.left() + margin, x_rect.top() + margin,
            x_rect.right() - margin, x_rect.bottom() - margin,
        )
        painter.drawLine(
            x_rect.right() - margin, x_rect.top() + margin,
            x_rect.left() + margin, x_rect.bottom() - margin,
        )
        painter.restore()

    # ---- 供 view 判定点击区域 ----
    def remove_button_rect(self, rect: QRect) -> QRect:
        return QRect(
            rect.right() - self.H_PADDING - self.X_SIZE,
            rect.center().y() - self.X_SIZE // 2,
            self.X_SIZE,
            self.X_SIZE,
        )
