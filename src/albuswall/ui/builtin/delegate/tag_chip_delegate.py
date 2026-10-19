#
"""TagBar chip 绘制：圆角背景 + 文字 + 右侧移除按钮。"""

from __future__ import annotations

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem


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

    # ---- 尺寸 ----
    def sizeHint(self, option: QStyleOptionViewItem, index) -> QSize:
        text = index.data(Qt.ItemDataRole.DisplayRole) or ""
        fm = option.fontMetrics
        w = (
                self.H_PADDING * 2
                + fm.horizontalAdvance(text)
                + self.GAP
                + self.X_SIZE
        )
        h = fm.height() + self.V_PADDING * 2
        return QSize(w, h)

    # ---- 绘制 ----
    def paint(self, painter: QPainter, option, index) -> None:
        text = index.data(Qt.ItemDataRole.DisplayRole) or ""
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        rect = option.rect.adjusted(2, 2, -2, -2)
        painter.setBrush(self.BG_HOVER if hover else self.BG)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(rect, self.RADIUS, self.RADIUS)

        # 文字
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
        m = 3
        painter.drawLine(
            x_rect.left() + m, x_rect.top() + m,
            x_rect.right() - m, x_rect.bottom() - m,
        )
        painter.drawLine(
            x_rect.right() - m, x_rect.top() + m,
            x_rect.left() + m, x_rect.bottom() - m,
        )
        painter.restore()

    # ---- 让 view 判定点击区域 ----
    def remove_button_rect(self, rect: QRect) -> QRect:
        return QRect(
            rect.right() - self.H_PADDING - self.X_SIZE,
            rect.center().y() - self.X_SIZE // 2,
            self.X_SIZE,
            self.X_SIZE,
        )
