#
"""关闭按钮安装辅助。

只负责「造一个标准关闭按钮」，不决定「关闭」意味着什么 —— 按钮的
``clicked`` 由外部接线（或直接读 ``close_button``）。

``shortcut`` 参数保留给「这个按钮自己带一个快捷键」的场景；内置界面
不默认给按钮挂 ``QShortcut``，以免和全局键位系统抢 Esc。
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
    """在 ``host`` 上造一个标准关闭按钮并返回。

    :param host: 按钮的父控件（也是 ``shortcut`` 的作用域）。
    :param signal: 希望被触发的信号；``None`` 表示用按钮自己的 ``clicked``。
    :param object_name: 可选 objectName（样式表 / 对象命名用）。
    :param text: 按钮文字，默认 ``✕``。
    :param shortcut: 可选快捷键；内置界面不传。
    """
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
