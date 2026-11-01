#
"""资产格子委托：缩略图 + 收藏 / 倒计时角标（+ 可选标题）。

旧版把「收藏图标 / 回收站倒计时」做成一个独立的 ``InfoCellDelegate``，
既绑死在虚拟滚动控件上，又要求外部注入一堆 getter。这里改成**数据驱动**：
状态直接读 :class:`~albuswall.ui.model.roles.ItemRole` 的 ``Favorite`` /
``DaysLeft`` / ``Deleted``，模型有什么就画什么，没有（``None``）就不画。

因此同一个委托能服务：

* 相册内的资产网格（``SquareGridView``）—— 有 ``QPixmap`` 就是缩略图；
* 任何实现了 ``ItemRole`` 的模型 —— 不需要改委托。

``None`` 与 ``False`` 语义不同：``None`` 表示「状态未知」，不画角标；
``False`` 才表示「明确未收藏」。
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import QStyleOptionViewItem

from ..model.roles import ItemRole
from .square_thumb_delegate import SquareThumbDelegate

__all__ = ["AssetThumbDelegate"]


class AssetThumbDelegate(SquareThumbDelegate):
    """资产格子的绘制委托。

    :param radius:         圆角半径（像素，0 表示直角）
    :param padding:        格子内边距
    :param show_favorite:  是否在已收藏的格子上画星标
    :param show_days_left: 是否在回收站格子上画剩余天数角标
    :param show_deleted:   是否给软删格子压一层灰（视觉降权）
    :param show_title:     是否在底部叠一行标题
    """

    def __init__(
            self,
            parent: Any = None,
            *,
            radius: int = 8,
            padding: int = 4,
            show_favorite: bool = True,
            show_days_left: bool = True,
            show_deleted: bool = True,
            show_title: bool = False,
    ) -> None:
        super().__init__(
            parent,
            decoration_role=int(ItemRole.Pixmap),
            padding=padding,
            border_radius=radius,
        )
        self._show_favorite = bool(show_favorite)
        self._show_days_left = bool(show_days_left)
        self._show_deleted = bool(show_deleted)
        self._show_title = bool(show_title)

    # ------------------------------------------------------------------ #
    # 外观配置
    # ------------------------------------------------------------------ #
    def set_show_favorite(self, visible: bool) -> None:
        self._show_favorite = bool(visible)

    def set_show_days_left(self, visible: bool) -> None:
        self._show_days_left = bool(visible)

    def set_show_deleted(self, visible: bool) -> None:
        self._show_deleted = bool(visible)

    def set_show_title(self, visible: bool) -> None:
        self._show_title = bool(visible)

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
        deleted = index.data(ItemRole.Deleted)
        if self._show_deleted and deleted is True:
            painter.fillRect(target, QColor(0, 0, 0, 120))

        if self._show_title:
            self._draw_title(painter, target, option, index, deleted)

        favorite = index.data(ItemRole.Favorite)
        if self._show_favorite and favorite is True:
            self._draw_star(painter, target)

        days_left = index.data(ItemRole.DaysLeft)
        if self._show_days_left and days_left is not None:
            self.draw_badge(
                painter,
                target,
                self._days_text(int(days_left)),
                background=QColor(180, 60, 60, 200),
            )

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _draw_title(
            self,
            painter: QPainter,
            target: QRect,
            option: QStyleOptionViewItem,
            index,
            deleted: Any,
    ) -> None:
        title = str(index.data(ItemRole.Title) or "")
        if not title:
            return

        height = max(18, option.fontMetrics.height() + 6)
        band = QRect(
            target.x(),
            target.y() + target.height() - height,
            target.width(),
            height,
        )
        painter.fillRect(band, QColor(0, 0, 0, 140))

        font = QFont(option.font)
        painter.setFont(font)
        painter.setPen(
            QColor(200, 200, 200, 220) if deleted is True
            else QColor(255, 255, 255, 235)
        )
        metrics = painter.fontMetrics()
        painter.drawText(
            band.adjusted(6, 0, -6, 0),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            metrics.elidedText(
                title, Qt.TextElideMode.ElideMiddle, band.width() - 12),
        )

    @staticmethod
    def _draw_star(painter: QPainter, target: QRect) -> None:
        painter.save()
        font = QFont(painter.font())
        font.setPointSizeF(max(9.0, font.pointSizeF() + 2))
        painter.setFont(font)
        painter.setPen(QColor(255, 214, 92))
        rect = QRect(target.x(), target.y(), target.width(), target.height())
        painter.drawText(
            rect.adjusted(0, 2, -4, 0),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop,
            "★",
        )
        painter.restore()

    def _days_text(self, days: int) -> str:
        """剩余天数文案（本地化；``<=0`` 表示今天清空）。"""
        if days <= 0:
            return self.tr("今天清除")
        template = self.tr("剩余 %1 天")
        return template.replace("%1", str(days))
