#
"""按键身份：一个物理键 = 一个枚举成员，成员上挂着它的**全部写法**。

为什么需要这一层
================

Qt 的 PortableText 对**同一个物理键**有多种写法，而「配置文件里怎么写」与
「按键事件转出来是什么」并不总是一致：

===============  =========================  ============================
物理按键          常见写法                    ``QKeyEvent`` 转出的文本
===============  =========================  ============================
``Shift+Tab``     ``Shift+Tab`` / ``Backtab``  ``Shift+Backtab``
主回车键          ``Enter``                  ``Return``
小键盘回车        ``Enter``                  ``Enter``
``Delete``        ``Delete`` / ``Del``        ``Del``
``Insert``        ``Insert`` / ``Ins``        ``Ins``
``Escape``        ``Escape`` / ``Esc``        ``Esc``
===============  =========================  ============================

所以这里**不做文本归一**：``KeySequence`` 存什么就是什么（配置里写 ``Delete``
就仍是 ``Delete``，``keys_for()`` 原样回显），匹配时把两侧都解析成
:class:`KeyAlias` **身份**再比——一个成员 ``aliases`` 里出现过的任何写法，
都算同一个键（``Shift+Tab`` == ``Shift+Backtab`` == ``Backtab``）。

用法
====

.. code-block:: python

    >>> KeyAlias.BACKTAB.aliases
    ('Backtab', 'Shift+Tab', 'Shift+Backtab')
    >>> KeyAlias.of("Shift+Tab") is KeyAlias.BACKTAB
    True
    >>> step_identity("Ctrl+Shift+Tab")
    (frozenset({'Ctrl', 'Shift'}), <KeyAlias.BACKTAB: 'Backtab'>)
    >>> step_identity("Ctrl+F")          # 没有别名的按键：身份就是键名
    (frozenset({'Ctrl'}), 'F')

身份的形状
==========

``(修饰键集合, KeyAlias | str)``：修饰键是 **frozenset**（顺序无意义），
按键是 :class:`KeyAlias` 成员（有别名）或原始键名（``F11`` / ``A`` / ``+``
这类没有别名的）。
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .sequence import KeySequence

__all__ = [
    "NAVIGATION_KEYS",
    "WIDGET_KEYS",
    "KeyAlias",
    "is_navigation_step",
    "is_widget_step",
    "key_alias_of",
    "sequence_identity",
    "split_step",
    "step_identity",
]


class KeyAlias(str, Enum):
    """同一个物理键在 PortableText 里的多种写法（一个成员 = 一个身份）。

    成员值取 Qt 自己的规范写法；全部可接受写法见 :attr:`aliases`。
    只收录**真的有两种以上写法**的按键，其余按键的身份就是它的键名
    （``F11`` / ``A`` / ``+`` ……），不进枚举——枚举的价值就在「别名」。
    """

    ESCAPE = "Esc"
    TAB = "Tab"
    BACKTAB = "Backtab"
    RETURN = "Return"
    SPACE = "Space"
    DELETE = "Del"
    INSERT = "Ins"
    HOME = "Home"
    END = "End"
    PAGE_UP = "PgUp"
    PAGE_DOWN = "PgDown"
    LEFT = "Left"
    RIGHT = "Right"
    UP = "Up"
    DOWN = "Down"
    MENU = "Menu"
    PRINT = "Print"
    SYSREQ = "SysReq"

    @property
    def aliases(self) -> tuple[str, ...]:
        """这个键的全部写法（纯键名 + 带修饰键的写法，含成员值本身）。"""
        return _ALIASES_BY_KEY[self]

    @classmethod
    def of(cls, text: str) -> KeyAlias | None:
        """任意写法（``Delete`` / ``Shift+Tab`` …）→ 成员；无别名的键返回 ``None``。"""
        return key_alias_of(text)


#: 键名 → 身份。键是**不带修饰键**的写法，值是对应的枚举成员。
_ALIAS_TO_KEY: dict[str, KeyAlias] = {
    "Esc": KeyAlias.ESCAPE,
    "Escape": KeyAlias.ESCAPE,
    "Tab": KeyAlias.TAB,
    "Backtab": KeyAlias.BACKTAB,
    "Return": KeyAlias.RETURN,
    "Enter": KeyAlias.RETURN,
    "Space": KeyAlias.SPACE,
    "Del": KeyAlias.DELETE,
    "Delete": KeyAlias.DELETE,
    "Ins": KeyAlias.INSERT,
    "Insert": KeyAlias.INSERT,
    "Home": KeyAlias.HOME,
    "End": KeyAlias.END,
    "PgUp": KeyAlias.PAGE_UP,
    "PageUp": KeyAlias.PAGE_UP,
    "PgDown": KeyAlias.PAGE_DOWN,
    "PageDown": KeyAlias.PAGE_DOWN,
    "Left": KeyAlias.LEFT,
    "Right": KeyAlias.RIGHT,
    "Up": KeyAlias.UP,
    "Down": KeyAlias.DOWN,
    "Menu": KeyAlias.MENU,
    "Print": KeyAlias.PRINT,
    "SysReq": KeyAlias.SYSREQ,
}

#: 带修饰键的**完整写法** → 身份。
#:
#: 这是「别名」最需要枚举的地方：Qt 把物理的 ``Shift+Tab`` 当成 ``Backtab``
#: 键，事件转出来是 ``Shift+Backtab``；配置文件里却常写 ``Shift+Tab``。
#: 三种写法在这里收成同一个身份。
_COMPOSITE_ALIASES: dict[str, tuple[frozenset[str], KeyAlias]] = {
    "Backtab": (frozenset({"Shift"}), KeyAlias.BACKTAB),
    "Shift+Tab": (frozenset({"Shift"}), KeyAlias.BACKTAB),
    "Shift+Backtab": (frozenset({"Shift"}), KeyAlias.BACKTAB),
}

#: 成员 → 它的全部写法（纯键名在前，完整写法在后，去重保序）。
_ALIASES_BY_KEY: dict[KeyAlias, tuple[str, ...]] = {
    member: tuple(dict.fromkeys(
        [name for name, mapped in _ALIAS_TO_KEY.items() if mapped is member]
        + [spelling for spelling, (_, mapped) in _COMPOSITE_ALIASES.items()
           if mapped is member]
    ))
    for member in KeyAlias
}

#: Qt PortableText 认的修饰键名（也是规范顺序）。
_MODIFIERS: tuple[str, ...] = ("Ctrl", "Shift", "Alt", "Meta")

#: 永远交给控件自己处理的键：焦点 / 光标 / 取消。
#:
#: 即使配置文件里把它们绑成了快捷键，也是**告警并忽略**——Tab 与方向键是
#: Qt 的焦点链，Esc 是窗口自己的层级（``Window.keyPressEvent``），抢过来
#: 会让整个键盘导航失效。
NAVIGATION_KEYS: frozenset[KeyAlias] = frozenset({
    KeyAlias.TAB,
    KeyAlias.BACKTAB,
    KeyAlias.LEFT,
    KeyAlias.RIGHT,
    KeyAlias.UP,
    KeyAlias.DOWN,
    KeyAlias.HOME,
    KeyAlias.END,
    KeyAlias.PAGE_UP,
    KeyAlias.PAGE_DOWN,
    KeyAlias.ESCAPE,
})

#: 控件自己的「激活 / 预览」键：焦点在 ``keymap_passthrough`` 控件或文本
#: 输入上时让渡出去（按钮点空格、网格快速预览、文本输入）。
WIDGET_KEYS: frozenset[KeyAlias] = frozenset({
    KeyAlias.SPACE,
    KeyAlias.RETURN,
})


def split_step(step: str) -> tuple[tuple[str, ...], str]:
    """把一步按键拆成 ``(修饰键元组, 键名)``。

    ``Ctrl+Shift+P`` → ``(("Ctrl", "Shift"), "P")``；
    ``Ctrl++`` → ``(("Ctrl",), "+")``（``+`` 本身就是那个键）；
    ``Ctrl+,`` → ``(("Ctrl",), ",")``。
    """
    parts = step.split("+")
    modifiers: list[str] = []
    index = 0
    while index < len(parts) and parts[index] in _MODIFIERS:
        modifiers.append(parts[index])
        index += 1
    return tuple(modifiers), "+".join(parts[index:])


def key_alias_of(step: str) -> KeyAlias | None:
    """一步按键（任意写法）→ :class:`KeyAlias`；没有别名时 ``None``。"""
    composite = _COMPOSITE_ALIASES.get(step.strip())
    if composite is not None:
        return composite[1]
    _, name = split_step(step)
    return _ALIAS_TO_KEY.get(name)


def step_identity(step: str) -> tuple[frozenset[str], KeyAlias | str]:
    """一步按键的**身份**：``(修饰键集合, KeyAlias | 键名)``。

    ``Shift+Tab`` / ``Shift+Backtab`` / ``Backtab`` 三者同一身份：
    Qt 把「Shift+Tab」当成 ``Backtab`` 键，物理按键与配置写法必须能对上。
    """
    composite = _COMPOSITE_ALIASES.get(step.strip())
    if composite is not None:
        return composite

    modifiers, name = split_step(step)
    mods = set(modifiers)
    alias = _ALIAS_TO_KEY.get(name)
    if alias is KeyAlias.BACKTAB:
        # Qt 的 Backtab 就是 Shift+Tab：补上 Shift，三种写法才对得上
        mods.add("Shift")
    elif alias is KeyAlias.TAB and "Shift" in mods:
        # 「Tab + Shift」与「Backtab」是同一个物理键
        alias = KeyAlias.BACKTAB
    key: KeyAlias | str = name if alias is None else alias
    return frozenset(mods), key


def sequence_identity(
        sequence: KeySequence,
) -> tuple[tuple[frozenset[str], KeyAlias | str], ...]:
    """整条序列（可能带和弦）的身份元组；和弦逐步给出。"""
    return tuple(step_identity(step) for step in sequence.chords)


def is_navigation_step(step: str) -> bool:
    """这一步是否是「永远归控件」的导航 / 取消键。"""
    return key_alias_of(step) in NAVIGATION_KEYS


def is_widget_step(step: str) -> bool:
    """这一步是否是控件的「激活 / 预览」键（空格 / 回车）。"""
    return key_alias_of(step) in WIDGET_KEYS
