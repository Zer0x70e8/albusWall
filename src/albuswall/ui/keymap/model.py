#
"""快捷键槽（slot）与绑定记录。

绑定表是「按键 → 命令」；快捷键槽是「命令 → 处理函数」。一个槽就是一条
可被快捷键触发的功能，注册后可以：

- 被任意多个键位指向（一对多：``Ctrl+N`` 和 ``N`` 都开新建）；
- 触发时携带**固定实参**（位置参数 + 关键字参数，来自配置或装饰器声明）；
- 通过 :meth:`albuswall.ui.keymap.ShortcutRegistry.register_commands` 变成
  ``albuswall.command`` 里的一条真实命令（``func`` 就是这个处理函数）。

槽用 ``id`` 唯一标识，推荐直接写成命令路径（如 ``"view.fullscreen"``）——
这样绑定表里的 key、命令注册表里的 path、槽的 id 天然是同一个名字，不需要
第三套映射。

一个槽的**绑定记录**是 :class:`ShortcutBinding`：键位元组 + 位置参数元组 +
关键字参数字典。配置文件的写法就是这三样::

    slot_name = ["Alt+1", [1], {"mode": "x"}]

（第一个元素是快捷键：单个写字符串，多个写字符串数组。）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from albuswall.command import Handler

from .sequence import KeySequence, parse_sequences

__all__ = ["ShortcutBinding", "ShortcutSlot"]


@dataclass(frozen=True, slots=True)
class ShortcutBinding:
    """一个槽的完整绑定记录：键位 + 触发时传入的实参。

    Attributes:
        keys: 会触发该槽的键位（可以是 0 个 —— 例如用户显式解绑）。
        args: 触发时按位置传入的实参。
        kwargs: 触发时按关键字传入的实参。
    """

    keys: tuple[KeySequence, ...] = ()
    args: tuple[Any, ...] = ()
    kwargs: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.keys, tuple):
            object.__setattr__(self, "keys", tuple(self.keys))
        if not isinstance(self.args, tuple):
            object.__setattr__(self, "args", tuple(self.args))
        if not isinstance(self.kwargs, Mapping):
            raise TypeError(
                f"ShortcutBinding.kwargs must be a mapping, "
                f"got {type(self.kwargs).__name__}"
            )
        for name in self.kwargs:
            if not isinstance(name, str):
                raise TypeError(
                    f"ShortcutBinding.kwargs keys must be strings: {name!r}"
                )

    @property
    def enabled(self) -> bool:
        """是否至少有一个键位。没有键位 = 显式解绑（但槽仍然存在）。"""
        return bool(self.keys)


@dataclass(frozen=True, slots=True)
class ShortcutSlot:
    """一条快捷键功能。

    Attributes:
        id: 槽 id，同时作为命令路径（``"view.fullscreen"``）。
        handler: 触发时调用的函数；签名与命令 handler 一致。
        help: 一句话说明（菜单 / 设置页展示用）。
        default_keys: **代码内兜底键位**。仅当配置文件里没有这个命令的条目时
            才生效；随包分发的默认表（``resources/config/shortcuts.ini``）
            优先于它。插件新增的命令可以靠它「开箱即用」，不必改资源文件。
        default_args: 兜底位置参数（配置文件没写参数时生效）。
        default_kwargs: 兜底关键字参数（配置文件没写参数时生效）。
    """

    id: str
    handler: Handler
    help: str = ""
    default_keys: tuple[KeySequence, ...] = ()
    default_args: tuple[Any, ...] = ()
    default_kwargs: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError("ShortcutSlot.id must be a non-empty string")
        if not callable(self.handler):
            raise TypeError(
                f"ShortcutSlot {self.id!r}: handler must be callable"
            )
        if not isinstance(self.default_keys, tuple) or any(
                not isinstance(key, KeySequence) for key in self.default_keys
        ):
            object.__setattr__(
                self, "default_keys", parse_sequences(self.default_keys))
        if not isinstance(self.default_args, tuple):
            object.__setattr__(self, "default_args", tuple(self.default_args))
        if not isinstance(self.default_kwargs, Mapping):
            raise TypeError(
                f"ShortcutSlot {self.id!r}: default_kwargs must be a mapping"
            )

    @property
    def default_binding(self) -> ShortcutBinding:
        """装饰器声明的兜底绑定（键位 + 参数）。"""
        return ShortcutBinding(
            keys=self.default_keys,
            args=self.default_args,
            kwargs=self.default_kwargs,
        )

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """直接调用槽（等价于 ``slot.handler(*args, **kwargs)``）。

        参数由调用方显式给出；「配置里固定的实参」走
        :meth:`ShortcutRegistry.invoke`。
        """
        return self.handler(*args, **kwargs)
