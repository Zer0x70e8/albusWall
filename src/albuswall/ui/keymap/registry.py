#
"""快捷键绑定表 + 快捷键槽注册表。

这一层只做**注册**：解析键位、登记槽、建立「键位 ⇄ 命令」映射、读写配置。
本模块不 import 任何 ``QtWidgets``，因此在没有 ``QApplication`` 的进程里
也能安全构造。

两个方向的映射都支持**一对多**：

- 一个键位 → 多个命令：``bind("Ctrl+W", "window.close", "viewer.close")``
- 一个命令 → 多个键位：``bind_command("app.quit", ["Ctrl+Q", "Ctrl+Shift+Q"])``

内部只维护 ``键位 → 命令列表`` 一张表，反向索引按需派生（:meth:`keys_for`），
避免两份索引在 unbind / replace 时写漏。

装载顺序（:meth:`load`）：清空 → 默认表 → 用户表逐条覆盖 → 槽声明的兜底
键位补位。三层优先级从高到低：

1. 用户表（``<user_config_dir>/shortcuts.ini``）
2. 随包默认表（``resources/config/shortcuts.ini``）
3. 槽装饰器里的 ``keys=`` / ``args=`` / ``kwargs=``（插件自带的
   「开箱即用」键位与固定实参）
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from albuswall.command import Command, CommandRegistry, Handler

from .config import ShortcutConfig
from .errors import DuplicateShortcutSlot
from .model import ShortcutBinding, ShortcutSlot
from .sequence import KeySequence, parse_sequences

__all__ = [
    "ShortcutRegistry",
    "bind",
    "bind_command",
    "get_default_registry",
    "load_shortcuts",
    "registry_shortcut_registry",
    "shortcut_slot",
]

logger = logging.getLogger(__package__)

#: 键位入参：字符串（单键 / 和弦 / JSON 数组）、KeySequence、或它们的可迭代。
KeyInput = str | KeySequence | Iterable[str | KeySequence]

#: 「没传这个参数」的哨兵：``bind_command`` 里区分「清空」和「不改动」。
_UNSET: Any = object()


def _coerce_keys(keys: KeyInput) -> tuple[KeySequence, ...]:
    """把各种入参统一成规范化后的键位元组。

    ``"G, G"`` 在这里是一个**和弦**（PortableText 语义），不是两个快捷键；
    要传多个键位请用可迭代对象或 JSON 数组字符串。
    """
    if isinstance(keys, KeySequence):
        return (keys,)
    if isinstance(keys, str):
        return parse_sequences(keys)
    return parse_sequences(list(keys))


def _validate_command(command: str) -> str:
    if not isinstance(command, str) or not command.strip():
        raise ValueError(f"command name must be a non-empty string: {command!r}")
    return command


class ShortcutRegistry:
    """快捷键槽 + 键位绑定表的注册中心。

    生命周期分两段，互不干扰：

    - **导入期**：``@shortcut_slot(...)`` 登记槽、``bind(...)`` 登记兜底绑定；
    - **启动期**：``load(config)`` 从配置文件整表重载，导期绑定会被这里清空
      重建（配置文件才是键位的唯一事实来源）。

    Args:
        command_registry: 可选。给出时，新登记的槽会**同时**作为命令注册进
            ``albuswall.command``，于是 CLI / 菜单 / 插件看到的是同一套命令。
    """

    def __init__(
            self,
            *,
            command_registry: CommandRegistry | None = None,
    ) -> None:
        self._slots: dict[str, ShortcutSlot] = {}
        self._table: dict[KeySequence, list[str]] = {}
        self._params: dict[str, tuple[tuple[Any, ...], dict[str, Any]]] = {}
        #: 最近一次 ``load()`` 从配置文件读到的命令（用于持久化时区分
        #: 「从没加载过」和「加载后被显式解绑」，见 :meth:`persist`）。
        self._loaded: set[str] = set()
        self._command_registry = command_registry
        self._config: ShortcutConfig | None = None

    # ==================================================================
    # 快捷键槽
    # ==================================================================

    def register_slot(
            self,
            slot: ShortcutSlot,
            *,
            replace: bool = False,
    ) -> ShortcutSlot:
        """登记一个槽。

        Args:
            slot: 槽对象（``id`` 唯一）。
            replace: ``True`` 时覆盖同名槽；默认重复登记直接报错，避免
                「两个插件抢同一个命令名」被静默吞掉。

        Raises:
            DuplicateShortcutSlot: 同名槽已存在且 ``replace=False``，或
                该命令在 ``CommandRegistry`` 里已被别的 handler 占用。
        """
        if slot.id in self._slots and not replace:
            raise DuplicateShortcutSlot(
                f"shortcut slot already registered: {slot.id!r}"
            )
        self._slots[slot.id] = slot
        # 装饰器声明的兜底实参立刻生效；配置文件里有该命令时由 load() 覆盖。
        self._params[slot.id] = (
            slot.default_args, dict(slot.default_kwargs),
        )
        self._register_as_command(slot)
        return slot

    def slot(
            self,
            slot_id: str,
            *,
            help_: str = "",
            keys: KeyInput = (),
            args: Iterable[Any] = (),
            kwargs: Mapping[str, Any] | None = None,
            replace: bool = False,
    ) -> Callable[[Handler], Handler]:
        """装饰器形式的 :meth:`register_slot`。

        .. code-block:: python

            shortcuts = ShortcutRegistry()

            @shortcuts.slot("view.fullscreen", help_="全屏", keys=["F11"])
            def toggle_fullscreen() -> None:
                ...

            @shortcuts.slot("album.open", keys="Alt+1", args=(1,))
            def open_album(index: int) -> None:
                ...

        ``keys`` / ``args`` / ``kwargs`` 都是**兜底值**：配置文件里没有这个
        命令时才生效（见模块 docstring 的三层优先级）。装饰器返回原函数，
        不做包装，因此别的代码仍可直接调它。
        """
        default_keys = _coerce_keys(keys)
        default_args = tuple(args)
        default_kwargs = dict(kwargs or {})

        def decorator(handler: Handler) -> Handler:
            self.register_slot(
                ShortcutSlot(
                    id=slot_id,
                    handler=handler,
                    help=help_,
                    default_keys=default_keys,
                    default_args=default_args,
                    default_kwargs=default_kwargs,
                ),
                replace=replace,
            )
            return handler

        return decorator

    def get_slot(self, slot_id: str) -> ShortcutSlot | None:
        return self._slots.get(slot_id)

    def has_slot(self, slot_id: str) -> bool:
        return slot_id in self._slots

    def slots(self) -> tuple[ShortcutSlot, ...]:
        """按登记顺序返回全部槽。"""
        return tuple(self._slots.values())

    def slot_ids(self) -> tuple[str, ...]:
        return tuple(self._slots)

    def command_handler(self, slot_id: str) -> Handler:
        """取槽的处理函数；未登记时抛 ``KeyError``（便于命令层直接调用）。"""
        try:
            return self._slots[slot_id].handler
        except KeyError:
            raise KeyError(f"no such shortcut slot: {slot_id!r}") from None

    def default_keys_for(self, command: str) -> tuple[KeySequence, ...]:
        """槽装饰器里声明的兜底键位；不是槽 / 没写 ``keys`` 时返回 ``()``。"""
        slot = self._slots.get(command)
        return slot.default_keys if slot is not None else ()

    def default_binding_for(self, command: str) -> ShortcutBinding:
        """槽装饰器声明的兜底绑定；不是槽时返回空记录。"""
        slot = self._slots.get(command)
        return slot.default_binding if slot is not None else ShortcutBinding()

    # ==================================================================
    # 绑定表
    # ==================================================================

    def bind(
            self,
            keys: KeyInput,
            *commands: str,
            args: Iterable[Any] = _UNSET,
            kwargs: Mapping[str, Any] | None = _UNSET,
            replace: bool = False,
    ) -> tuple[KeySequence, ...]:
        """把一组键位绑到若干命令上（一个键位 → 多个命令）。

        返回规范化后的键位元组，方便调用方回显「实际生效的写法」。
        ``replace=True`` 时该键位原有的命令列表被整体替换，否则追加。

        显式给出 ``args`` / ``kwargs`` 时，同样的实参会写给这次涉及的每个
        命令（不同命令要不同实参时，请对这命令单独调 :meth:`bind_command`）。
        """
        if not commands:
            raise ValueError("bind: at least one command is required")
        targets = [_validate_command(command) for command in commands]
        sequences = _coerce_keys(keys)
        if not sequences:
            raise ValueError("bind: key sequence must not be empty")

        for sequence in sequences:
            if replace:
                self._table[sequence] = list(dict.fromkeys(targets))
                continue
            bucket = self._table.setdefault(sequence, [])
            for command in targets:
                if command not in bucket:
                    bucket.append(command)
        if args is not _UNSET or kwargs is not _UNSET:
            for command in targets:
                self._set_params(command, args, kwargs)
        return sequences

    def bind_command(
            self,
            command: str,
            keys: KeyInput,
            *,
            args: Iterable[Any] = _UNSET,
            kwargs: Mapping[str, Any] | None = _UNSET,
            replace: bool = True,
    ) -> tuple[KeySequence, ...]:
        """把若干键位绑到同一个命令上（一个命令 → 多个键位）。

        这是配置文件的方向（``命令 = [快捷键, 位置参数, 关键字参数]``）。
        ``replace=True``（默认）先清掉该命令原有的全部键位，再按 ``keys``
        重建；``replace=False`` 则做并集追加。

        ``args`` / ``kwargs`` 是**触发时传给槽的固定实参**，与键位相互独立：
        不传就保留原值（新命令则是空），传了就整体替换这一项。
        """
        target = _validate_command(command)
        sequences = _coerce_keys(keys)
        if replace:
            self.unbind_command(target)
        for sequence in sequences:
            bucket = self._table.setdefault(sequence, [])
            if target not in bucket:
                bucket.append(target)
        if args is not _UNSET or kwargs is not _UNSET:
            self._set_params(target, args, kwargs)
        return sequences

    def bind_mapping(
            self,
            mapping: Mapping[str, ShortcutBinding | KeyInput],
            *,
            replace: bool = True,
    ) -> None:
        """批量 ``bind_command``（配置加载 / 热重载用）。

        值可以是完整 :class:`ShortcutBinding`，也可以只给键位（自动补空参数）。
        """
        for command, value in mapping.items():
            binding = (
                value if isinstance(value, ShortcutBinding)
                else ShortcutBinding(keys=_coerce_keys(value))
            )
            self.bind_command(
                command,
                binding.keys,
                args=binding.args,
                kwargs=binding.kwargs,
                replace=replace,
            )

    def unbind(
            self,
            keys: KeyInput,
            command: str | None = None,
    ) -> None:
        """解除绑定。

        ``command=None`` 时整个键位解绑；给出 ``command`` 时只把它从该键位的
        命令列表里摘掉（列表空了就删键位）。
        """
        for sequence in _coerce_keys(keys):
            bucket = self._table.get(sequence)
            if bucket is None:
                continue
            if command is None:
                del self._table[sequence]
                continue
            target = _validate_command(command)
            if target in bucket:
                bucket.remove(target)
            if not bucket:
                del self._table[sequence]

    def unbind_command(self, command: str) -> None:
        """把一个命令从所有键位上摘掉。"""
        target = _validate_command(command)
        for sequence in [
            seq for seq, bucket in self._table.items() if target in bucket
        ]:
            bucket = self._table[sequence]
            bucket.remove(target)
            if not bucket:
                del self._table[sequence]

    def clear_bindings(self) -> None:
        """清空绑定表与「触发实参」（槽本身不受影响）。"""
        self._table.clear()
        self._params.clear()

    def _set_params(
            self,
            command: str,
            args: Iterable[Any] | object,
            kwargs: Mapping[str, Any] | None | object,
    ) -> None:
        """设置触发实参；``_UNSET`` 表示「这一项不改」。

        校验放在这里而不是 ``bind`` 里：空参数也要能写，所以不能用
        ``args is None`` 之类的真假判断区分「没传」。
        """
        target = _validate_command(command)
        current_args, current_kwargs = self._params.get(target, ((), {}))

        new_args = current_args
        if args is not _UNSET:
            try:
                new_args = tuple(args)  # type: ignore[arg-type]
            except TypeError as exc:
                raise ValueError(
                    f"{target!r}: positional args must be iterable, "
                    f"got {type(args).__name__}"
                ) from exc

        new_kwargs = dict(current_kwargs)
        if kwargs is not _UNSET:
            if kwargs is None:
                new_kwargs = {}
            elif not isinstance(kwargs, Mapping):
                raise ValueError(
                    f"{target!r}: keyword args must be a mapping, "
                    f"got {type(kwargs).__name__}"
                )
            else:
                new_kwargs = dict(kwargs)
        for name in new_kwargs:
            if not isinstance(name, str):
                raise ValueError(
                    f"{target!r}: keyword arg names must be strings: {name!r}"
                )

        self._params[target] = (new_args, new_kwargs)

    # ==================================================================
    # 查询 / 触发
    # ==================================================================

    def commands_for(self, keys: str | KeySequence) -> tuple[str, ...]:
        """一个键位绑定的全部命令（一对多的「多」在这里）。"""
        sequence = KeySequence.parse(keys)
        return tuple(self._table.get(sequence, ()))

    def keys_for(self, command: str) -> tuple[KeySequence, ...]:
        """一个命令绑定的全部键位，顺序按绑定表首次出现顺序。"""
        target = _validate_command(command)
        return tuple(
            sequence for sequence, bucket in self._table.items()
            if target in bucket
        )

    def args_for(self, command: str) -> tuple[Any, ...]:
        """触发该命令时使用的固定位置参数。"""
        target = _validate_command(command)
        return self._params.get(target, ((), {}))[0]

    def kwargs_for(self, command: str) -> dict[str, Any]:
        """触发该命令时使用的固定关键字参数（返回副本）。"""
        target = _validate_command(command)
        return dict(self._params.get(target, ((), {}))[1])

    def binding_for(self, command: str) -> ShortcutBinding:
        """一个命令的完整绑定记录：键位 + 位置参数 + 关键字参数。"""
        target = _validate_command(command)
        return ShortcutBinding(
            keys=self.keys_for(target),
            args=self.args_for(target),
            kwargs=self.kwargs_for(target),
        )

    def set_call_args(
            self,
            command: str,
            args: Iterable[Any] = (),
            kwargs: Mapping[str, Any] | None = None,
    ) -> None:
        """设置触发该命令时传入的固定实参（两项都整体替换）。"""
        self._set_params(command, args, kwargs)

    def invoke(self, command: str, *args: Any, **kwargs: Any) -> Any:
        """触发一个槽：用「配置里的固定实参 + 显式实参」调用处理函数。

        显式实参追加在固定位置参数之后，关键字参数覆盖同名键。槽未登记时
        抛 ``KeyError``（交不出处理函数就是编程错误，不该静默）。
        """
        target = _validate_command(command)
        slot = self._slots.get(target)
        if slot is None:
            raise KeyError(f"no such shortcut slot: {target!r}")
        base_args, base_kwargs = self._params.get(target, ((), {}))
        merged_kwargs = {**base_kwargs, **kwargs}
        return slot.handler(*base_args, *args, **merged_kwargs)

    def trigger(self, keys: str | KeySequence) -> tuple[Any, ...]:
        """按一下键位：触发它绑定的所有命令，返回各自的返回值。

        键位没有绑定任何命令时返回 ``()``；绑到了尚未注册的槽时记日志并跳过
        （命令可能由别的插件稍后注册，这里不该炸）。
        """
        sequence = KeySequence.parse(keys)
        results: list[Any] = []
        for command in self._table.get(sequence, ()):
            if command not in self._slots:
                logger.debug(
                    "shortcut %s -> unregistered command %r ignored",
                    sequence, command,
                )
                continue
            results.append(self.invoke(command))
        return tuple(results)

    def bindings(self) -> dict[KeySequence, tuple[str, ...]]:
        """整张绑定表的快照（键位 → 命令元组）。"""
        return {
            sequence: tuple(bucket)
            for sequence, bucket in self._table.items()
        }

    def commands(self) -> tuple[str, ...]:
        """绑定表里出现过的命令（去重，保持顺序）。"""
        seen: dict[str, None] = {}
        for bucket in self._table.values():
            for command in bucket:
                seen.setdefault(command, None)
        return tuple(seen)

    def conflicts(self) -> dict[KeySequence, tuple[str, ...]]:
        """一个键位绑了多个命令的条目（诊断用，不是错误）。

        「一键多命令」是合法能力（例如 ``Ctrl+W`` 同时关窗口和关详情），但大
        多数时候是手滑，所以提供这份报告给设置页 / 日志提示。
        """
        return {
            sequence: tuple(bucket)
            for sequence, bucket in self._table.items()
            if len(bucket) > 1
        }

    def unbound_slots(self) -> tuple[str, ...]:
        """还没有任何键位指过来的槽——通常是「忘了在默认表里加一行」。"""
        bound = set(self.commands())
        return tuple(
            slot_id for slot_id in self._slots if slot_id not in bound
        )

    # ==================================================================
    # 配置加载 / 持久化
    # ==================================================================

    def load(
            self,
            config: ShortcutConfig | None = None,
            *,
            strict: bool = True,
    ) -> ShortcutRegistry:
        """从配置文件整表重载绑定（会先清空当前绑定与实参）。

        每次调用都重新读盘（不做缓存），因此热改键位只要再调一次即可。
        槽声明的兜底绑定（``default_keys`` / ``default_args`` /
        ``default_kwargs``）只给「配置里没出现的命令」补位。
        """
        shortcut_config = config if config is not None else ShortcutConfig()
        table = shortcut_config.load(strict=strict)

        self.clear_bindings()
        for slot in self._slots.values():
            if slot.id not in table:
                table[slot.id] = slot.default_binding
        for command, binding in table.items():
            self.bind_command(
                command,
                binding.keys,
                args=binding.args,
                kwargs=binding.kwargs,
                replace=False,
            )

        self._loaded = set(table)
        self._config = shortcut_config
        logger.debug(
            "shortcuts loaded: %d bindings, %d slots",
            len(self._table), len(self._slots),
        )
        return self

    def persist(self, config: ShortcutConfig | None = None) -> Path | None:
        """把「与默认不同的条目」写回用户表。

        默认基线 = 随包默认表优先、槽声明的兜底绑定兜底；只有当前绑定记录
        （键位 + 位置参数 + 关键字参数）与基线不一致的命令才会落盘。因此
        「改回默认」再 ``persist`` 会把该条目从用户表里删掉，用户表永远是
        最小差异集。

        Returns:
            写入的文件路径；没有可写路径（``user_path=None``）时返回 ``None``。
        """
        shortcut_config = (
            config if config is not None
            else (self._config or ShortcutConfig())
        )
        baselines = shortcut_config.load_defaults(strict=False)

        overrides: dict[str, ShortcutBinding] = {}
        bound = set(self.commands())
        for command in self._all_commands():
            # 既不是 load 读到的、也没被本节登记绑定过的命令，当前"键位为空"
            # 只说明本进程还没接触它，不代表用户解绑；写回去会把默认表关掉。
            if command not in self._loaded and command not in bound:
                continue
            current = self.binding_for(command)
            baseline = baselines.get(command)
            if baseline is None:
                baseline = self.default_binding_for(command)
            if current != baseline:
                overrides[command] = current

        if shortcut_config.user_path is None:
            return None
        return shortcut_config.save(overrides)

    def _all_commands(self) -> tuple[str, ...]:
        """需要参与持久化比较的命令：槽 + 绑定表 + 上次 load 读到的命令。

        用 ``_loaded`` 而不是「默认表里的全部命令」是关键：没 ``load()`` 过
        就 ``persist()`` 时，绝不能把默认表里那些「本进程还没加载」的命令
        当成「被用户解绑」写回去（那会把默认键位整片关掉）。而 ``load()``
        读到的命令即使后来被解绑，也仍然在比较集合里，显式解绑才写得回去。
        """
        seen: dict[str, None] = {}
        for slot_id in self._slots:
            seen.setdefault(slot_id, None)
        for command in self.commands():
            seen.setdefault(command, None)
        for command in self._loaded:
            seen.setdefault(command, None)
        return tuple(seen)

    # ==================================================================
    # 与 command 层对接
    # ==================================================================

    def set_command_registry(
            self,
            command_registry: CommandRegistry | None,
    ) -> None:
        """设置（或清空）目标命令注册表，并把已登记的槽补注册进去。"""
        self._command_registry = command_registry
        if command_registry is None:
            return
        for slot in self._slots.values():
            self._register_as_command(slot)

    def register_commands(
            self,
            command_registry: CommandRegistry | None = None,
    ) -> tuple[str, ...]:
        """把全部槽注册成 ``CommandRegistry`` 里的命令，返回路径元组。

        已在注册表里、且 ``func`` 就是同一个处理函数时跳过（幂等）；
        被别的 handler 占用时抛 :class:`DuplicateShortcutSlot`。
        """
        registry = command_registry or self._command_registry
        if registry is None:
            raise ValueError("no command registry available")
        for slot in self._slots.values():
            self._register_as_command(slot, registry=registry)
        return tuple(self._slots)

    def _register_as_command(
            self,
            slot: ShortcutSlot,
            *,
            registry: CommandRegistry | None = None,
    ) -> None:
        target = registry if registry is not None else self._command_registry
        if target is None:
            return
        existing = target.get(slot.id)
        if existing is not None and existing.func is not None:
            if existing.func is slot.handler:
                return
            raise DuplicateShortcutSlot(
                f"command {slot.id!r} is already registered by another handler"
            )
        target.register(
            Command(path=slot.id, func=slot.handler, help=slot.help)
        )

    # ==================================================================
    # 生命周期
    # ==================================================================

    def clear(self) -> None:
        """清空槽、绑定、实参、命令注册表引用与配置引用（测试辅助）。"""
        self._slots.clear()
        self._table.clear()
        self._params.clear()
        self._loaded.clear()
        self._command_registry = None
        self._config = None

    def __len__(self) -> int:
        return len(self._table)

    def __repr__(self) -> str:  # pragma: no cover - 调试可读性
        return (
            f"<ShortcutRegistry slots={len(self._slots)} "
            f"bindings={len(self._table)}>"
        )


# ======================================================================
# 默认实例 + 模块级便捷入口
# ======================================================================

_default = ShortcutRegistry()


def get_default_registry() -> ShortcutRegistry:
    """进程级默认注册表（与 ``command.get_default_registry`` 风格一致）。"""
    return _default


def shortcut_slot(
        slot_id: str,
        *,
        help_: str = "",
        keys: KeyInput = (),
        args: Iterable[Any] = (),
        kwargs: Mapping[str, Any] | None = None,
        replace: bool = False,
) -> Callable[[Handler], Handler]:
    """模块级装饰器：``_default.slot(...)`` 的简写。"""
    return _default.slot(
        slot_id,
        help_=help_,
        keys=keys,
        args=args,
        kwargs=kwargs,
        replace=replace,
    )


def bind(
        keys: KeyInput,
        *commands: str,
        replace: bool = False,
) -> tuple[KeySequence, ...]:
    """模块级便捷入口：``_default.bind(...)``。"""
    return _default.bind(keys, *commands, replace=replace)


def bind_command(
        command: str,
        keys: KeyInput,
        *,
        replace: bool = True,
) -> tuple[KeySequence, ...]:
    """模块级便捷入口：``_default.bind_command(...)``。"""
    return _default.bind_command(command, keys, replace=replace)


def load_shortcuts(
        config: ShortcutConfig | None = None,
        *,
        registry: ShortcutRegistry | None = None,
        strict: bool = True,
) -> ShortcutRegistry:
    """从配置文件整表重载（默认操作进程级注册表）。"""
    target = registry if registry is not None else _default
    return target.load(config, strict=strict)


def registry_shortcut_registry(container) -> None:
    """把进程级注册表注册进依赖容器（与 ``registry_*`` 系列同风格）。

    只是登记「容器里有 shortcuts」，**不**自动调 ``load()``：加载时机属于
    界面启动流程，由调用方显式 ``load_shortcuts()`` 决定。
    """
    container.register(
        "shortcuts", get_default_registry, returns=ShortcutRegistry,
    )
