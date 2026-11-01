#
"""虚拟滚动控件的「带底部信息条」单元格委托。

``VirtualScrollWidget`` 用 ``CellDelegate`` + 全局索引作数据源（不是 Qt 的
item model），因此这里保留 getter 注入的形态；同时提供
:meth:`InfoCellDelegate.from_model`，直接把任意提供
:class:`~albuswall.ui.model.roles.ItemRole` 的 Qt 模型挂上来 ——
「虚拟滚动 + 模型」不再需要手工写两个 getter。

信息条两种互斥模式（:class:`InfoMode`）::

    FAVORITE  ┌───────────────────────────┐
              │          缩略图            │
              │░░ [★] ░░░░░░░░░░░░░░░░░░░│
              └───────────────────────────┘

    DAYS      ┌───────────────────────────┐
              │          缩略图            │
              │░░ 剩余 3 天 ░░░░░░░░░░░░░░│
              └───────────────────────────┘

数据缺失（``None``）时按「未知」处理，回退到默认文本，不会把未知当成
「未收藏」；缩略图缺失时默认连阴影条一起不画，避免占位格挂一条黑带。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from enum import Enum, auto
from typing import Any

from PySide6.QtCore import QCoreApplication, QRect, QRectF, Qt
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

from ..model.roles import ItemRole
from ..widgets.virtual_scroll.delegate import CellDelegate, CellOption

__all__ = ["InfoCellDelegate", "InfoMode"]

_logger = logging.getLogger(__name__)

#: 翻译上下文（委托不是 widget，用模块级 ``QCoreApplication.translate``）。
_TR_CONTEXT = "InfoCellDelegate"

IconLike = QIcon | QPixmap | None


def _tr(text: str) -> str:
    """本模块的翻译入口（信息条上的倒计时文案）。"""
    return QCoreApplication.translate(_TR_CONTEXT, text)


class InfoMode(Enum):
    """信息条的显示模式（互斥）。"""

    NONE = auto()
    FAVORITE = auto()
    DAYS = auto()


class InfoCellDelegate(CellDelegate):
    """缩略图 + 底部信息条。

    Args:
        pixmap_getter: 接收全局索引，返回 ``QPixmap`` 或 ``None``。
        mode: 信息条模式，见 :class:`InfoMode`。
        favorite_getter: 收藏状态源（返回 ``bool`` / ``None``）。
        days_getter: 回收站剩余天数源（返回 ``int`` / ``None``）。
        days_formatter: 天数 → 文本；``None`` 时用内置本地化规则。
        placeholder_color: 无缩略图时的填充色；``None`` 表示不填充。
        border_radius: 单元格圆角半径（像素）。
        default_favorite_text: 收藏状态未知时的文本。
        default_days_text: 天数未知时的文本。
        favorite_text_true / favorite_text_false: 无图标时的降级文本。
        icon_size: 收藏图标边长。
        padding: 信息条内边距。
        overlay_ratio: 阴影条占单元格高度的比例（默认 0.25）。
        hide_info_without_pixmap: 缩略图缺失时是否连信息条一起不画。
    """

    def __init__(
            self,
            pixmap_getter: Callable[[int], QPixmap | None] | None = None,
            *,
            mode: InfoMode = InfoMode.FAVORITE,
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
    ) -> None:
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
        """
        def read(index: int, role: ItemRole) -> Any:
            qt_index = model.index(int(index), 0)
            return qt_index.data(role) if qt_index.isValid() else None

        return cls(
            lambda index: read(index, ItemRole.Pixmap),
            mode=mode,
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
        return self._mode is InfoMode.FAVORITE

    @show_favorite.setter
    def show_favorite(self, enabled: bool) -> None:
        self._mode = InfoMode.FAVORITE if enabled else InfoMode.NONE

    @property
    def show_days(self) -> bool:
        return self._mode is InfoMode.DAYS

    @show_days.setter
    def show_days(self, enabled: bool) -> None:
        self._mode = InfoMode.DAYS if enabled else InfoMode.NONE

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
    def paint(self, painter: QPainter, option: CellOption) -> None:
        rect = option.rect
        if rect.isEmpty():
            return

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

        has_thumb = self._paint_pixmap(painter, option)
        if has_thumb or not self._hide_info_without_pixmap:
            if self._mode is InfoMode.FAVORITE:
                self._paint_favorite(painter, option)
            elif self._mode is InfoMode.DAYS:
                self._paint_days(painter, option)

        painter.restore()

    # ------------------------------------------------------------------ #
    # 内部：缩略图
    # ------------------------------------------------------------------ #
    def _paint_pixmap(self, painter: QPainter, option: CellOption) -> bool:
        pixmap = None
        if self._get_pixmap is not None:
            try:
                pixmap = self._get_pixmap(option.index)
            except Exception:  # noqa: BLE001 —— paint 期间不允许异常外泄
                _logger.exception(
                    "pixmap_getter failed at index=%s", option.index)
        if pixmap is not None and not pixmap.isNull():
            painter.drawPixmap(option.rect, pixmap)
            return True
        if self._placeholder_color is not None:
            painter.fillRect(option.rect, self._placeholder_color)
        return False

    # ------------------------------------------------------------------ #
    # 内部：两种模式
    # ------------------------------------------------------------------ #
    def _paint_favorite(self, painter: QPainter, option: CellOption) -> None:
        favorite = self._resolve_favorite(option.index)

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

        band = self._make_band(painter, option)
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

    def _paint_days(self, painter: QPainter, option: CellOption) -> None:
        days = self._resolve_days(option.index)
        text = (
            self._default_days_text if days is None else self._format_days(days)
        )
        if not text:
            return

        band = self._make_band(painter, option)
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
    def _make_band(
            self, painter: QPainter, option: CellOption) -> QRect | None:
        """准备字体与画笔、画出阴影条，返回条带矩形。"""
        rect = option.rect

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
    def _resolve_favorite(self, index: int) -> bool | None:
        if self._favorite_getter is None:
            return None
        try:
            value = self._favorite_getter(index)
        except Exception:  # noqa: BLE001
            _logger.exception("favorite_getter failed at index=%s", index)
            return None
        return None if value is None else bool(value)

    def _resolve_days(self, index: int) -> int | None:
        if self._days_getter is None:
            return None
        try:
            value = self._days_getter(index)
        except Exception:  # noqa: BLE001
            _logger.exception("days_getter failed at index=%s", index)
            return None
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _format_days(self, days: int) -> str:
        if self._days_formatter is not None:
            return self._days_formatter(days)
        if days > 0:
            return _tr("剩余 %1 天").replace("%1", str(days))
        if days == 0:
            return _tr("今天删除")
        return _tr("已过期")


def _clamp(ratio: float) -> float:
    return min(max(float(ratio), 0.0), 1.0)
