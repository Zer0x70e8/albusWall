#
"""网格单元格的「缩略图 + 底部信息条」委托。

内容网格（``Content`` → :class:`~albuswall.ui.widgets.tile_view.TileView`）是
标准的 ``QListView``，所以这里的委托是普通的 ``QStyledItemDelegate``：
格子几何来自 ``option.rect``（视图已强制成 ``cell_size × cell_size``），数据
按 :class:`~albuswall.ui.model.roles.ItemRole` 契约直接问模型 —— 不再需要
外部注入一堆 getter。getter 仍然保留（``pixmap_getter`` / ``favorite_getter``
/ ``days_getter``），用于不走模型的纯索引场景。

信息条三种模式（:class:`InfoMode`）::

    FAVORITE  ┌───────────────────────────┐
              │          缩略图            │
              │░░ [♥] ░░░░░░░░░░░░░░░░░░░│
              └───────────────────────────┘

    DAYS      ┌───────────────────────────┐
              │          缩略图            │
              │░░ 剩余 3 天 ░░░░░░░░░░░░░░│
              └───────────────────────────┘

    AUTO      ┌───────────────────────────┐   逐格判断（内容网格用这个）：
              │  普通资产 ░ ×  ♥ 角标      │   ``ItemRole.DaysLeft`` 有值
              │  回收站资产 ░ 剩余 3 天    │   → 画剩余天数，否则画收藏状态
              └───────────────────────────┘

``FAVORITE`` / ``DAYS`` 是**整块**互斥的（一个委托只显示一种）；内容网格
同时装着「普通资产」和（回收站视图里的）「已删资产」，需要按格判断，所以
用 ``AUTO``：被回收的那格显示剩余天数，其余格显示收藏状态。判断逻辑见
:meth:`InfoCellDelegate.mode_for`。

数据缺失（``None``）时按「未知」处理，回退到默认文本，不会把未知当成
「未收藏」；缩略图缺失时默认连阴影条一起不画，避免占位格挂一条黑带。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from enum import Enum, auto
from typing import Any

from PySide6.QtCore import QCoreApplication, QModelIndex, QRect, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QIcon,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPixmap,
)
from PySide6.QtWidgets import QStyle, QStyledItemDelegate

from ..model.roles import ItemRole

__all__ = ["InfoCellDelegate", "InfoMode"]

_logger = logging.getLogger(__name__)

#: 翻译上下文（委托不是 widget，用模块级 ``QCoreApplication.translate``）。
_TR_CONTEXT = "InfoCellDelegate"

IconLike = QIcon | QPixmap | None


def _tr(text: str) -> str:
    """本模块的翻译入口（信息条上的倒计时文案）。"""
    return QCoreApplication.translate(_TR_CONTEXT, text)


class InfoMode(Enum):
    """信息条的显示模式。

    ``NONE`` / ``FAVORITE`` / ``DAYS`` 是整块互斥的；``AUTO`` 按格判断
    （回收站里的格子显示剩余天数，其余的显示收藏状态）。
    """

    NONE = auto()
    FAVORITE = auto()
    DAYS = auto()
    AUTO = auto()


class InfoCellDelegate(QStyledItemDelegate):
    """缩略图 + 底部信息条（``QListView`` 委托）。

    Args:
        parent: ``QObject`` 父对象（交给 ``QStyledItemDelegate``）。
        mode: 信息条模式，见 :class:`InfoMode`（``AUTO`` = 逐格判断）。
        pixmap_getter: 可选；接收全局索引返回 ``QPixmap``（不传就读
            ``ItemRole.Pixmap`` / ``DecorationRole``）。
        favorite_getter: 可选；收藏状态源（``bool`` / ``None``）。
        days_getter: 可选；回收站剩余天数源（``int`` / ``None``）。
        days_formatter: 天数 → 文本；``None`` 时用内置本地化规则。
        placeholder_color: 无缩略图时的填充色；``None`` 表示不填充。
        border_radius: 单元格圆角半径（像素）。
        default_favorite_text: 收藏状态未知时的文本。
        default_days_text: 天数未知时的文本。
        favorite_text_true / favorite_text_false: 无图标时的降级文本。
        text_color / overlay_color / font: 信息条样式。
        icon_size: 收藏图标边长。
        padding: 信息条内边距。
        overlay_ratio: 阴影条占单元格高度的比例（默认 0.25）。
        hide_info_without_pixmap: 缩略图缺失时是否连信息条一起不画。
        show_focus_ring: 当前格（键盘焦点）是否画一圈高亮描边。
    """

    def __init__(
            self,
            parent: Any = None,
            *,
            mode: InfoMode = InfoMode.FAVORITE,
            pixmap_getter: Callable[[int], QPixmap | None] | None = None,
            favorite_getter: Callable[[int], bool | None] | None = None,
            days_getter: Callable[[int], int | None] | None = None,
            days_formatter: Callable[[int], str] | None = None,
            placeholder_color: QColor | None = None,
            border_radius: int = 0,
            default_favorite_text: str = "",
            default_days_text: str = "",
            favorite_text_true: str = "★",
            favorite_text_false: str = "",
            text_color: QColor | None = None,
            overlay_color: QColor | None = None,
            font: QFont | None = None,
            icon_size: int = 16,
            padding: int = 6,
            overlay_ratio: float = 0.25,
            hide_info_without_pixmap: bool = True,
            show_focus_ring: bool = True,
    ) -> None:
        super().__init__(parent)
        self._get_pixmap = pixmap_getter
        self._mode = mode
        self._favorite_getter = favorite_getter
        self._days_getter = days_getter
        self._days_formatter = days_formatter

        self._placeholder_color = placeholder_color
        self._border_radius = max(0, int(border_radius))

        self._default_favorite_text = default_favorite_text
        self._default_days_text = default_days_text
        self._favorite_text_true = favorite_text_true
        self._favorite_text_false = favorite_text_false

        self._text_color = text_color
        self._overlay_color = overlay_color
        self._font = font

        self._icon_size = max(0, int(icon_size))
        self._padding = max(0, int(padding))
        self._overlay_ratio = _clamp(overlay_ratio)

        #: 收藏图标：``{True: 已收藏图标, False: 未收藏图标}``
        self._icons: dict[bool, QIcon | None] = {True: None, False: None}
        self._hide_info_without_pixmap = bool(hide_info_without_pixmap)
        self._show_focus_ring = bool(show_focus_ring)

    # ------------------------------------------------------------------ #
    # 直接接模型
    # ------------------------------------------------------------------ #
    @classmethod
    def from_model(
            cls,
            model: Any,
            *,
            mode: InfoMode = InfoMode.FAVORITE,
            **options: Any,
    ) -> "InfoCellDelegate":
        """用任意 Qt 模型（``ItemRole`` 契约）构造委托。

        模型只需按行号提供 ``ItemRole.Pixmap`` / ``ItemRole.Favorite`` /
        ``ItemRole.DaysLeft``；缺失的 role 会按「未知」处理。

        注：直接 ``InfoCellDelegate(mode=...)`` 也能用 —— ``paint`` 拿到的是
        ``QModelIndex``，本来就能读 role；``from_model`` 额外把同样的读取
        包成 getter，供 :meth:`mode_for` 这类「只有行号」的调用使用。
        """
        def read(index: int, role: ItemRole) -> Any:
            qt_index = model.index(int(index), 0)
            return qt_index.data(role) if qt_index.isValid() else None

        return cls(
            mode=mode,
            pixmap_getter=lambda index: read(index, ItemRole.Pixmap),
            favorite_getter=lambda index: read(index, ItemRole.Favorite),
            days_getter=lambda index: read(index, ItemRole.DaysLeft),
            **options,
        )

    # ------------------------------------------------------------------ #
    # 属性
    # ------------------------------------------------------------------ #
    @property
    def mode(self) -> InfoMode:
        return self._mode

    @mode.setter
    def mode(self, value: InfoMode) -> None:
        if not isinstance(value, InfoMode):
            raise TypeError(f"mode 必须是 InfoMode，收到 {type(value).__name__}")
        self._mode = value

    def set_mode(self, mode: InfoMode) -> None:
        self.mode = mode

    @property
    def show_favorite(self) -> bool:
        return self._mode in (InfoMode.FAVORITE, InfoMode.AUTO)

    @show_favorite.setter
    def show_favorite(self, enabled: bool) -> None:
        self._mode = InfoMode.FAVORITE if enabled else InfoMode.NONE

    @property
    def show_days(self) -> bool:
        return self._mode in (InfoMode.DAYS, InfoMode.AUTO)

    @show_days.setter
    def show_days(self, enabled: bool) -> None:
        self._mode = InfoMode.DAYS if enabled else InfoMode.NONE

    def mode_for(self, target: int | QModelIndex) -> InfoMode:
        """某一格实际该画什么（``AUTO`` 的逐格判断落在这里）。

        规则（互斥，按优先级）：

        1. ``ItemRole.DaysLeft`` 有值 → :attr:`InfoMode.DAYS`
           —— 被回收的资产显示回收站剩余天数；
        2. 否则 ``ItemRole.Favorite`` 为 ``True`` → :attr:`InfoMode.FAVORITE`
           —— 普通资产显示收藏状态；
        3. 其余（未收藏 / 状态未知）→ :attr:`InfoMode.NONE`，不占画面。

        非 ``AUTO`` 模式直接返回配置的模式（整块互斥）。

        Args:
            target: 行号，或该格的 ``QModelIndex``（给了索引就能直接读
                role；只给行号时依赖注入的 getter，见 :meth:`from_model`）。
        """
        mode = self._mode
        if mode is not InfoMode.AUTO:
            return mode
        row, index = _target_parts(target)
        if self._resolve_days(row, index) is not None:
            return InfoMode.DAYS
        if self._resolve_favorite(row, index) is True:
            return InfoMode.FAVORITE
        return InfoMode.NONE

    def set_icon(self, icon: IconLike, *, checked: bool = True) -> None:
        """设置收藏状态图标（``True`` = 已收藏，``False`` = 未收藏）。"""
        if isinstance(icon, QPixmap):
            icon = None if icon.isNull() else QIcon(icon)
        if icon is not None and icon.isNull():
            icon = None
        self._icons[bool(checked)] = icon

    def icon(self, *, checked: bool = True) -> QIcon | None:
        return self._icons.get(bool(checked))

    def set_border_radius(self, radius: int) -> None:
        self._border_radius = max(0, int(radius))

    def set_placeholder_color(self, color: QColor | None) -> None:
        self._placeholder_color = color

    # ------------------------------------------------------------------ #
    # paint
    # ------------------------------------------------------------------ #
    def paint(self, painter: QPainter, option, index: QModelIndex) -> None:
        rect: QRect = option.rect
        if rect.isEmpty():
            return

        row = index.row() if isinstance(index, QModelIndex) else -1
        model_index = index if isinstance(index, QModelIndex) else None

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        if self._border_radius > 0:
            path = QPainterPath()
            path.addRoundedRect(
                QRectF(rect),
                float(self._border_radius),
                float(self._border_radius),
            )
            painter.setClipPath(path, Qt.ClipOperation.IntersectClip)

        has_thumb = self._paint_pixmap(painter, rect, row, model_index)
        if has_thumb or not self._hide_info_without_pixmap:
            mode = self.mode_for(model_index if model_index is not None else row)
            if mode is InfoMode.FAVORITE:
                self._paint_favorite(painter, rect, row, model_index)
            elif mode is InfoMode.DAYS:
                self._paint_days(painter, rect, row, model_index)

        self._paint_focus_ring(painter, rect, option)
        painter.restore()

    # ------------------------------------------------------------------ #
    # 内部：焦点描边
    # ------------------------------------------------------------------ #
    def _paint_focus_ring(self, painter: QPainter, rect: QRect, option) -> None:
        """当前格（键盘光标）在视图持有焦点时画一圈描边。"""
        if not self._show_focus_ring:
            return
        if not (option.state & QStyle.StateFlag.State_HasFocus):
            return
        pen_color = QColor(0, 122, 255, 235)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(pen_color)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(rect.adjusted(2, 2, -2, -2), 6, 6)

    # ------------------------------------------------------------------ #
    # 内部：缩略图
    # ------------------------------------------------------------------ #
    def _paint_pixmap(
            self,
            painter: QPainter,
            rect: QRect,
            row: int,
            index: QModelIndex | None,
    ) -> bool:
        pixmap = None
        if self._get_pixmap is not None:
            try:
                pixmap = self._get_pixmap(row)
            except Exception:  # noqa: BLE001 —— paint 期间不允许异常外泄
                _logger.exception("pixmap_getter failed at index=%s", row)
        elif index is not None:
            pixmap = index.data(ItemRole.Pixmap)
            if not isinstance(pixmap, QPixmap):
                pixmap = index.data(Qt.ItemDataRole.DecorationRole)
        if isinstance(pixmap, QPixmap) and not pixmap.isNull():
            painter.drawPixmap(rect, pixmap)
            return True
        if self._placeholder_color is not None:
            painter.fillRect(rect, self._placeholder_color)
        return False

    # ------------------------------------------------------------------ #
    # 内部：两种模式
    # ------------------------------------------------------------------ #
    def _paint_favorite(
            self,
            painter: QPainter,
            rect: QRect,
            row: int,
            index: QModelIndex | None,
    ) -> None:
        favorite = self._resolve_favorite(row, index)

        icon: QIcon | None = None
        text = ""
        if favorite is None:
            text = self._default_favorite_text
        else:
            icon = self._icons.get(bool(favorite))
            if icon is None:
                text = (
                    self._favorite_text_true if favorite
                    else self._favorite_text_false
                )

        if icon is None and not text:
            return

        band = self._make_band(painter, rect)
        if band is None:
            return

        metrics = QFontMetrics(painter.font())
        center_y = band.top() + band.height() // 2
        x = band.left() + self._padding

        if icon is not None:
            size = min(self._icon_size, band.height())
            if size > 0:
                icon.paint(
                    painter,
                    QRect(x, center_y - size // 2, size, size),
                    Qt.AlignmentFlag.AlignCenter,
                )
        else:
            self._draw_text(
                painter, metrics, text, x, center_y, band, band.right())

    def _paint_days(
            self,
            painter: QPainter,
            rect: QRect,
            row: int,
            index: QModelIndex | None,
    ) -> None:
        days = self._resolve_days(row, index)
        text = (
            self._default_days_text if days is None else self.format_days(days)
        )
        if not text:
            return

        band = self._make_band(painter, rect)
        if band is None:
            return

        metrics = QFontMetrics(painter.font())
        center_y = band.top() + band.height() // 2
        self._draw_text(
            painter, metrics, text,
            band.left() + self._padding, center_y, band, band.right(),
        )

    # ------------------------------------------------------------------ #
    # 内部：阴影条 / 文本
    # ------------------------------------------------------------------ #
    def _make_band(self, painter: QPainter, rect: QRect) -> QRect | None:
        """准备字体与画笔、画出阴影条，返回条带矩形。"""
        font = self._resolve_font(painter)
        painter.setFont(font)
        metrics = QFontMetrics(font)

        band_height = int(round(rect.height() * self._overlay_ratio))
        band_height = max(band_height, metrics.height() + 2 * self._padding)
        band_height = min(band_height, rect.height())
        if band_height <= 0:
            return None

        band = QRect(
            rect.left(), rect.bottom() - band_height + 1,
            rect.width(), band_height,
        )

        overlay = self._overlay_color or QColor(0, 0, 0, 160)
        gradient = QLinearGradient(
            float(band.left()), float(band.top()),
            float(band.left()), float(band.bottom() + 1),
        )
        transparent = QColor(overlay)
        transparent.setAlpha(0)
        gradient.setColorAt(0.0, transparent)
        gradient.setColorAt(1.0, overlay)

        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(band, gradient)
        painter.setPen(self._resolve_text_color())
        return band

    @staticmethod
    def _draw_text(
            painter: QPainter,
            metrics: QFontMetrics,
            text: str,
            x: int,
            center_y: int,
            band: QRect,
            right: int,
    ) -> int:
        """在条带内画单行左对齐文本，返回下一段内容应放置的 x（放不下返回 -1）。"""
        available = right - x + 1
        if available <= 0:
            return -1

        elided = metrics.elidedText(
            text, Qt.TextElideMode.ElideRight, available)
        width = metrics.horizontalAdvance(elided)
        if width <= 0:
            return x

        painter.drawText(
            QRect(x, band.top(), width, band.height()),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            elided,
        )
        return x + width

    # ------------------------------------------------------------------ #
    # 内部：样式
    # ------------------------------------------------------------------ #
    def _resolve_font(self, painter: QPainter) -> QFont:
        return (
            QFont(self._font) if self._font is not None
            else QFont(painter.font())
        )

    def _resolve_text_color(self) -> QColor:
        return (
            QColor(self._text_color) if self._text_color is not None
            else QColor(255, 255, 255)
        )

    # ------------------------------------------------------------------ #
    # 内部：数据取值
    # ------------------------------------------------------------------ #
    def _resolve_favorite(
            self, row: int, index: QModelIndex | None = None,
    ) -> bool | None:
        if self._favorite_getter is None:
            if index is None:
                return None
            value = index.data(ItemRole.Favorite)
            return None if value is None else bool(value)
        try:
            value = self._favorite_getter(row)
        except Exception:  # noqa: BLE001
            _logger.exception("favorite_getter failed at index=%s", row)
            return None
        return None if value is None else bool(value)

    def _resolve_days(
            self, row: int, index: QModelIndex | None = None,
    ) -> int | None:
        if self._days_getter is None:
            if index is None:
                return None
            value = index.data(ItemRole.DaysLeft)
        else:
            try:
                value = self._days_getter(row)
            except Exception:  # noqa: BLE001
                _logger.exception("days_getter failed at index=%s", row)
                return None
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def format_days(self, days: int) -> str:
        """剩余天数 → 信息条文本（``days_formatter`` 优先，其次内置规则）。

        内置规则的源文是**英文**（``%1 days left`` / ``Deleted today`` /
        ``Expired``），由 ``InfoCellDelegate`` 上下文的翻译表翻成中文。
        """
        if self._days_formatter is not None:
            return self._days_formatter(days)
        if days > 0:
            return _tr("%1 days left").replace("%1", str(days))
        if days == 0:
            return _tr("Deleted today")
        return _tr("Expired")


def _target_parts(target: int | QModelIndex) -> tuple[int, QModelIndex | None]:
    """把「行号 / 索引」拆成 (行号, 索引)；索引无效时索引按 ``None``。"""
    if isinstance(target, QModelIndex):
        return (target.row() if target.isValid() else -1,
                target if target.isValid() else None)
    return int(target), None


def _clamp(ratio: float) -> float:
    return min(max(float(ratio), 0.0), 1.0)
