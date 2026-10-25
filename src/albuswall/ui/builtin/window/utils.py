#
"""关闭按钮安装辅助。

只负责「造一个标准关闭按钮」，不决定「关闭」意味着什么 —— 那由 presenter
接线。

键盘：内置 UI 的 ``Esc`` 统一由键位系统（``keymap`` 的 ``window.escape``）
分发，不再由每个关闭按钮各自安装一个 ``QShortcut`` —— 否则「Esc 到底关
哪一层」会变成创建顺序的副产品。``shortcut`` 参数保留给外部 / 第三方 UI
显式要求「这个按钮自己带一个快捷键」的场景。
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
        shortcut: "Qt.Key | int | QKeySequence | None" = None,
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
