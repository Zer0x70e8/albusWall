#
"""按键文本 → 规范形式 / 键盘事件 → 按键文本（唯一一处 Qt 解释）。

整个键位系统只在**一个地方**把「用户看到 / 配置文件里写的字符串」和
「Qt 送来的键盘事件」对上，就是本模块。其余模块（registry / state /
store）都不直接碰 ``QKeySequence``，这样「按键怎么算同一个键」只有一种
口径。

规范形式
========

落盘、比较、去重统一使用 Qt 的 **PortableText**：

.. code-block:: text

    "ctrl+,"            →  "Ctrl+,"
    "CTRL + Shift + o"  →  "Ctrl+Shift+O"
    "left"              →  "Left"
    "F1"                →  "F1"

只支持**单段**组合键（一个按键 + 修饰键）。多段序列（``Ctrl+K, Ctrl+C``）
需要待定状态机，当前明确不支持：``normalize_sequence`` 会抛
``ValueError``，设置页把错误显示在行内，不会悄悄绑错。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeyEvent, QKeySequence
from PySide6.QtWidgets import (
    QKeySequenceEdit,
    QLineEdit,
    QPlainTextEdit,
    QTextEdit,
)

__all__ = [
    "MODIFIER_KEYS",
    "display_text",
    "is_key_sequence_editor",
    "is_modifier_key",
    "is_text_input",
    "normalize_sequence",
    "sequence_from_event",
    "text_input_allows_modifiers",
]

#: 单独按下的修饰键（不构成有效绑定）
MODIFIER_KEYS = frozenset({
    Qt.Key.Key_Shift,
    Qt.Key.Key_Control,
    Qt.Key.Key_Alt,
    Qt.Key.Key_Meta,
    Qt.Key.Key_AltGr,
    Qt.Key.Key_CapsLock,
    Qt.Key.Key_NumLock,
    Qt.Key.Key_ScrollLock,
    Qt.Key.Key_Super_L,
    Qt.Key.Key_Super_R,
    Qt.Key.Key_Hyper_L,
    Qt.Key.Key_Hyper_R,
})

#: 输入框里也照样抢的修饰键（Cmd/Ctrl 组合通常是命令，不是输入内容）
_COMMAND_MODIFIERS = (
    Qt.KeyboardModifier.ControlModifier
    | Qt.KeyboardModifier.AltModifier
    | Qt.KeyboardModifier.MetaModifier
)


def normalize_sequence(text: object) -> str:
    """把任意写法收敛成 PortableText 规范形式。

    Args:
        text: 配置 / 用户输入里的按键文本；``None`` / 空串视为「不绑定」。

    Returns:
        规范字符串；空串表示不绑定。

    Raises:
        ValueError: 无法解析，或写成了多段序列（当前不支持）。
    """
    raw = str(text or "").strip()
    if not raw:
        return ""

    sequence = QKeySequence.fromString(
        raw, QKeySequence.SequenceFormat.PortableText,
    )
    if sequence.isEmpty():
        raise ValueError(f"unsupported key sequence: {raw!r}")
    if sequence.count() != 1:
        raise ValueError(
            f"multi-key sequences are not supported: {raw!r} "
            f"(bind a single key combination instead)"
        )
    return sequence.toString(QKeySequence.SequenceFormat.PortableText)


def display_text(sequence: str) -> str:
    """规范形式 → 当前平台的展示文本（macOS 上显示 ⌘ 之类）。"""
    text = str(sequence or "").strip()
    if not text:
        return ""
    parsed = QKeySequence.fromString(
        text, QKeySequence.SequenceFormat.PortableText,
    )
    if parsed.isEmpty():
        return text
    return parsed.toString(QKeySequence.SequenceFormat.NativeText)


def is_modifier_key(key: int) -> bool:
    """``key`` 是不是「单独的修饰键」（不应被当成绑定）。"""
    try:
        return Qt.Key(key) in MODIFIER_KEYS
    except ValueError:  # 未知键值
        return False


def sequence_from_event(event: QKeyEvent) -> str:
    """键盘事件 → 规范按键文本；修饰键本身返回空串。

    Qt 在按键事件里已经合并了修饰状态（``Shift+F`` 是 key=F +
    ``ShiftModifier``），所以这里直接交给 ``QKeySequence`` 归一。
    """
    if is_modifier_key(event.key()):
        return ""
    return QKeySequence(event.keyCombination()).toString(
        QKeySequence.SequenceFormat.PortableText
    )


def is_text_input(widget: object) -> bool:
    """焦点控件是不是「正在输入文字」的控件。

    输入框里只放行带 Ctrl / Alt / Meta 的绑定（少量动作可显式允许），
    否则用户想敲一个 ``F`` 就会触发「收藏」。
    """
    if widget is None:
        return False
    if isinstance(widget, (QLineEdit, QPlainTextEdit, QTextEdit)):
        return True
    if isinstance(widget, QKeySequenceEdit):
        return True
    # QSpinBox / QDoubleSpinBox / 可编辑 QComboBox 的焦点控件是内部的
    # QLineEdit，上面的 isinstance 已经覆盖；这里再兜一层命名判断，
    # 兼容第三方代理控件。
    name = type(widget).__name__.lower()
    return name.endswith(("lineedit", "textedit", "plaintextedit"))


def is_key_sequence_editor(widget: object) -> bool:
    """焦点是否落在 ``QKeySequenceEdit``（或它的子控件）里。

    采键状态必须完全放行：否则用户想绑 ``Ctrl+F`` 时，事件会先被键位
    系统吃掉、变成「聚焦搜索」，编辑框里永远录不到这个组合。
    """
    node = widget
    while node is not None:
        if isinstance(node, QKeySequenceEdit):
            return True
        try:
            node = node.parentWidget()
        except AttributeError:  # 不是 QWidget
            return False
    return False


def text_input_allows_modifiers(modifiers: Qt.KeyboardModifier) -> bool:
    """输入框里该事件是否仍应交给键位系统（带命令修饰键时返回 True）。"""
    return bool(modifiers & _COMMAND_MODIFIERS)
