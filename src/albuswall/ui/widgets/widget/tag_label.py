# -*- coding: utf-8 -*-
"""
TagLabel —— 带徽章(Tag)的标签

实现方式：徽章预渲染为 QPixmap，通过 QTextImageFormat 作为内联图片插入 QTextDocument。
完全绕开 QTextObjectInterface / registerHandler 路径（该路径在部分 PySide6 版本里
intrinsicSize 回调不会被调用，表现为徽章被当成零宽字符）。
"""

import sys

from PySide6.QtCore import Qt, QSize, QRectF, QPointF, QEvent, QUrl
from PySide6.QtGui import (
    QColor, QFont, QFontMetricsF, QIcon, QPainter, QPainterPath, QPen, QPixmap,
    QTextCharFormat, QTextCursor, QTextDocument, QTextImageFormat,
    QTextOption, QAbstractTextDocumentLayout, QPalette,
)
from PySide6.QtWidgets import QApplication, QWidget, QVBoxLayout, QSizePolicy


class TagRenderer:
    """把 tag dict 渲染成 QPixmap（高 DPI）。"""
    PAD_H = 6.0
    PAD_V = 2.0
    GAP = 4.0
    RADIUS = 4.0

    @classmethod
    def _resolve_marker(cls, tag):
        ic = tag.get("icon", None)
        if ic is None:
            ic = tag.get("dot", None)
        if ic is None:
            return None
        if isinstance(ic, QColor):
            return "color", ic
        if isinstance(ic, QPixmap):
            return "pixmap", ic
        if isinstance(ic, QIcon):
            return "icon", ic
        if isinstance(ic, str):
            return "color", QColor(ic)
        return None

    @classmethod
    def metrics(cls, doc, tag):
        font: QFont = tag.get("font") or doc.defaultFont()
        fm = QFontMetricsF(font)
        text = tag.get("text", "")
        text_w = fm.horizontalAdvance(text)
        h = fm.height()

        marker = cls._resolve_marker(tag)
        dot_d = h * 0.60 if marker else 0.0
        gap = cls.GAP if marker else 0.0

        w = cls.PAD_H * 2 + text_w + gap + dot_d
        box_h = h + cls.PAD_V * 2
        return font, fm, text_w, w, box_h, dot_d, gap, marker

    @classmethod
    def render(cls, doc, tag, dpr=1.0):
        font, fm, text_w, w, box_h, dot_d, gap, marker = cls.metrics(doc, tag)

        pw = max(1, int(round(w * dpr)))
        ph = max(1, int(round(box_h * dpr)))

        pm = QPixmap(pw, ph)
        pm.fill(Qt.GlobalColor.transparent)
        pm.setDevicePixelRatio(dpr)

        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.scale(dpr, dpr)

        box = QRectF(0, 0, w, box_h)

        # 1) 背景圆角矩形
        path = QPainterPath()
        path.addRoundedRect(box, cls.RADIUS, cls.RADIUS)
        bg = QColor(tag.get("bg", "#E8EEF7"))
        p.fillPath(path, bg)
        border = tag.get("border")
        if border:
            p.setPen(QPen(QColor(border), 1))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path)

        # 2) 左侧文字
        p.setFont(font)
        p.setPen(QColor(tag.get("fg", "#33475B")))
        text_rect = QRectF(cls.PAD_H, cls.PAD_V, text_w, fm.height())
        align = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        p.drawText(text_rect, align, tag.get("text", ""))

        # 3) 右侧圆点/图标
        if marker is not None:
            cx = cls.PAD_H + text_w + gap + dot_d / 2.0
            cy = box_h / 2.0
            kind, payload = marker
            if kind == "color":
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(payload)
                p.drawEllipse(QPointF(cx, cy), dot_d / 2.0, dot_d / 2.0)
            else:
                d = max(1, int(round(dot_d)))
                if kind == "icon":
                    ipm = payload.pixmap(d, d)
                else:
                    ipm = payload.scaled(
                        d, d,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                p.drawPixmap(QPointF(cx - ipm.width() / 2.0,
                                     cy - ipm.height() / 2.0), ipm)

        p.end()
        return pm, w, box_h


class TagLabel(QWidget):
    def __init__(self, text="", tags=None, parent=None):
        super().__init__(parent)
        self._text = text
        self._tags = list(tags or [])
        self._max_hint_width = 400
        self._text_color = self.palette().color(QPalette.ColorRole.WindowText)

        self.setSizePolicy(QSizePolicy.Policy.Preferred,
                           QSizePolicy.Policy.Minimum)

        self._doc = QTextDocument(self)
        self._doc.setDocumentMargin(0)
        self._doc.setDefaultFont(self.font())
        opt = self._doc.defaultTextOption()
        opt.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self._doc.setDefaultTextOption(opt)

        self._rebuild()

    # ---- API ----
    def setText(self, text):  # noqa: N802
        if text == self._text:
            return
        self._text = text
        self._rebuild()

    def text(self):
        return self._text

    def setTags(self, tags):  # noqa: N802
        self._tags = list(tags or [])
        self._rebuild()

    def addTag(self, tag):  # noqa: N802
        self._tags.append(tag)
        self._rebuild()

    def clearTags(self):  # noqa: N802
        self._tags.clear()
        self._rebuild()

    def setTextColor(self, color):  # noqa: N802
        self._text_color = QColor(color)
        self._rebuild()

    def setMaxHintWidth(self, w):  # noqa: N802
        self._max_hint_width = w
        self.updateGeometry()

    def _rebuild(self):
        doc = self._doc
        doc.setDefaultFont(self.font())

        cursor = QTextCursor(doc)
        cursor.beginEditBlock()
        cursor.select(QTextCursor.SelectionType.Document)
        cursor.removeSelectedText()
        cursor.movePosition(QTextCursor.MoveOperation.Start)

        base_fmt = QTextCharFormat()
        base_fmt.setForeground(self._text_color)
        base_fmt.setFont(self.font())

        dpr = self.devicePixelRatioF() or 1.0

        for i, tag in enumerate(self._tags):
            pm, w, h = TagRenderer.render(doc, tag, dpr=dpr)
            name = f"__tag_{i}"
            doc.addResource(QTextDocument.ResourceType.ImageResource,
                            QUrl(name), pm)

            img_fmt = QTextImageFormat()
            img_fmt.setName(name)
            img_fmt.setWidth(w)
            img_fmt.setHeight(h)
            img_fmt.setVerticalAlignment(
                QTextCharFormat.VerticalAlignment.AlignMiddle)
            cursor.insertImage(img_fmt)
            cursor.insertText(" ", base_fmt)

        if self._text:
            cursor.insertText(self._text, base_fmt)

        cursor.endEditBlock()

        self._doc.setTextWidth(self.width() if self.width() > 0 else -1)
        self._doc.documentLayout().documentSize()
        self.updateGeometry()
        self.update()

    # ---- 尺寸 ----
    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, w):
        if w <= 0:
            return 0
        self._doc.setTextWidth(w)
        return int(self._doc.size().height() + 0.999) + 1

    def sizeHint(self):
        doc = self._doc
        saved = doc.textWidth()
        doc.setTextWidth(-1)
        ideal = doc.idealWidth()
        doc.setTextWidth(saved)

        w = min(ideal, self._max_hint_width)
        doc.setTextWidth(w)
        return QSize(int(w) + 1, int(doc.size().height() + 0.999) + 1)

    def minimumSizeHint(self):
        h = self.heightForWidth(40)
        return QSize(40, max(h, 1))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._doc.setTextWidth(self.width())

    def changeEvent(self, event):
        if event.type() == QEvent.Type.FontChange:
            self._rebuild()
        super().changeEvent(event)

    def paintEvent(self, event):
        if self._doc.textWidth() != self.width():
            self._doc.setTextWidth(self.width())

        painter = QPainter(self)
        ctx = QAbstractTextDocumentLayout.PaintContext()
        ctx.palette = self.palette()
        ctx.clip = QRectF(event.rect())
        self._doc.documentLayout().draw(painter, ctx)


# ---- Demo ----
if __name__ == "__main__":
    def _make_icon_pixmap(color, size=16):
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(color))
        p.drawRoundedRect(1, 1, size - 2, size - 2, 3, 3)
        p.setBrush(QColor("#FFFFFF"))
        p.drawEllipse(QPointF(size / 2, size / 2), size * 0.22, size * 0.22)
        p.end()
        return pm


    def main():
        app = QApplication(sys.argv)

        win = QWidget()
        win.setWindowTitle("TagLabel Demo")
        win.resize(440, 560)
        win.setStyleSheet("background: #FFFFFF;")

        lay = QVBoxLayout(win)
        lay.setContentsMargins(20, 20, 20, 20)
        lay.setSpacing(18)

        l1 = TagLabel("短文本示例，徽章在前，文本紧随其后。")
        l1.setTags([{"text": "Python", "bg": "#E8F0FE", "fg": "#1A73E8",
                     "dot": "#1A73E8"}])
        lay.addWidget(l1)

        l2 = TagLabel(
            "这是一段很长的文本，用来测试自动换行的效果。注意换行之后文字依然从最左边开始，"
            "而徽章紧贴在文本之前。因为徽章是作为内联图片参与排版的，所以首行会先出现徽章，"
            "然后正文从徽章后面继续往后排，排满一行之后自然折到下一行最左侧。"
        )
        l2.setTags([{"text": "长文本", "bg": "#FDE7E9", "fg": "#C5221F",
                     "dot": "#EA4335"}])
        lay.addWidget(l2)

        l3 = TagLabel("徽章右侧也可以放小图标，而不是纯色圆点。")
        l3.setTags([{"text": "已完成", "bg": "#E6F4EA", "fg": "#137333",
                     "icon": _make_icon_pixmap("#34A853")}])
        lay.addWidget(l3)

        l4 = TagLabel("支持多个徽章串联，也支持只有文字没有圆点的样式。")
        l4.setTags([
            {"text": "Tag A", "bg": "#FFF4E5", "fg": "#B26A00"},
            {"text": "Tag B", "bg": "#F3E8FD", "fg": "#7B1FA2", "dot": "#9C27B0"},
        ])
        lay.addWidget(l4)

        lay.addStretch(1)
        win.show()
        sys.exit(app.exec())


    # if __name__ == "__main__":
    main()
