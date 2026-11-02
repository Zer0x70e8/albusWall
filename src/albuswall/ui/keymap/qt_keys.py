#
"""Qt 键值 → nav 键位字符串的转换。

本模块 import Qt，属于 keymap 层的**适配器**：nav 层（``albuswall.ui.nav``）
不依赖它，也不被它依赖——两边只靠字符串契约相连
（``"up"`` / ``"down"`` / ``"left"`` / ``"right"`` / ``"enter"`` / ``"esc"`` /
``"tab"``，与 ``albuswall.ui.nav.model.NAV_*`` 一一对应）。

为什么需要它：nav 的 ``Node.pass_through_keys`` 与
``NavRegistry.should_pass_through`` 用字符串表达「哪些键让渡给控件自治」，
而事件循环里拿到的是 ``QKeyEvent``。转换是调用方的编排责任：

.. code-block:: python

    from albuswall.ui.keymap.qt_keys import qt_event_to_nav

    key = qt_event_to_nav(event)          # → "up" / None
    if key is not None and nav.should_pass_through(page_id, key):
        event.ignore()                     # 让控件自己处理
        return
    ...

设计约束：

* **只映射 nav 认识的 7 个键**，其它键返回 ``None``（调用方据此判断「这个键
  不归导航层管」，而不是猜一个默认值）；
* **不看 modifier**：``Ctrl+Up`` 与 ``Up`` 都映射成 ``"up"``。是否让渡由调用方
  结合 modifier 判断（nav 的 ``pass_through_keys`` 不区分 modifier）；带
  ``Ctrl`` 的组合键一般应先交给 keymap 的快捷键表，根本走不到这里；
* 与 keymap 的 ``Sequence`` 体系互补：``sequence.py`` 负责
  **字符串 ↔ 规范化快捷键文本**，本模块负责 **Qt 键值 → 导航语义名**。
  二者都不做「QKeyEvent → 快捷键文本」的拼装（那需要 QKeySequence 的
  修饰键状态机，属于将来接入真实按键的分发层）。
"""

from __future__ import annotations

from PySide6.QtCore import Qt

__all__ = ["qt_event_to_nav", "qt_key_to_nav"]

#: Qt 键值 → nav 键位字符串。只收 nav「按键语义」认识的那几个键。
#:
#: * 主键盘与小键盘的方向键在 Qt 里**共用同一枚举值**（``Qt.Key_Up`` 等），
#:   没有「小键盘 Up」这种独立键值，所以不应、也不能单列；
#: * ``Qt.Key_Enter``（小键盘 Enter）与 ``Qt.Key_Return``（主键盘 Enter）是
#:   **两个不同枚举值**，都必须出现，否则小键盘回车不生效；
#: * ``Qt.Key_Backtab`` 是 Shift+Tab 的 Qt 表示，对 nav 仍属 ``"tab"``
#:   （nav 的 ``exit_back`` 就是 Shift+Tab；调用方按 modifier 决定弹栈还是
#:   让渡，见 ``albuswall.ui.nav`` 包文档契约 6）。
_QT_KEY_TO_NAV: dict[int, str] = {
    int(Qt.Key.Key_Up): "up",
    int(Qt.Key.Key_Down): "down",
    int(Qt.Key.Key_Left): "left",
    int(Qt.Key.Key_Right): "right",
    int(Qt.Key.Key_Return): "enter",
    int(Qt.Key.Key_Enter): "enter",
    int(Qt.Key.Key_Escape): "esc",
    int(Qt.Key.Key_Tab): "tab",
    int(Qt.Key.Key_Backtab): "tab",
}


def qt_key_to_nav(key: int | "Qt.Key") -> str | None:
    """Qt 键值 → nav 键位字符串；未映射返回 ``None``。"""
    try:
        return _QT_KEY_TO_NAV.get(int(key))
    except (TypeError, ValueError):  # 传入非整型键值：按未映射处理
        return None


def qt_event_to_nav(event) -> str | None:
    """``QKeyEvent`` → nav 键位字符串；未映射返回 ``None``。

    忽略 modifier：带 ``Ctrl`` / ``Alt`` 的按键仍然映射到同一个语义名，
    是否让渡由调用方结合 modifier 判断。
    """
    key = getattr(event, "key", None)
    if key is None:
        return None
    return qt_key_to_nav(key())
