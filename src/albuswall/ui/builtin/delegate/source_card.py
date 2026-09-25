#
""""""

from PySide6.QtCore import (
    QModelIndex,
    QRectF,
    QSize,
    Qt,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QPainter,
    QPainterPath,
    QPen,
    # QPalette,
    QTextLayout,
    QTextOption,
)
from PySide6.QtWidgets import (
    QStyle,
    QStyledItemDelegate,
)

from albuswall.ui.builtin.model.source_card import CardListModel
from albuswall.ui.builtin.widgets import ColumnListView


class CardItemDelegate(QStyledItemDelegate):
    # 兜底默认值（view 不存在时用）
    _DEFAULTS = dict(ColumnListView.CARD_DEFAULTS)
    # ---------- 卡片配色（对齐 QSS #IngestSource 的 Big Capsule 风格） ----------
    # 不要用 QPalette.Base —— 深色主题下它是不透明纯黑，会挡住模糊背景。
    CARD_BG        = QColor(96, 96, 96, 140)     # rgba(96, 96, 96, 0.22)
    CARD_BORDER    = QColor(140, 140, 140, 200)    # rgba(80, 80, 80, 0.70)
    CARD_BORDER_HO = QColor(200, 200, 200, 225) # hover 边框
    CARD_TEXT      = QColor(240, 240, 240, 235) # 主文本
    CARD_ACCENT    = QColor(0, 122, 255)        # Apple Blue

    def __init__(self, parent=None):
        super().__init__(parent)
        self._fonts_key = None
        self._title_font = self._desc_font = self._path_font = self._chip_font = None
        # ★ 与字体一起缓存的度量
        self._title_fm = self._desc_fm = self._path_fm = self._chip_fm = None

    # ---------- 参数读取 ----------
    @staticmethod
    def _v(option, name: str, defaults: dict) -> int:
        view = option.widget
        if view is not None:
            val = getattr(view, name, None)
            if val is not None:
                # noinspection bad-argument-type
                return int(val)
        return defaults[name]

    # ---------- 字体：跟随 option.font 动态重建 ----------
    def _ensure_fonts(self, base: QFont):
        key = (base.family(), base.pointSizeF(), base.weight(), base.italic())
        if key == self._fonts_key:
            return
        pt = base.pointSizeF() or 10.0

        f = QFont(base); f.setPointSizeF(pt + 1.5); f.setBold(True)
        self._title_font = f
        self._title_fm = QFontMetrics(f)

        f = QFont(base); f.setPointSizeF(max(8.0, pt - 0.5))
        self._desc_font = f
        self._desc_fm = QFontMetrics(f)

        f = QFont(base)
        f.setPointSizeF(max(7.5, pt - 1.5))
        f.setStyleHint(QFont.StyleHint.Monospace)
        self._path_font = f
        self._path_fm = QFontMetrics(f)

        f = QFont(base); f.setPointSizeF(max(7.5, pt - 1.5))
        self._chip_font = f
        self._chip_fm = QFontMetrics(f)

        self._fonts_key = key

    # ---------- 颜色工具 ----------
    @staticmethod
    def _mix(a: QColor, b: QColor, t: float) -> QColor:
        t = max(0.0, min(1.0, t))
        return QColor(
            round(a.red() + (b.red() - a.red()) * t),
            round(a.green() + (b.green() - a.green()) * t),
            round(a.blue() + (b.blue() - a.blue()) * t),
            round(a.alpha() + (b.alpha() - a.alpha()) * t),
        )

    @staticmethod
    def _alpha(c: QColor, a: float) -> QColor:
        c2 = QColor(c)
        c2.setAlphaF(max(0.0, min(1.0, a)))
        return c2

    # ---------- 文本换行（★ 原缺失函数） ----------
    @staticmethod
    def _wrap_text(text: str, font: QFont,
                   max_width: float, max_lines: int) -> list[str]:
        """用 QTextLayout 做按词换行，最多 max_lines 行；超出加省略号。"""
        if not text or max_width <= 0 or max_lines <= 0:
            return []

        layout = QTextLayout(text, font)
        opt = QTextOption()
        opt.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        layout.setTextOption(opt)
        layout.beginLayout()

        lines: list[str] = []
        try:
            while len(lines) < max_lines:
                line = layout.createLine()
                if not line.isValid():
                    break
                line.setLineWidth(float(max_width))
                start = line.textStart()
                length = line.textLength()
                lines.append(text[start:start + length].rstrip())

            # 若还有未排完的内容，最后一行加省略号
            if lines:
                extra = layout.createLine()
                if extra.isValid():
                    fm = QFontMetrics(font)
                    last = lines[-1].rstrip()
                    # noinspection shadowing-builtins
                    ellipsis = "…"
                    while last and fm.horizontalAdvance(last + ellipsis) > max_width:
                        last = last[:-1]
                    lines[-1] = last + ellipsis
        finally:
            layout.endLayout()

        return lines

    # ---------- 尺寸 ----------
    def sizeHint(self, option, index) -> QSize:
        return QSize(self._v(option, "cardMinWidth", self._DEFAULTS),
                     self._v(option, "cardMinHeight", self._DEFAULTS))

    # ---------- 绘制 ----------
    # noinspection method-overriding
    def paint(self, painter: QPainter, option, index: QModelIndex):
        # noinspection pep8-naming
        D = self._DEFAULTS

        radius = self._v(option, "cardRadius", D)
        padding = self._v(option, "cardPadding", D)
        gap = self._v(option, "cardGap", D)
        chip_h = self._v(option, "chipHeight", D)
        chip_pad_x = self._v(option, "chipPadX", D)
        chip_gap = self._v(option, "chipGap", D)
        max_lines = self._v(option, "maxDescLines", D)

        # border_t = self._v(option, "borderAlpha", D) / 100.0
        hover_t = self._v(option, "hoverAlpha", D) / 100.0
        # hover_border_t = self._v(option, "hoverBorderAlpha", D) / 100.0
        select_t = self._v(option, "selectAlpha", D) / 100.0
        desc_a = self._v(option, "descAlpha", D) / 100.0
        path_a = self._v(option, "pathAlpha", D) / 100.0
        # chip_bg_a = self._v(option, "chipBgAlpha", D) / 100.0

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        rect = QRectF(option.rect).adjusted(1.0, 1.0, -1.0, -1.0)
        if rect.width() < 8 or rect.height() < 8:
            painter.restore()
            return

        # pal = option.palette
        # base_bg = pal.color(QPalette.ColorRole.Base)
        # text_col = pal.color(QPalette.ColorRole.Text)
        # accent = pal.color(QPalette.ColorRole.Highlight)
        # 直接使用类常量，避免调色板在深色主题下返回不透明黑
        base_bg = self.CARD_BG
        text_col = self.CARD_TEXT
        accent = self.CARD_ACCENT

        self._ensure_fonts(option.font)

        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        focused = bool(option.state & QStyle.StateFlag.State_HasFocus)

        # if selected:
        #     bg = self._mix(base_bg, accent, select_t)
        #     border = accent
        #     border_w = 1.4
        # elif hovered:
        #     bg = self._mix(base_bg, accent, hover_t)
        #     border = self._mix(base_bg, accent, hover_border_t)
        #     border_w = 1.2
        # else:
        #     bg = base_bg
        #     border = self._mix(base_bg, text_col, border_t)
        #     border_w = 1.0

        if selected:
            bg = self._mix(base_bg, accent, select_t)      # 蓝调混合
            border = accent
            border_w = 1.4
        elif hovered:
            bg = self._mix(base_bg, QColor(128, 128, 128, 120), hover_t)
            border = self.CARD_BORDER_HO
            border_w = 1.2
        else:
            bg = base_bg
            border = self.CARD_BORDER
            border_w = 1.0

        if focused:
            border, border_w = accent, 2.0

        title_col = text_col
        desc_col = self._alpha(text_col, desc_a)
        path_col = self._alpha(text_col, path_a)
        # chip_bg = self._alpha(accent, chip_bg_a)
        # chip_fg = accent.darker(130)
        chip_bg = QColor(0, 122, 255, 64)  # rgba(0, 122, 255, 0.25)
        chip_fg = QColor(220, 235, 255, 240)  # 近白偏蓝

        # ---------- 卡片背景 ----------
        card_path = QPainterPath()
        card_path.addRoundedRect(rect, radius, radius)
        painter.fillPath(card_path, bg)
        painter.setPen(QPen(border, border_w))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(card_path)

        inner = rect.adjusted(padding, padding, -padding, -padding)
        if inner.width() < 20 or inner.height() < 20:
            painter.restore()
            return

        # ★ 使用缓存度量
        title_fm = self._title_fm
        desc_fm = self._desc_fm
        path_fm = self._path_fm
        chip_fm = self._chip_fm

        align_l_v = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        elide_right = Qt.TextElideMode.ElideRight
        elide_middle = Qt.TextElideMode.ElideMiddle

        # ---------- 标题 ----------
        title_h = float(title_fm.height())
        title_rect = QRectF(inner.left(), inner.top(), inner.width(), title_h)
        title = index.data(CardListModel.TitleRole) or ""
        painter.setFont(self._title_font)
        painter.setPen(title_col)
        painter.drawText(title_rect, align_l_v,
                         title_fm.elidedText(str(title), elide_right,
                                             int(title_rect.width())))

        # ---------- 底部：标签 -> 路径 ----------
        cursor_bottom = inner.bottom()
        tags = [str(t) for t in (index.data(CardListModel.TagsRole) or [])
                if str(t).strip()]

        if tags:
            tags_rect = QRectF(inner.left(), cursor_bottom - chip_h,
                               inner.width(), float(chip_h))
            cursor_bottom = tags_rect.top() - gap
        else:
            tags_rect = QRectF()

        path_text = str(index.data(CardListModel.PathRole) or "")
        if path_text:
            ph = float(path_fm.height())
            path_rect = QRectF(inner.left(), cursor_bottom - ph, inner.width(), ph)
            cursor_bottom = path_rect.top() - gap
        else:
            path_rect = QRectF()

        # ---------- 描述 ----------
        desc_top = title_rect.bottom() + gap
        desc_rect = QRectF(inner.left(), desc_top,
                           inner.width(), max(0.0, cursor_bottom - desc_top))

        desc = str(index.data(CardListModel.DescriptionRole) or "")
        line_h = float(desc_fm.lineSpacing())
        if desc and desc_rect.height() >= line_h > 0:
            lines_n = max(1, min(max_lines, int(desc_rect.height() // line_h)))
            lines = self._wrap_text(desc, self._desc_font,
                                    desc_rect.width(), lines_n)
            painter.setFont(self._desc_font)
            painter.setPen(desc_col)
            y = desc_rect.top()
            for line in lines:
                painter.drawText(
                    QRectF(desc_rect.left(), y, desc_rect.width(), line_h),
                    align_l_v, line)
                y += line_h

        # ---------- 路径 ----------
        if not path_rect.isNull() and path_rect.width() > 1:
            painter.setFont(self._path_font)
            painter.setPen(path_col)
            painter.drawText(path_rect, align_l_v,
                             path_fm.elidedText(path_text, elide_middle,
                                                int(path_rect.width())))

        # ---------- 标签 ----------
        if tags and tags_rect.width() > 1:
            painter.setFont(self._chip_font)
            x = tags_rect.left()
            r = tags_rect.height() / 2.0
            right = tags_rect.right()
            for tag in tags:
                w = float(chip_fm.horizontalAdvance(tag) + 2 * chip_pad_x)
                if x + w > right:
                    break
                chip = QRectF(x, tags_rect.top(), w, tags_rect.height())
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(chip_bg)
                painter.drawRoundedRect(chip, r, r)
                painter.setPen(chip_fg)
                painter.drawText(chip, Qt.AlignmentFlag.AlignCenter, tag)
                x += w + chip_gap

        painter.restore()
