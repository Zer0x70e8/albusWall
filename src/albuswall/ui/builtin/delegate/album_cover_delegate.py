#
""""""

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QLinearGradient

from albuswall.ui.builtin.delegate.square_thumb_delegate import (
    SquareThumbDelegate,
)


class AlbumCOverDelegate(SquareThumbDelegate):
    """在缩略图底部叠加标题与描述的 delegate。

    数据来源（按需替换成你自己的 role）:
        标题  ← DisplayRole
        描述  ← 自定义 role，例如 AlbumRole.Description
    """

    DESC_ROLE = Qt.ItemDataRole.UserRole + 1  # 换成你实际的 role

    def _draw_overlay(self, painter, target, option, index) -> None:
        title = index.data(Qt.ItemDataRole.DisplayRole) or ""
        desc = index.data(self.DESC_ROLE) or ""
        if not title and not desc:
            return

        # --- 底部渐变压底，保证亮图上也读得清 ---
        grad_h = max(28, target.height() // 3)
        grad_rect = QRect(
            target.x(),
            target.y() + target.height() - grad_h,
            target.width(),
            grad_h,
        )
        grad = QLinearGradient(grad_rect.topLeft(), grad_rect.bottomLeft())
        grad.setColorAt(0.0, QColor(0, 0, 0, 0))
        grad.setColorAt(1.0, QColor(0, 0, 0, 190))
        painter.fillRect(grad_rect, grad)

        # --- 字体：标题加粗，描述更小更灰 ---
        pad = 6
        text_rect = grad_rect.adjusted(pad, 2, -pad, -2)

        title_font = QFont(option.font)
        title_font.setBold(True)
        fm_title = QFontMetrics(title_font)

        desc_font = QFont(option.font)
        desc_font.setPointSizeF(max(7.0, option.font.pointSizeF() - 1))
        fm_desc = QFontMetrics(desc_font)

        # --- 从下往上排：先描述，再标题 ---
        y = text_rect.bottom()
        if desc:
            h = fm_desc.height()
            r = QRect(text_rect.x(), y - h + 1, text_rect.width(), h)
            painter.setFont(desc_font)
            painter.setPen(QColor(220, 220, 220))
            painter.drawText(
                r,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                fm_desc.elidedText(desc, Qt.TextElideMode.ElideRight, r.width()),
            )
            y -= h

        if title:
            h = fm_title.height()
            r = QRect(text_rect.x(), y - h + 1, text_rect.width(), h)
            painter.setFont(title_font)
            painter.setPen(Qt.GlobalColor.white)
            painter.drawText(
                r,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                fm_title.elidedText(title, Qt.TextElideMode.ElideRight, r.width()),
            )
