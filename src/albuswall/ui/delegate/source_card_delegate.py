#
"""导入源卡片委托：圆角底 + 标题 + 描述 + 路径 + 标签药丸。

绘制参数（圆角 / 内边距 / 药丸尺寸 / 描述行数 / 透明度 …）优先读**视图**
上的同名属性（``ColumnListView`` 用 QSS 的 ``qproperty-cardRadius`` 之类去调），
读不到就用本模块的 :data:`CARD_DEFAULTS` —— 因此委托不依赖任何具体视图类，
绑到 ``QListView``、``ColumnListView`` 或自绘视图上都能跑。

数据全部来自 :class:`~albuswall.ui.model.roles.SourceCardRole`，与
:class:`~albuswall.ui.model.SourceCardModel` 对应。
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QModelIndex, QRectF, QSize, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QPainter,
    QPainterPath,
    QPen,
    QTextLayout,
    QTextOption,
)
from PySide6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem

from ..model.roles import SourceCardRole

__all__ = ["CARD_DEFAULTS", "SourceCardDelegate"]

#: 视图没提供同名属性时用的默认绘制参数。
CARD_DEFAULTS: dict[str, int] = {
    "cardRadius": 10,
    "cardPadding": 12,
    "cardGap": 6,
    "chipHeight": 22,
    "chipPadX": 9,
    "chipGap": 6,
    "maxDescLines": 3,
    "cardMinWidth": 220,
    "cardMinHeight": 176,
    "hoverAlpha": 4,
    "selectAlpha": 10,
    "descAlpha": 78,
    "pathAlpha": 50,
}


class SourceCardDelegate(QStyledItemDelegate):
    """导入源卡片绘制委托。"""

    # ---- 卡片配色（对齐 QSS 里 #IngestSource 的 Big Capsule 风格） ----
    # 不要用 QPalette.Base —— 深色主题下它是不透明纯黑，会挡住模糊背景。
    CARD_BG = QColor(96, 96, 96, 140)
    CARD_BORDER = QColor(140, 140, 140, 200)
    CARD_BORDER_HOVER = QColor(200, 200, 200, 225)
    CARD_TEXT = QColor(240, 240, 240, 235)
    CARD_ACCENT = QColor(0, 122, 255)
    CHIP_BG = QColor(0, 122, 255, 64)
    CHIP_FG = QColor(220, 235, 255, 240)

    def __init__(self, parent: Any = None, **overrides: int) -> None:
        super().__init__(parent)
        self._defaults = dict(CARD_DEFAULTS)
        self._defaults.update({k: int(v) for k, v in overrides.items()})
        self._fonts_key: tuple[Any, ...] | None = None
        self._title_font = self._desc_font = self._path_font = self._chip_font = None
        self._title_fm = self._desc_fm = self._path_fm = self._chip_fm = None

    # ------------------------------------------------------------------ #
    # 参数读取
    # ------------------------------------------------------------------ #
    def _value(self, option: QStyleOptionViewItem, name: str) -> int:
        """先读视图同名属性（QSS 可调），再退回本地默认值。"""
        view = option.widget
        if view is not None:
            found = getattr(view, name, None)
            if found is not None:
                try:
                    return int(found)
                except (TypeError, ValueError):
                    pass
        return self._defaults[name]

    # ------------------------------------------------------------------ #
    # 字体：跟随 option.font 动态重建
    # ------------------------------------------------------------------ #
    def _ensure_fonts(self, base: QFont) -> None:
        key = (base.family(), base.pointSizeF(), base.weight(), base.italic())
        if key == self._fonts_key:
            return
        point = base.pointSizeF() or 10.0

        title = QFont(base)
        title.setPointSizeF(point + 1.5)
        title.setBold(True)
        self._title_font = title
        self._title_fm = QFontMetrics(title)

        description = QFont(base)
        description.setPointSizeF(max(8.0, point - 0.5))
        self._desc_font = description
        self._desc_fm = QFontMetrics(description)

        path = QFont(base)
        path.setPointSizeF(max(7.5, point - 1.5))
        path.setStyleHint(QFont.StyleHint.Monospace)
        self._path_font = path
        self._path_fm = QFontMetrics(path)

        chip = QFont(base)
        chip.setPointSizeF(max(7.5, point - 1.5))
        self._chip_font = chip
        self._chip_fm = QFontMetrics(chip)

        self._fonts_key = key

    # ------------------------------------------------------------------ #
    # 颜色工具
    # ------------------------------------------------------------------ #
    @staticmethod
    def _mix(first: QColor, second: QColor, ratio: float) -> QColor:
        ratio = max(0.0, min(1.0, ratio))
        return QColor(
            round(first.red() + (second.red() - first.red()) * ratio),
            round(first.green() + (second.green() - first.green()) * ratio),
            round(first.blue() + (second.blue() - first.blue()) * ratio),
            round(first.alpha() + (second.alpha() - first.alpha()) * ratio),
        )

    @staticmethod
    def _alpha(color: QColor, alpha: float) -> QColor:
        mixed = QColor(color)
        mixed.setAlphaF(max(0.0, min(1.0, alpha)))
        return mixed

    # ------------------------------------------------------------------ #
    # 按词换行（最多 max_lines 行，超出补省略号）
    # ------------------------------------------------------------------ #
    @staticmethod
    def wrap_text(
            text: str,
            font: QFont,
            max_width: float,
            max_lines: int,
    ) -> list[str]:
        """用 ``QTextLayout`` 按词换行，最多 ``max_lines`` 行。"""
        if not text or max_width <= 0 or max_lines <= 0:
            return []

        layout = QTextLayout(text, font)
        text_option = QTextOption()
        text_option.setWrapMode(
            QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        layout.setTextOption(text_option)
        layout.beginLayout()

        lines: list[str] = []
        try:
            while len(lines) < max_lines:
                line = layout.createLine()
                if not line.isValid():
                    break
                line.setLineWidth(float(max_width))
                start = line.textStart()
                lines.append(
                    text[start:start + line.textLength()].rstrip())

            # 还有没排完的内容 → 最后一行补省略号
            if lines:
                extra = layout.createLine()
                if extra.isValid():
                    metrics = QFontMetrics(font)
                    last = lines[-1].rstrip()
                    while last and metrics.horizontalAdvance(
                            last + "…") > max_width:
                        last = last[:-1]
                    lines[-1] = last + "…"
        finally:
            layout.endLayout()

        return lines

    # ------------------------------------------------------------------ #
    # 尺寸
    # ------------------------------------------------------------------ #
    def sizeHint(  # noqa: N802 - Qt 命名
            self,
            option: QStyleOptionViewItem,
            index: QModelIndex,
    ) -> QSize:
        return QSize(
            self._value(option, "cardMinWidth"),
            self._value(option, "cardMinHeight"),
        )

    # ------------------------------------------------------------------ #
    # 绘制
    # ------------------------------------------------------------------ #
    def paint(
            self,
            painter: QPainter,
            option: QStyleOptionViewItem,
            index: QModelIndex,
    ) -> None:
        radius = self._value(option, "cardRadius")
        padding = self._value(option, "cardPadding")
        gap = self._value(option, "cardGap")
        chip_height = self._value(option, "chipHeight")
        chip_pad_x = self._value(option, "chipPadX")
        chip_gap = self._value(option, "chipGap")
        max_lines = self._value(option, "maxDescLines")

        hover_ratio = self._value(option, "hoverAlpha") / 100.0
        select_ratio = self._value(option, "selectAlpha") / 100.0
        desc_alpha = self._value(option, "descAlpha") / 100.0
        path_alpha = self._value(option, "pathAlpha") / 100.0

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        rect = QRectF(option.rect).adjusted(1.0, 1.0, -1.0, -1.0)
        if rect.width() < 8 or rect.height() < 8:
            painter.restore()
            return

        self._ensure_fonts(option.font)

        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        focused = bool(option.state & QStyle.StateFlag.State_HasFocus)

        if selected:
            background = self._mix(self.CARD_BG, self.CARD_ACCENT, select_ratio)
            border, border_width = self.CARD_ACCENT, 1.4
        elif hovered:
            background = self._mix(
                self.CARD_BG, QColor(128, 128, 128, 120), hover_ratio)
            border, border_width = self.CARD_BORDER_HOVER, 1.2
        else:
            background = self.CARD_BG
            border, border_width = self.CARD_BORDER, 1.0

        if focused:
            border, border_width = self.CARD_ACCENT, 2.0

        card_path = QPainterPath()
        card_path.addRoundedRect(rect, radius, radius)
        painter.fillPath(card_path, background)
        painter.setPen(QPen(border, border_width))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(card_path)

        inner = rect.adjusted(padding, padding, -padding, -padding)
        if inner.width() < 20 or inner.height() < 20:
            painter.restore()
            return

        title_fm, desc_fm = self._title_fm, self._desc_fm
        path_fm, chip_fm = self._path_fm, self._chip_fm

        # ---------- 标题 ----------
        title_height = float(title_fm.height())
        title_rect = QRectF(
            inner.left(), inner.top(), inner.width(), title_height)
        title = str(index.data(SourceCardRole.Title) or "")
        painter.setFont(self._title_font)
        painter.setPen(self.CARD_TEXT)
        painter.drawText(
            title_rect,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            title_fm.elidedText(
                title, Qt.TextElideMode.ElideRight, int(title_rect.width())),
        )

        # ---------- 底部：标签 → 路径（自下而上） ----------
        cursor_bottom = inner.bottom()
        tags = [
            str(tag) for tag in (index.data(SourceCardRole.Tags) or [])
            if str(tag).strip()
        ]
        if tags:
            tags_rect: QRectF | None = QRectF(
                inner.left(),
                cursor_bottom - chip_height,
                inner.width(),
                float(chip_height),
            )
            cursor_bottom = tags_rect.top() - gap
        else:
            tags_rect = None

        path_text = str(index.data(SourceCardRole.Path) or "")
        if path_text:
            path_height = float(path_fm.height())
            path_rect: QRectF | None = QRectF(
                inner.left(), cursor_bottom - path_height,
                inner.width(), path_height)
            cursor_bottom = path_rect.top() - gap
        else:
            path_rect = None

        # ---------- 描述 ----------
        desc_top = title_rect.bottom() + gap
        desc_rect = QRectF(
            inner.left(), desc_top,
            inner.width(), max(0.0, cursor_bottom - desc_top))

        description = str(index.data(SourceCardRole.Description) or "")
        line_height = float(desc_fm.lineSpacing())
        if description and desc_rect.height() >= line_height > 0:
            line_count = max(
                1, min(max_lines, int(desc_rect.height() // line_height)))
            lines = self.wrap_text(
                description, self._desc_font, desc_rect.width(), line_count)
            painter.setFont(self._desc_font)
            painter.setPen(self._alpha(self.CARD_TEXT, desc_alpha))
            y = desc_rect.top()
            for line in lines:
                painter.drawText(
                    QRectF(desc_rect.left(), y, desc_rect.width(), line_height),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    line,
                )
                y += line_height

        # ---------- 路径（半等宽，中间省略） ----------
        if path_rect is not None and path_rect.width() > 1:
            painter.setFont(self._path_font)
            painter.setPen(self._alpha(self.CARD_TEXT, path_alpha))
            painter.drawText(
                path_rect,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                path_fm.elidedText(
                    path_text, Qt.TextElideMode.ElideMiddle,
                    int(path_rect.width())),
            )

        # ---------- 标签药丸 ----------
        if tags and tags_rect is not None and tags_rect.width() > 1:
            painter.setFont(self._chip_font)
            x = tags_rect.left()
            radius_y = tags_rect.height() / 2.0
            right = tags_rect.right()
            for tag in tags:
                width = float(chip_fm.horizontalAdvance(tag) + 2 * chip_pad_x)
                if x + width > right:
                    break
                chip = QRectF(x, tags_rect.top(), width, tags_rect.height())
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(self.CHIP_BG)
                painter.drawRoundedRect(chip, radius_y, radius_y)
                painter.setPen(self.CHIP_FG)
                painter.drawText(chip, Qt.AlignmentFlag.AlignCenter, tag)
                x += width + chip_gap

        painter.restore()
