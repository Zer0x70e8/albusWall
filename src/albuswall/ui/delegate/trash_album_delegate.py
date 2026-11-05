#
"""回收站相册委托：**单行**排版 —— 左边图标 / 中间弹簧 / 右边名称。

相册页底部的 ``Album.trash_view`` 只放虚拟 Trash 相册（通常一行），
拿正方形的封面网格（:class:`~albuswall.ui.delegate.AlbumCoverDelegate`）
去画会白白占掉一整个格子，所以这里换成「一行一条」的列表排版::

    ┌──────────────────────────────────────────────┐
    │  🗑️                    ← 弹簧 →       Trash  │
    └──────────────────────────────────────────────┘

* **左**：``ItemRole.Icon``（当前是 emoji 文本，见
  :data:`~albuswall.ui.model.AlbumModel` 的 ``icon`` 字段）；
* **中**：弹簧（图标与名称之间全部留白，随行宽伸缩）；
* **右**：``ItemRole.Title``（缺省回落到 ``DisplayRole``），右对齐 + 省略号。

数据只从 role 取，因此同一份委托可以吃任何实现了
``ItemRole.Icon`` / ``ItemRole.Title`` 的模型。选中 / hover 由委托自绘
圆角底色（QSS 里 ``#AlbumTrashView::item`` 的默认高亮已被压平）。
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QRect, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter
from PySide6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem

from ..model.roles import ItemRole

__all__ = ["TrashAlbumDelegate"]

#: 行高默认值（像素）：一行回收站条目，比分方正格子矮得多。
DEFAULT_ROW_HEIGHT = 40
#: 行内左右内边距。
DEFAULT_PADDING = 10
#: 图标与名称之间的最小间隙（弹簧被压到最小时仍留出的空）。
DEFAULT_GAP = 12


class TrashAlbumDelegate(QStyledItemDelegate):
    """回收站相册的单行委托（左图标 + 弹簧 + 右名称）。"""

    #: 名称颜色
    TITLE_COLOR = QColor(240, 240, 240, 240)
    #: 选中 / hover 的自绘底色（QSS 的 item 高亮已被压平，见 main_window.qss）
    SELECTED_BG = QColor(0, 122, 255, 72)
    HOVER_BG = QColor(255, 255, 255, 20)
    #: 选中时贴左边画一道强调条
    SELECTED_ACCENT = QColor(0, 122, 255, 235)

    def __init__(
            self,
            parent: Any = None,
            *,
            row_height: int = DEFAULT_ROW_HEIGHT,
            padding: int = DEFAULT_PADDING,
            gap: int = DEFAULT_GAP,
            radius: int = 10,
            icon_role: int = int(ItemRole.Icon),
            title_role: int = int(ItemRole.Title),
            title_color: QColor | None = None,
    ) -> None:
        super().__init__(parent)
        self._row_height = max(1, int(row_height))
        self._padding = max(0, int(padding))
        self._gap = max(0, int(gap))
        self._radius = max(0, int(radius))
        self._icon_role = int(icon_role)
        self._title_role = int(title_role)
        self._title_color = QColor(title_color or self.TITLE_COLOR)
        # 字体按 option.font 动态重建（跟随 QSS 的字号）
        self._font_key: tuple[Any, ...] | None = None
        self._icon_font: QFont | None = None
        self._title_font: QFont | None = None
        self._icon_fm: QFontMetrics | None = None
        self._title_fm: QFontMetrics | None = None

    # ------------------------------------------------------------------ #
    # 外观配置
    # ------------------------------------------------------------------ #
    def set_row_height(self, height: int) -> None:
        """行高（像素）；视图据此算固定高度。"""
        self._row_height = max(1, int(height))
        self._invalidate()

    def row_height(self) -> int:
        return self._row_height

    def set_padding(self, padding: int) -> None:
        self._padding = max(0, int(padding))
        self._invalidate()

    def padding(self) -> int:
        return self._padding

    def set_gap(self, gap: int) -> None:
        """图标与名称之间的最小留白（弹簧最窄时的宽度）。"""
        self._gap = max(0, int(gap))
        self._invalidate()

    def gap(self) -> int:
        return self._gap

    def set_border_radius(self, radius: int) -> None:
        self._radius = max(0, int(radius))
        self._invalidate()

    def border_radius(self) -> int:
        return self._radius

    def set_title_color(self, color: QColor) -> None:
        self._title_color = QColor(color)
        self._invalidate()

    def title_color(self) -> QColor:
        return QColor(self._title_color)

    # ------------------------------------------------------------------ #
    # QStyledItemDelegate
    # ------------------------------------------------------------------ #
    def sizeHint(self, option: QStyleOptionViewItem, index) -> QSize:  # noqa: N802
        return QSize(0, self._row_height)

    def paint(self, painter: QPainter, option, index) -> None:
        painter.save()
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

            rect = QRect(option.rect)
            if rect.isEmpty():
                return

            selected = bool(option.state & QStyle.StateFlag.State_Selected)
            hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
            if selected or hovered:
                self._draw_background(painter, rect, selected)

            inner = rect.adjusted(
                self._padding, 0, -self._padding, 0)
            if inner.width() <= 0:
                return

            self._ensure_fonts(option.font)
            icon_text = self._text_data(index, self._icon_role)
            title = self._text_data(index, self._title_role)

            # ---- 左：图标 ----
            left = inner.left()
            if icon_text and self._icon_fm is not None:
                icon_width = self._icon_fm.horizontalAdvance(icon_text)
                icon_rect = QRect(
                    left,
                    inner.top(),
                    min(icon_width, inner.width()),
                    inner.height(),
                )
                painter.setFont(self._icon_font)
                painter.setPen(self._title_color)
                painter.drawText(
                    icon_rect,
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    icon_text,
                )
                left = icon_rect.right() + self._gap

            # ---- 右：名称（弹簧就是 left 与 right 之间的留白） ----
            right = inner.right()
            if title and self._title_fm is not None and right > left:
                title_width = right - left
                title_rect = QRect(left, inner.top(), title_width, inner.height())
                painter.setFont(self._title_font)
                painter.setPen(self._title_color)
                painter.drawText(
                    title_rect,
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                    self._title_fm.elidedText(
                        title, Qt.TextElideMode.ElideRight, title_width),
                )
        finally:
            painter.restore()

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _draw_background(
            self,
            painter: QPainter,
            rect: QRect,
            selected: bool,
    ) -> None:
        """选中的圆角底色 + 左侧强调条；hover 只画淡淡的底色。"""
        body = QRectF(rect).adjusted(1.0, 1.0, -1.0, -1.0)
        if body.width() <= 0 or body.height() <= 0:
            return
        radius = float(min(self._radius, int(body.height() / 2)))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self.SELECTED_BG if selected else self.HOVER_BG)
        painter.drawRoundedRect(body, radius, radius)

        if selected:
            accent_width = 3.0
            accent = QRectF(
                body.left(),
                body.top() + 4.0,
                accent_width,
                max(0.0, body.height() - 8.0),
            )
            if accent.height() > 0:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(self.SELECTED_ACCENT)
                painter.drawRoundedRect(
                    accent, accent_width / 2, accent_width / 2)

    def _ensure_fonts(self, base: QFont) -> None:
        """跟随 ``option.font`` 重建图标 / 名称字体（只做一次缓存）。"""
        key = (base.family(), base.pointSizeF(), base.weight(), base.italic())
        if key == self._font_key:
            return
        point = base.pointSizeF() if base.pointSizeF() > 0 else 10.0

        icon = QFont(base)
        icon.setPointSizeF(point + 2.0)
        self._icon_font = icon
        self._icon_fm = QFontMetrics(icon)

        title = QFont(base)
        title.setPointSizeF(point + 0.5)
        title.setBold(True)
        self._title_font = title
        self._title_fm = QFontMetrics(title)

        self._font_key = key

    def _text_data(self, index, role: int) -> str:
        """按 role 取文本；``Title`` 没给时回落到 ``DisplayRole``。"""
        value = index.data(role)
        if value in (None, "") and role == int(ItemRole.Title):
            value = index.data(Qt.ItemDataRole.DisplayRole)
        return "" if value is None else str(value)

    def _invalidate(self) -> None:
        parent = self.parent()
        if parent is not None and hasattr(parent, "update"):
            parent.update()
