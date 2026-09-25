#
"""关闭按钮安装辅助。

只负责“造一个标准关闭按钮 + 把 Esc 和鼠标点击汇到同一个信号”，
不决定“关闭”意味着什么 —— 那由 presenter 接线。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QPushButton, QWidget

try:
    from PySide6.QtCore import SignalInstance
except ImportError:  # 视 PySide6 版本而定
    from typing import Any as SignalInstance  # type: ignore[assignment]

__all__ = ["install_close_button"]


def install_close_button(
        host: QWidget,
        signal: "SignalInstance | None" = None,
        object_name: str = "",
        text: str = "\u2715",
        shortcut: "Qt.Key | int | QKeySequence | None" = Qt.Key.Key_Escape,
) -> QPushButton:
    btn = QPushButton(text, host)
    if object_name:
        btn.setObjectName(object_name)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)

    target: "SignalInstance" = signal if signal is not None else btn.clicked

    if target is not btn.clicked:
        btn.clicked.connect(target.emit)

    if shortcut is not None:
        # Qt.Key 是 IntEnum，isinstance(x, int) 对它也成立，
        # 所以必须先判 QKeySequence，再判 int / 其它。
        seq = (
            shortcut
            if isinstance(shortcut, QKeySequence)
            else QKeySequence(shortcut)
        )
        sc = QShortcut(seq, host)
        sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        sc.activated.connect(target.emit)

    return btn
