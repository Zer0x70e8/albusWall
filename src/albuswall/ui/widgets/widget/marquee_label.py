# -*- coding: utf-8 -*-
"""PySide6 跑马灯 Label

特性：
  * 纯 QWidget 自绘，天然是单行，不存在换行 / 多行显示
  * 文本超过控件宽度时自动无缝循环滚动
  * 默认使用等宽字体，内置 IDEA / JetBrains 系常见等宽字体候选列表，
    自动挑一个本机真实存在且确实是等宽（fixed pitch）的字体
  * 支持手动指定字体、滚动速度、间隔、颜色、暂停等
"""

from __future__ import annotations

import sys

from PySide6.QtCore import QPointF, QSize, Qt, QTimer
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QFontInfo,
    QFontMetrics,
    QPainter,
)
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)


# --------------------------------------------------------------------------- #
# 字体候选：IDEA / JetBrains 全家桶里常见的等宽字体，按优先级排列
# --------------------------------------------------------------------------- #
MONO_FONT_CANDIDATES: list[str] = [
    # JetBrains 官方字体（IDEA 默认）
    "JetBrains Mono",
    # 常见代码字体
    "Fira Code",
    "Cascadia Code",
    "Cascadia Mono",
    "Source Code Pro",
    "Hack",
    "Consolas",
    "Menlo",
    "Monaco",
    "DejaVu Sans Mono",
    "Ubuntu Mono",
    "Liberation Mono",
    "Noto Sans Mono",
    "Courier New",
    # 带中文的等宽字体（中文场景推荐装一个）
    "Sarasa Mono SC",
    "更纱黑体 SC",
    "Source Han Mono SC",
    "Noto Sans Mono CJK SC",
]


def available_mono_families() -> list[str]:
    """返回本机真实存在的候选字体名（按优先级）。"""
    families = set(QFontDatabase.families())
    result = [name for name in MONO_FONT_CANDIDATES if name in families]
    if not result:
        # 一个都没装，退回系统固定宽度字体
        result = [QFontDatabase.systemFont(QFontDatabase.FixedFont).family()]
    return result


def pick_monospace_font(
    candidates: list[str] | None = None,
    point_size: float | None = None,
) -> QFont:
    """从候选列表里挑一个可用的等宽字体。

    优先选择 QFontInfo 判定为 fixed pitch 的字体；都不满足时退而求其次
    使用第一个存在的候选；再不行就用系统 FixedFont。
    """
    names = candidates or available_mono_families()
    available = set(QFontDatabase.families())
    usable = [n for n in names if n in available]

    chosen: QFont | None = None

    # 第一轮：确实等宽的
    for name in usable:
        f = QFont(name)
        f.setStyleHint(QFont.Monospace)
        f.setFixedPitch(True)
        if QFontInfo(f).fixedPitch():
            chosen = f
            break

    # 第二轮：退化为第一个存在的
    if chosen is None and usable:
        chosen = QFont(usable[0])
        chosen.setStyleHint(QFont.Monospace)
        chosen.setFixedPitch(True)

    # 第三轮：系统兜底
    if chosen is None:
        chosen = QFontDatabase.systemFont(QFontDatabase.FixedFont)

    chosen.setFixedPitch(True)
    if point_size:
        chosen.setPointSizeF(point_size)
    return chosen


# --------------------------------------------------------------------------- #
# 跑马灯 Label
# --------------------------------------------------------------------------- #
class MarqueeLabel(QWidget):
    """单行跑马灯标签。"""

    def __init__(
        self,
        text: str = "",
        parent: QWidget | None = None,
        *,
        font_families: list[str] | None = None,
        point_size: float | None = None,
    ) -> None:
        super().__init__(parent)

        self._text: str = ""
        self._offset: float = 0.0        # 当前滚动偏移（像素）
        self._gap: float = 48.0          # 两段文本之间的间隔
        self._speed: float = 45.0        # 滚动速度 px/s
        self._interval: int = 16         # 定时器间隔 ms（约 60fps）
        self._color = QColor("#e6e6e6")
        self._align = Qt.AlignLeft | Qt.AlignVCenter
        self._paused: bool = False

        # 字体（默认等宽 + 候选回退）
        self._font = pick_monospace_font(font_families, point_size)

        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMinimumWidth(40)

        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.PreciseTimer)
        self._timer.setInterval(self._interval)
        self._timer.timeout.connect(self._on_tick)

        self.setText(text)

    # ---------------- 公共 API ----------------

    def text(self) -> str:
        return self._text

    def setText(self, text: str) -> None:
        text = "" if text is None else str(text)
        # 强制单行：把所有换行 / 制表符压平
        text = (
            text.replace("\r\n", " ")
            .replace("\n", " ")
            .replace("\r", " ")
            .replace("\t", "    ")
        )
        if text == self._text:
            return
        self._text = text
        self._offset = 0.0
        self.updateGeometry()
        self._sync_scroll_state()
        self.update()

    def setTextColor(self, color: QColor | str) -> None:
        self._color = QColor(color)
        self.update()

    def setScrollSpeed(self, px_per_second: float) -> None:
        """滚动速度，单位 像素/秒。<=0 表示不滚动。"""
        self._speed = max(0.0, float(px_per_second))
        self._sync_scroll_state()

    def setGap(self, gap: float) -> None:
        """循环时两段文本之间的空白间距（像素）。"""
        self._gap = max(0.0, float(gap))
        self.update()

    def setPaused(self, paused: bool) -> None:
        self._paused = paused
        self._sync_scroll_state()

    def isPaused(self) -> bool:
        return self._paused

    def setAlignment(self, align: Qt.AlignmentFlag) -> None:
        """文本未超出宽度时的对齐方式（仅水平部分有效）。"""
        self._align = align
        self.update()

    # ---------------- 字体 ----------------

    def setFont(self, font: QFont) -> None:  # noqa: N802 (Qt 命名)
        f = QFont(font)
        f.setStyleHint(QFont.Monospace)
        f.setFixedPitch(True)
        self._font = f
        self._offset = 0.0
        self.updateGeometry()
        self._sync_scroll_state()
        self.update()

    def font(self) -> QFont:  # noqa: N802
        return self._font

    # ---------------- 尺寸 ----------------

    def sizeHint(self) -> QSize:  # noqa: N802
        fm = QFontMetrics(self._font)
        w = fm.horizontalAdvance(self._text) + 16
        return QSize(max(w, 120), fm.height() + 10)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        fm = QFontMetrics(self._font)
        return QSize(40, fm.height() + 10)

    # ---------------- 内部逻辑 ----------------

    def _text_width(self) -> int:
        return QFontMetrics(self._font).horizontalAdvance(self._text)

    def _needs_scroll(self) -> bool:
        if self._speed <= 0:
            return False
        return self._text_width() > self.contentsRect().width()

    def _sync_scroll_state(self) -> None:
        if self._paused or not self.isVisible() or not self._needs_scroll():
            self._timer.stop()
            if self._offset != 0.0:
                self._offset = 0.0
                self.update()
            return
        if not self._timer.isActive():
            self._timer.start(self._interval)

    def _on_tick(self) -> None:
        cycle = self._text_width() + self._gap
        if cycle <= 0:
            return
        self._offset += self._speed * self._interval / 1000.0
        if self._offset >= cycle:
            self._offset %= cycle
        self.update()

    # ---------------- 事件 ----------------

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._sync_scroll_state()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._sync_scroll_state()

    def hideEvent(self, event) -> None:  # noqa: N802
        self._timer.stop()
        super().hideEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        painter.setFont(self._font)
        painter.setPen(self._color)

        if not self._text:
            return

        fm = painter.fontMetrics()
        rect = self.contentsRect()
        w, h = rect.width(), rect.height()
        # 基线：垂直居中
        baseline = rect.top() + (h + fm.ascent() - fm.descent()) / 2.0

        text_w = fm.horizontalAdvance(self._text)

        # 没超宽：不滚动，按对齐方式静态绘制
        if text_w <= w:
            if self._align & Qt.AlignHCenter:
                x = rect.left() + (w - text_w) / 2.0
            elif self._align & Qt.AlignRight:
                x = rect.left() + (w - text_w)
            else:
                x = float(rect.left())
            painter.drawText(QPointF(x, baseline), self._text)
            return

        # 超宽：循环绘制直到铺满可视区域，实现无缝滚动
        cycle = text_w + self._gap
        x = rect.left() - self._offset
        while x < rect.right():
            painter.drawText(QPointF(x, baseline), self._text)
            x += cycle


# --------------------------------------------------------------------------- #
# 演示窗口
# --------------------------------------------------------------------------- #
class DemoWindow(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("PySide6 跑马灯 Label Demo")
        self.resize(820, 420)
        self.setStyleSheet(
            "DemoWindow { background-color: #1e1e1e; }"
            "QLabel { color: #a9b7c6; font-family: 'Microsoft YaHei'; }"
            "QComboBox { color: #a9b7c6; background: #2b2b2b;"
            "            border: 1px solid #3c3f41; padding: 3px 6px; }"
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(14)

        # 顶部：字体切换
        bar = QHBoxLayout()
        bar.addWidget(QLabel("等宽字体:"))
        self.combo = QComboBox()
        self.combo.setMinimumWidth(240)
        families = available_mono_families()
        self.combo.addItems(families)
        bar.addWidget(self.combo)
        bar.addStretch(1)
        root.addLayout(bar)

        # 示例文本
        samples = [
            "短文本不会滚动：Hello, Marquee!",
            "[INFO] 2024-06-01 12:00:00.123 [main] c.e.demo.Application "
            "- 应用启动完成，耗时 1234 ms，正在等待用户操作……",
            "C:/Users/Administrator/AppData/Local/JetBrains/Toolbox/apps/"
            "IntelliJ-IDEA-Ultimate/ch-0/241.14494.240/jbr/bin/java.exe",
            "The quick brown fox jumps over the lazy dog. 0123456789 !@#$%^&*()",
        ]

        self.labels: list[MarqueeLabel] = []
        for txt in samples:
            lbl = MarqueeLabel(txt, point_size=11)
            lbl.setMinimumHeight(34)
            self.labels.append(lbl)
            root.addWidget(lbl)

        root.addStretch(1)

        # 使用当前自动选中的字体初始化下拉框
        cur_family = self.labels[0].font().family()
        idx = self.combo.findText(cur_family)
        if idx >= 0:
            self.combo.setCurrentIndex(idx)

        self.combo.currentTextChanged.connect(self._on_font_changed)

    def _on_font_changed(self, family: str) -> None:
        f = QFont(family)
        f.setPointSizeF(11)
        f.setStyleHint(QFont.Monospace)
        f.setFixedPitch(True)
        for lbl in self.labels:
            lbl.setFont(f)


def main() -> int:
    app = QApplication(sys.argv)
    w = DemoWindow()
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
