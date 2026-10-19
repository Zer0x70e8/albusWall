#
"""带底部信息条的单元格委托（两种互斥显示模式）。

在图片底部约 1/4 区域叠加一层渐变阴影，并以左对齐方式显示信息：

    Mode.FAVORITE:  ┌───────────────────────────┐
                    │          缩略图            │
                    │░░░ [★] ░░░░░░░░░░░░░░░░░░│  ← 收藏图标
                    └───────────────────────────┘

    Mode.DAYS:      ┌───────────────────────────┐
                    │          缩略图            │
                    │░░░ 剩余 3 天 ░░░░░░░░░░░░░│  ← 删除倒计时
                    └───────────────────────────┘

    Mode.NONE:      不绘制信息条（纯缩略图）。

两种模式**互斥**，由 :attr:`mode` 切换；同一时刻只显示一种。
数据通过 getter 注入，未注入或 getter 返回 ``None`` 时显示默认文本。

样式：
    · 文字颜色 / 字体 / 图标尺寸 / 阴影颜色 / 阴影高度比例均可通过属性调整。
    · ``text_color`` / ``font`` 为 ``None`` 时跟随 ``QPainter`` 当前设置
      （即宿主 widget 的字体），因此样式表中设置的 font 会被继承。
"""

from __future__ import annotations

import logging
from enum import Enum, auto
from typing import Callable, Optional, Union

from PySide6.QtCore import QRect, QRectF, Qt
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

from ..widgets.virtual_scroll.delegate import CellDelegate, CellOption

__all__ = ["InfoCellDelegate", "InfoMode"]

_logger = logging.getLogger(__name__)

IconLike = Union[QIcon, QPixmap, None]


class InfoMode(Enum):
    """信息条的显示模式（互斥）。"""

    NONE = auto()  #: 不显示信息条
    FAVORITE = auto()  #: 显示收藏状态（图标优先，无图标则文本）
    DAYS = auto()  #: 显示删除剩余天数


class InfoCellDelegate(CellDelegate):
    """带底部信息条的单元格委托。

    Args:
        pixmap_getter:
            接收全局索引，返回 ``QPixmap`` 或 ``None``。
        mode:
            当前显示模式，见 :class:`InfoMode`。默认 ``FAVORITE``。
        favorite_getter:
            ``Mode.FAVORITE`` 下的数据源：接收索引返回
            ``True`` / ``False`` / ``None``。``None`` 视为未知，回退默认文本。
            未注入时所有单元格都显示默认收藏文本。
        days_getter:
            ``Mode.DAYS`` 下的数据源：接收索引返回剩余天数 ``int`` 或 ``None``。
            ``None`` 视为未知，回退默认天数文本。未注入时都显示默认天数文本。
        days_formatter:
            天数 → 文本；``None`` 时用内置规则。
        placeholder_color:
            缩略图缺失时的填充色；``None`` 表示不填充。
        border_radius:
            单元格圆角半径（像素），0 表示直角。
        default_favorite_text:
            ``favorite_getter`` 未提供或返回 ``None`` 时显示的文本。
        default_days_text:
            ``days_getter`` 未提供或返回 ``None`` 时显示的文本。
        favorite_text_true / favorite_text_false:
            有 getter 但未给对应状态设置图标时的降级文本。
            默认 ``True`` 用 ``"★"``，``False`` 用空串（不显示）。
        text_color:
            信息条文字颜色；``None`` 时使用白色。
        overlay_color:
            阴影渐变底部颜色；``None`` 时使用 ``QColor(0, 0, 0, 160)``。
        font:
            信息条字体；``None`` 时沿用 painter 当前字体。
        icon_size:
            收藏图标边长（像素）。
        padding:
            信息条内边距（同时作为图标与文本间距基数）。
        overlay_ratio:
            阴影条占单元格高度的比例，默认 0.25（即底部 1/4）。
    """

    def __init__(
            self,
            pixmap_getter: Callable[[int], Optional[QPixmap]],
            *,
            mode: InfoMode = InfoMode.FAVORITE,
            favorite_getter: Optional[Callable[[int], Optional[bool]]] = None,
            days_getter: Optional[Callable[[int], Optional[int]]] = None,
            days_formatter: Optional[Callable[[int], str]] = None,
            placeholder_color: Optional[QColor] = None,
            border_radius: int = 0,
            default_favorite_text: str = "empty",
            default_days_text: str = "empty",
            favorite_text_true: str = "★",
            favorite_text_false: str = "",
            text_color: Optional[QColor] = None,
            overlay_color: Optional[QColor] = None,
            font: Optional[QFont] = None,
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
        self._overlay_ratio = min(max(float(overlay_ratio), 0.0), 1.0)


        # 收藏图标：{True: 已收藏图标, False: 未收藏图标}
        self._icons: dict[bool, Optional[QIcon]] = {True: None, False: None}
        self._hide_info_without_pixmap = bool(hide_info_without_pixmap)

    @property
    def hide_info_without_pixmap(self) -> bool:
        """缩略图缺失时是否隐藏信息条（含阴影）。默认 True。"""
        return self._hide_info_without_pixmap

    @hide_info_without_pixmap.setter
    def hide_info_without_pixmap(self, value: bool) -> None:
        self._hide_info_without_pixmap = bool(value)

    # ------------------------------------------------------------------ #
    # 模式
    # ------------------------------------------------------------------ #
    @property
    def mode(self) -> InfoMode:
        """当前显示模式（互斥）。"""
        return self._mode

    @mode.setter
    def mode(self, value: InfoMode) -> None:
        if not isinstance(value, InfoMode):
            raise TypeError(f"mode 必须是 InfoMode，收到 {type(value).__name__}")
        self._mode = value

    def set_mode(self, mode: InfoMode) -> None:
        """等价于 ``delegate.mode = mode``；便于在 Qt 属性系统外调用。"""
        self.mode = mode

    # ------------------------------------------------------------------ #
    # 图标接口
    # ------------------------------------------------------------------ #
    def set_icon(self, icon: IconLike, *, checked: bool = True) -> None:
        """设置收藏状态图标。

        Args:
            icon: ``QIcon`` 或 ``QPixmap``；传 ``None`` 清除该状态图标。
            checked: ``True`` 设置"已收藏"图标；``False`` 设置"未收藏"图标。
        """
        if isinstance(icon, QPixmap):
            icon = None if icon.isNull() else QIcon(icon)
        if icon is not None and icon.isNull():
            icon = None
        self._icons[bool(checked)] = icon

    def icon(self, *, checked: bool = True) -> Optional[QIcon]:
        """取回已设置的收藏图标（``checked`` 同上）。"""
        return self._icons.get(bool(checked))

    # ------------------------------------------------------------------ #
    # 属性
    # ------------------------------------------------------------------ #
    @property
    def text_color(self) -> Optional[QColor]:
        return self._text_color

    @text_color.setter
    def text_color(self, color: Optional[QColor]) -> None:
        self._text_color = color

    @property
    def overlay_color(self) -> Optional[QColor]:
        return self._overlay_color

    @overlay_color.setter
    def overlay_color(self, color: Optional[QColor]) -> None:
        self._overlay_color = color

    @property
    def font(self) -> Optional[QFont]:
        return self._font

    @font.setter
    def font(self, font: Optional[QFont]) -> None:
        self._font = font

    @property
    def icon_size(self) -> int:
        return self._icon_size

    @icon_size.setter
    def icon_size(self, value: int) -> None:
        self._icon_size = max(0, int(value))

    @property
    def padding(self) -> int:
        return self._padding

    @padding.setter
    def padding(self, value: int) -> None:
        self._padding = max(0, int(value))

    @property
    def overlay_ratio(self) -> float:
        return self._overlay_ratio

    @overlay_ratio.setter
    def overlay_ratio(self, value: float) -> None:
        self._overlay_ratio = min(max(float(value), 0.0), 1.0)

    @property
    def border_radius(self) -> int:
        return self._border_radius

    @border_radius.setter
    def border_radius(self, value: int) -> None:
        self._border_radius = max(0, int(value))

    # 便捷属性：模式切换（enum 名字直接暴露，便于 qss/qproperty 场景）
    @property
    def show_favorite(self) -> bool:
        return self._mode is InfoMode.FAVORITE

    @show_favorite.setter
    def show_favorite(self, on: bool) -> None:
        self._mode = InfoMode.FAVORITE if on else InfoMode.NONE

    @property
    def show_days(self) -> bool:
        return self._mode is InfoMode.DAYS

    @show_days.setter
    def show_days(self, on: bool) -> None:
        self._mode = InfoMode.DAYS if on else InfoMode.NONE

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

        # 只有真画了缩略图，才叠信息条 —— 占位格不再出现阴影 / 文本
        has_thumb = self._paint_pixmap(painter, option)
        if has_thumb or not self._hide_info_without_pixmap:
            if self._mode is InfoMode.FAVORITE:
                self._paint_favorite(painter, option)
            elif self._mode is InfoMode.DAYS:
                self._paint_days(painter, option)
        # InfoMode.NONE → 不绘制信息条

        painter.restore()

    # ------------------------------------------------------------------ #
    # 内部：缩略图
    # ------------------------------------------------------------------ #
    def _paint_pixmap(self, painter: QPainter, option: CellOption) -> bool:
        """绘制缩略图；返回是否画出了真实缩略图（占位填充不计）。"""
        pixmap = self._get_pixmap(option.index) if self._get_pixmap else None
        if pixmap is not None and not pixmap.isNull():
            painter.drawPixmap(option.rect, pixmap)
            return True
        if self._placeholder_color is not None:
            painter.fillRect(option.rect, self._placeholder_color)
        return False

    # ------------------------------------------------------------------ #
    # 内部：模式 — 收藏
    # ------------------------------------------------------------------ #
    def _paint_favorite(self, painter: QPainter, option: CellOption) -> None:
        fav = self._resolve_favorite(option.index)

        icon: Optional[QIcon] = None
        text = ""
        if fav is None:
            text = self._default_favorite_text
        else:
            icon = self._icons.get(bool(fav))
            if icon is None:
                text = (
                    self._favorite_text_true if fav
                    else self._favorite_text_false
                )

        if icon is None and not text:
            return  # 该状态下无内容可画 → 不画阴影条

        band = self._make_band(painter, option)
        if band is None:
            return

        fm = QFontMetrics(painter.font())
        cy = band.top() + band.height() // 2
        x = band.left() + self._padding
        right = band.right()

        if icon is not None:
            size = min(self._icon_size, band.height())
            if size > 0:
                icon.paint(
                    painter,
                    QRect(x, cy - size // 2, size, size),
                    Qt.AlignmentFlag.AlignCenter,
                )
        else:
            self._draw_text(painter, fm, text, x, cy, band, right)

    # ------------------------------------------------------------------ #
    # 内部：模式 — 剩余天数
    # ------------------------------------------------------------------ #
    def _paint_days(self, painter: QPainter, option: CellOption) -> None:
        days = self._resolve_days(option.index)
        text = (
            self._default_days_text if days is None
            else self._format_days(days)
        )
        if not text:
            return

        band = self._make_band(painter, option)
        if band is None:
            return

        fm = QFontMetrics(painter.font())
        cy = band.top() + band.height() // 2
        x = band.left() + self._padding
        right = band.right()
        self._draw_text(painter, fm, text, x, cy, band, right)

    # ------------------------------------------------------------------ #
    # 内部：阴影条 & 文本
    # ------------------------------------------------------------------ #
    def _make_band(
            self, painter: QPainter, option: CellOption
    ) -> Optional[QRect]:
        """准备字体 / 画笔，画出阴影条，返回条带矩形；不可绘制时返回 ``None``。"""
        rect = option.rect

        font = self._resolve_font(painter)
        painter.setFont(font)
        fm = QFontMetrics(font)

        band_h = int(round(rect.height() * self._overlay_ratio))
        band_h = max(band_h, fm.height() + 2 * self._padding)
        band_h = min(band_h, rect.height())
        if band_h <= 0:
            return None

        band = QRect(
            rect.left(), rect.bottom() - band_h + 1,
            rect.width(), band_h,
        )

        overlay = self._overlay_color or QColor(0, 0, 0, 160)
        grad = QLinearGradient(
            float(band.left()), float(band.top()),
            float(band.left()), float(band.bottom() + 1),
        )
        transparent = QColor(overlay)
        transparent.setAlpha(0)
        grad.setColorAt(0.0, transparent)
        grad.setColorAt(1.0, overlay)

        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(band, grad)
        painter.setPen(self._resolve_text_color())

        return band

    @staticmethod
    def _draw_text(
            painter: QPainter,
            fm: QFontMetrics,
            text: str,
            x: int,
            cy: int,
            band: QRect,
            right: int,
    ) -> int:
        """在 ``band`` 内绘制单行左对齐文本，返回下一段内容应放置的 x。

        放不下时返回 -1。
        """
        avail = right - x + 1
        if avail <= 0:
            return -1

        elided = fm.elidedText(text, Qt.TextElideMode.ElideRight, avail)
        w = fm.horizontalAdvance(elided)
        if w <= 0:
            return x

        painter.drawText(
            QRect(x, band.top(), w, band.height()),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            elided,
        )
        return x + w

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
    def _resolve_favorite(self, index: int) -> Optional[bool]:
        if self._favorite_getter is None:
            return None
        try:
            value = self._favorite_getter(index)
        except Exception:  # noqa: BLE001 —— paint 期间不允许异常外泄
            _logger.exception("favorite_getter failed at index=%s", index)
            return None
        return None if value is None else bool(value)

    def _resolve_days(self, index: int) -> Optional[int]:
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
            return f"剩余 {days} 天"
        if days == 0:
            return "今天删除"
        return "已过期"
