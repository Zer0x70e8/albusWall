#
"""快捷键序列：Qt 键位字符串的解析、规范化与分类。

一个 :class:`KeySequence` 就是 **QKeySequence PortableText** 的规范化字符串。
选 PortableText 而不是 NativeText 的原因很实际：后者随平台 / 键盘布局变化
（macOS 上是 ``⌘`` 而不是 ``Ctrl``），写进配置文件既不可移植，日志里也没法
直接对比；PortableText 在所有平台都写成 ``Ctrl+Shift+P``。

三种形态（:class:`ShortcutKind`）：

============  ==========================  ==============================
形态          例子                        说明
============  ==========================  ==============================
``SINGLE``    ``F5`` / ``Space``          单按键（无修饰键）
``MODIFIED``  ``Ctrl+N`` / ``Ctrl+Shift+P`` 修饰键 + 按键
``CHORD``     ``G, G`` / ``Ctrl+K, Ctrl+C`` 多步和弦（Qt 最多 4 步）
============  ==========================  ==============================

配置值用 **JSON 数组** 表示「一个命令挂多个键」::

    view.fullscreen = ["F11", "Ctrl+Shift+F"]

为什么不是逗号分隔的列表：PortableText 里逗号是**和弦分隔符**
（``G, G``），拿它分隔多个快捷键会把和弦拆坏；同理 ``Ctrl+,``（逗号键）
也只有放在 JSON 字符串里才不会被吞掉。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from PySide6.QtGui import QKeySequence

from .errors import InvalidKeySequence

__all__ = [
    "CHORD_SEPARATOR",
    "MAX_CHORDS",
    "MODIFIERS",
    "KeySequence",
    "ShortcutKind",
    "canonical_sequence",
    "format_sequences",
    "parse_sequence",
    "parse_sequences",
]

#: Qt PortableText 认的修饰键名（也是规范顺序）。
MODIFIERS: tuple[str, ...] = ("Ctrl", "Shift", "Alt", "Meta")
_MODIFIER_SET = frozenset(MODIFIERS)

#: Qt 一个快捷键最多 4 步和弦。
MAX_CHORDS = 4

#: PortableText 的和弦分隔符（固定为「逗号 + 空格」）。
CHORD_SEPARATOR = ", "


class ShortcutKind(str, Enum):
    """快捷键形态。只用于内省 / 文档 / 冲突提示，不参与匹配。"""

    SINGLE = "single"
    MODIFIED = "modified"
    CHORD = "chord"


def _is_modifier_only(chord: str) -> bool:
    """和弦是否只有修饰键（``Ctrl+Shift`` 这种不是可用的快捷键）。"""
    parts = chord.split("+")
    return bool(parts) and all(part in _MODIFIER_SET for part in parts)


def _raw_chord_steps(raw: str) -> int:
    """数一下原始文本里的和弦步数。

    PortableText 用逗号分隔和弦步，而「逗号键」写成 ``Ctrl+,`` 这种紧跟在
    ``+`` 后面的形式。所以：紧跟 ``+`` 的逗号算按键本身，其余逗号算分隔符。

    这个计数存在的唯一理由是提前发现「Qt 会静默截断」的超长和弦
    （``G, G, G, G, G`` 被 Qt 砍成 4 步，规范化后完全看不出少了东西）。
    真正的解析仍然交给 Qt。
    """
    steps = 1
    previous = ""
    for char in raw:
        if char == "," and previous != "+":
            steps += 1
        previous = char
    return steps


def canonical_sequence(raw: str) -> str:
    """把任意合法写法规范化成 PortableText；不合法时抛异常。

    ``QKeySequence`` 对拼不过的名字**不会抛异常**，而是静默返回空串
    （``Ctrl+Foo`` → ``""``）或截断出一个空和弦（``Ctrl+K, Foo`` →
    ``"Ctrl+K, "``）。这里必须把这两种静默失败转成
    :class:`InvalidKeySequence`，否则「配错的键位」会变成「悄悄不生效」。
    """
    text = raw.strip()
    if not text:
        raise InvalidKeySequence("empty key sequence")
    if _raw_chord_steps(text) > MAX_CHORDS:
        raise InvalidKeySequence(
            f"too many chord steps (max {MAX_CHORDS}): {raw!r}"
        )

    try:
        sequence = QKeySequence(text)
    except (TypeError, ValueError) as exc:  # pragma: no cover - 防御 Qt 版本差异
        raise InvalidKeySequence(f"unparsable key sequence: {raw!r}") from exc

    canonical = sequence.toString(
        QKeySequence.SequenceFormat.PortableText).strip()
    if not canonical:
        raise InvalidKeySequence(f"unknown key sequence: {raw!r}")

    chords = canonical.split(CHORD_SEPARATOR)
    if any(not chord.strip() for chord in chords):
        raise InvalidKeySequence(f"incomplete key sequence: {raw!r}")
    if len(chords) > MAX_CHORDS:
        raise InvalidKeySequence(
            f"too many chord steps (max {MAX_CHORDS}): {raw!r}"
        )
    for chord in chords:
        if _is_modifier_only(chord):
            raise InvalidKeySequence(
                f"modifier-only keys are not shortcuts: {chord!r} in {raw!r}"
            )
    return canonical


@dataclass(frozen=True, slots=True, order=True)
class KeySequence:
    """一个规范化的 Qt 快捷键序列（PortableText）。

    不要直接构造非规范文本：``KeySequence("ctrl+p")`` 会在
    ``__post_init__`` 里被拒绝。请用 :meth:`parse`（或
    :func:`parse_sequences` 处理配置值），它负责规范化。
    """

    text: str

    def __post_init__(self) -> None:
        if canonical_sequence(self.text) != self.text:
            raise InvalidKeySequence(
                f"not canonical: {self.text!r}; use KeySequence.parse()"
            )

    @classmethod
    def parse(cls, raw: str | KeySequence) -> KeySequence:
        """解析并规范化；已经是 ``KeySequence`` 时原样返回（幂等）。"""
        if isinstance(raw, KeySequence):
            return raw
        return cls(canonical_sequence(raw))

    @property
    def chords(self) -> tuple[str, ...]:
        """按步拆开后的和弦（单键序列只有一个元素）。"""
        return tuple(self.text.split(CHORD_SEPARATOR))

    @property
    def modifiers(self) -> frozenset[str]:
        """用到的修饰键集合（``Ctrl`` / ``Shift`` / ``Alt`` / ``Meta``）。"""
        found: set[str] = set()
        for chord in self.chords:
            for part in chord.split("+"):
                if part in _MODIFIER_SET:
                    found.add(part)
        return frozenset(found)

    @property
    def kind(self) -> ShortcutKind:
        """快捷键形态（单键 / 修饰键组合 / 多步和弦）。"""
        if len(self.chords) > 1:
            return ShortcutKind.CHORD
        if self.modifiers:
            return ShortcutKind.MODIFIED
        return ShortcutKind.SINGLE

    @property
    def is_single_key(self) -> bool:
        """是否是「无修饰键的单按键」（``F5`` / ``G``）。"""
        return self.kind is ShortcutKind.SINGLE

    def __str__(self) -> str:
        return self.text

    def __repr__(self) -> str:  # pragma: no cover - 调试可读性
        return f"KeySequence({self.text!r})"


def parse_sequence(raw: str | KeySequence) -> KeySequence:
    """模块级便捷入口，等价于 :meth:`KeySequence.parse`。"""
    return KeySequence.parse(raw)


def _parse_items(items: Iterable[str | KeySequence]) -> tuple[KeySequence, ...]:
    """逐项解析并去重（保持首次出现的顺序）。"""
    result: list[KeySequence] = []
    seen: set[KeySequence] = set()
    for item in items:
        if not isinstance(item, (str, KeySequence)):
            raise InvalidKeySequence(
                f"key sequence must be str or KeySequence, got {type(item).__name__}"
            )
        sequence = KeySequence.parse(item)
        if sequence in seen:
            continue
        seen.add(sequence)
        result.append(sequence)
    return tuple(result)


def parse_sequences(
        value: str | KeySequence | Iterable[str | KeySequence],
) -> tuple[KeySequence, ...]:
    """解析配置值 / 调用方传入的「一组快捷键」。

    接受的写法：

    - 单个字符串：``"Ctrl+N"``（其中 ``G, G`` 是一个和弦，不是两个快捷键）
    - JSON 数组字符串：``'["F11", "Ctrl+Shift+F"]'``
    - 空串 / 空数组：``""`` / ``"[]"`` → ``()``，表示**显式解绑**
    - 可迭代对象：元素是字符串或 :class:`KeySequence`

    去重且保持顺序：同一个键写两遍没有意义，也不应该让绑定表出现重复项。
    """
    if isinstance(value, KeySequence):
        return (value,)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ()
        if text.startswith("["):
            try:
                data = json.loads(text)
            except json.JSONDecodeError as exc:
                raise InvalidKeySequence(
                    f"invalid JSON list of key sequences: {value!r}"
                ) from exc
            if not isinstance(data, list) or any(
                    not isinstance(item, str) for item in data
            ):
                raise InvalidKeySequence(
                    "key sequence list must be a JSON array of strings: "
                    f"{value!r}"
                )
            return _parse_items(data)
        return _parse_items([text])
    return _parse_items(value)


def format_sequences(
        sequences: str | KeySequence | Iterable[str | KeySequence],
) -> str:
    """把一组快捷键写成配置文件里的 JSON 数组文本（规范化 + 去重）。"""
    if isinstance(sequences, (str, KeySequence)):
        sequences = [sequences]
    return json.dumps(
        [str(item) for item in _parse_items(sequences)],
        ensure_ascii=False,
    )
