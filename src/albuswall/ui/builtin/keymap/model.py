"""快捷键内核（Qt-free）：动作、绑定、组件、作用域、上下文、注册中心。

与 components / bindings / config / dispatch / viewmodel 一样，模块级不
import 任何 Qt。
"""
from __future__ import annotations

import contextlib
import inspect
import itertools
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Protocol

__all__ = [
    "Action", "Binding", "Component", "Context", "Registry", "Scope",
    "clear_actions", "register_action", "registry", "shortcut",
]


class Scope(Enum):
    """动作的生效范围。"""

    COMPONENT = "component"  # 需要焦点在本窗口（且输入框策略允许）
    GLOBAL = "global"        # 任何焦点下都生效（含输入框 / 其它窗口）


@dataclass(frozen=True, slots=True)
class Component:
    """组件树上的一个节点（静态数据，不参与运行期注册）。"""

    id: str
    label: str
    parent: str | None = None
    description: str = ""
    order: int = 100


@dataclass(frozen=True, slots=True)
class Context:
    """动作回调 / ``when`` 谓词拿到的上下文。"""

    window: Any = None
    container: Any = None
    presenters: Any = None
    focus: Any = None


def freeze_args(args: Mapping[str, Any] | None) -> tuple[tuple[str, Any], ...]:
    """``{name: value}`` → 可按名字排序、可比较的键值元组。"""
    return tuple(sorted((str(k), v) for k, v in (args or {}).items()))


@dataclass(frozen=True, slots=True)
class Binding:
    """一个键上的一个操作：动作 id + 这次触发携带的参数。"""

    action: str
    args: tuple[tuple[str, Any], ...] = ()

    @classmethod
    def build(cls, action: str, args: Mapping[str, Any] | None = None) -> Binding:
        action = str(action or "").strip()
        if not action:
            raise ValueError("binding needs a non-empty action id")
        return cls(action, freeze_args(args))

    @classmethod
    def from_raw(cls, raw: Any) -> Binding:
        """配置里的 ``"action_id"`` 或 ``{"action": …, "args": {…}}``。"""
        if isinstance(raw, str):
            return cls.build(raw)
        if isinstance(raw, Mapping):
            args = raw.get("args") or {}
            if not isinstance(args, Mapping):
                raise ValueError("binding args must be an object")
            return cls.build(str(raw.get("action") or ""), args)
        raise ValueError(f"invalid binding: {raw!r}")

    @property
    def arg_map(self) -> dict[str, Any]:
        return dict(self.args)

    def to_raw(self) -> str | dict[str, Any]:
        return self.action if not self.args else {
            "action": self.action, "args": self.arg_map,
        }


def _humanize(name: str) -> str:
    words = str(name or "").replace("_", " ").replace("-", " ").split()
    return " ".join(w[:1].upper() + w[1:] for w in words)


def _doc_summary(fn: Callable[..., Any]) -> str:
    first = (inspect.getdoc(fn) or "").split("\n\n", 1)[0]
    return " ".join(first.split())


def _signature(fn: Callable[..., Any]) -> inspect.Signature | None:
    try:
        return inspect.signature(fn)
    except (TypeError, ValueError):
        return None


def _takes_context(sig: inspect.Signature | None) -> bool:
    if sig is None:
        return False
    params = list(sig.parameters.values())
    return bool(params) and params[0].kind in (
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    )


def binding_signature(action: Action) -> inspect.Signature:
    """动作除 ``ctx`` 之外的参数签名（写入绑定时用它校验参数）。"""
    if action.signature is None:
        return inspect.Signature()
    params = list(action.signature.parameters.values())
    return inspect.Signature(params[1:] if action.takes_context else params)


@dataclass(frozen=True, slots=True)
class Action:
    """一条可绑定按键的动作声明（纯数据；展示文案在 ``ui.format``）。"""

    id: str
    label: str
    description: str
    keys: tuple[str, ...] = ()
    component: str = "app"
    scope: Scope = Scope.COMPONENT
    order: int = 100
    handler: Callable[..., Any] = field(default=None, compare=False)  # type: ignore[assignment]
    when: Callable[[Context], bool] | None = field(default=None, compare=False)
    allow_in_text_input: bool = False
    arguments: tuple[tuple[str, Any], ...] = ()
    #: 每个默认键各自的附加参数（``((键, ((参数名, 值), ...)), ...)``）。
    #: 用于「同一个动作、不同键带不同参数」的默认值，例如
    #: ``Shift+1..9`` → ``asset.move_to_slot`` 的各槽位。
    key_arguments: tuple[tuple[str, tuple[tuple[str, Any], ...]], ...] = ()
    signature: inspect.Signature | None = field(default=None, compare=False, repr=False)
    takes_context: bool = True
    seq: int = 0

    @property
    def default_args(self) -> dict[str, Any]:
        return dict(self.arguments)

    def arguments_for(self, key: str) -> tuple[tuple[str, Any], ...]:
        """某个默认键应携带的参数：动作参数 + 该键自己的参数。"""
        for name, extra in self.key_arguments:
            if name == key:
                merged = {**self.default_args, **dict(extra)}
                return freeze_args(merged)
        return self.arguments

    @property
    def is_global(self) -> bool:
        return self.scope is Scope.GLOBAL


_seq_counter = itertools.count(1)


class Registry:
    """内存注册中心：动作表 + 最终按键表；变更只从 :meth:`_notify` 广播。"""

    def __init__(self) -> None:
        self._actions: dict[str, Action] = {}
        self._overrides: dict[str, tuple[Binding, ...]] = {}
        self._listeners: list[Callable[[], None]] = []
        self._trigger_listeners: list[Callable[[str, Mapping[str, Any]], None]] = []
        self._enabled = True
        self._context = Context()
        self._focus_provider: Callable[[], Any] | None = None

    def register(self, action: Action) -> Action:
        if action.id in self._actions:
            raise ValueError(f"duplicate shortcut id: {action.id!r}")
        if not action.seq:
            action = replace(action, seq=next(_seq_counter))
        self._actions[action.id] = action
        self._notify()
        return action

    def unregister(self, action_id: str) -> None:
        if self._actions.pop(str(action_id), None) is not None:
            self._notify()

    def get_action(self, action_id: str) -> Action | None:
        return self._actions.get(str(action_id))

    def iter_actions(self) -> Iterator[Action]:
        return iter(sorted(self._actions.values(), key=lambda a: a.seq))

    def clear_actions(self) -> None:
        self._actions.clear()
        self._notify()

    def clear(self) -> None:
        self._actions.clear()
        self._overrides.clear()
        self._notify()

    def apply_bindings(self, bindings: Mapping[str, Iterable[Binding]]) -> list[str]:
        """从配置载入最终值；返回无法应用的 ``key:action`` 列表。"""
        from .bindings import normalize_key
        from .policy import validate_binding

        missing: list[str] = []
        for key, ops in bindings.items():
            try:
                norm = normalize_key(key)
            except ValueError:
                missing.append(str(key))
                continue
            kept: list[Binding] = []
            for binding in ops:
                action = self._actions.get(binding.action)
                if action is None:
                    missing.append(f"{norm}:{binding.action}")
                    continue
                try:
                    check_arguments(action, binding.args)
                    validate_binding(norm, binding.action)
                except ValueError:
                    missing.append(f"{norm}:{binding.action}")
                    continue
                kept.append(binding)
            if not kept and ops:
                continue  # 全是未知动作：保留默认键位，配置行原样不动
            self._overrides[norm] = _sorted(kept, self._actions)
        self._notify()
        return missing

    def default_bindings(self) -> dict[str, tuple[Binding, ...]]:
        table: dict[str, list[Binding]] = {}
        for action in self.iter_actions():
            for key in action.keys:
                table.setdefault(key, []).append(
                    Binding(action.id, action.arguments_for(key)))
        return {key: tuple(items) for key, items in table.items()}

    def effective_bindings(self) -> dict[str, tuple[Binding, ...]]:
        final = dict(self.default_bindings())
        for key, ops in self._overrides.items():
            if ops:
                final[key] = ops
            else:
                final.pop(key, None)
        cleaned: dict[str, tuple[Binding, ...]] = {}
        for key, ops in final.items():
            known = [b for b in ops if b.action in self._actions]
            if known:
                cleaned[key] = _sorted(known, self._actions)
        return dict(sorted(cleaned.items()))

    def bindings(self, key: str) -> tuple[tuple[Action, Binding], ...]:
        """某个键上的 ``(动作, 绑定)``，按动作登记顺序 ``seq``。"""
        return tuple(
            (action, binding)
            for binding in self.effective_bindings().get(key, ())
            if (action := self._actions.get(binding.action)) is not None)

    def add_operation(self, key: str, action_id: str, args: Mapping[str, Any] | None = None) -> Binding:
        binding = self._checked(action_id, args)
        ops = list(self.effective_bindings().get(key, ()))
        if binding in ops:
            return binding
        self._overrides[key] = _sorted([*ops, binding], self._actions)
        self._notify()
        return binding

    def remove_operation(self, key: str, binding: Binding) -> bool:
        ops = list(self.effective_bindings().get(key, ()))
        if binding not in ops:
            return False
        ops.remove(binding)
        self._overrides[key] = tuple(ops)
        self._notify()
        return True

    def update_operation(
            self, key: str, old: Binding, args: Mapping[str, Any] | None,
    ) -> Binding:
        new = self._checked(old.action, args)
        ops = list(self.effective_bindings().get(key, ()))
        if old in ops:
            ops[ops.index(old)] = new
        else:
            ops.append(new)
        self._overrides[key] = _sorted(ops, self._actions)
        self._notify()
        return new

    def move_key(self, old: str, new: str) -> None:
        old, new = str(old), str(new)
        if old == new:
            return
        moved = list(self.effective_bindings().get(old, ()))
        if not moved:
            return
        target = list(self.effective_bindings().get(new, ()))
        for binding in moved:
            if binding not in target:
                target.append(binding)
        self._overrides[new] = _sorted(target, self._actions)
        self._overrides[old] = ()
        self._notify()

    def remove_key(self, key: str) -> None:
        if key in self.effective_bindings():
            self._overrides[key] = ()
            self._notify()

    def reset_key(self, key: str) -> None:
        if self._overrides.pop(key, None) is not None:
            self._notify()

    def reset_action(self, action_id: str) -> None:
        defaults = self.default_bindings()
        current = self.effective_bindings()
        for key in set(defaults) | set(current):
            want = [b for b in current.get(key, ()) if b.action != action_id]
            want += [b for b in defaults.get(key, ()) if b.action == action_id]
            ops = _sorted(want, self._actions)
            if ops == defaults.get(key, ()):
                self._overrides.pop(key, None)
            elif ops:
                self._overrides[key] = ops
            else:
                self._overrides[key] = ()
        self._notify()

    def reset_all(self) -> None:
        if self._overrides:
            self._overrides.clear()
            self._notify()

    def invoke(self, action: Action, binding: Binding, ctx: Context) -> bool:
        args = binding.arg_map
        for listener in tuple(self._trigger_listeners):
            with contextlib.suppress(Exception):  # 订阅者失败不影响触发
                listener(action.id, args)
        try:
            return call_action(action, ctx, args) is not False
        except Exception:  # noqa: BLE001 - 单个动作失败不打断输入
            return False

    def dispatch(self, key: str, ctx: Context, allows: Callable[[Action], bool]) -> bool:
        """按 ``seq`` 依次执行该键上通过策略的动作；返回是否消费。"""
        if not self._enabled:
            return False
        handled = False
        for action, binding in self.bindings(key):
            if allows(action) and self.invoke(action, binding, ctx):
                handled = True
        return handled

    def trigger_action(self, action_id: str, **args: Any) -> bool:
        """程序化触发（不看 ``when`` 与输入框策略）。"""
        action = self.get_action(action_id)
        if action is None:
            return False
        try:
            check_arguments(action, args, allow_partial=False)
        except ValueError:
            return False
        return self.invoke(action, Binding(action.id, freeze_args(args)), self.context())

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        self._notify()

    def set_context(
            self, *,
            window: Any = None,
            container: Any = None,
            presenters: Any = None,
            focus: Any = None,
    ) -> None:
        self._context = Context(
            window if window is not None else self._context.window,
            container if container is not None else self._context.container,
            presenters if presenters is not None else self._context.presenters,
            focus if focus is not None else self._context.focus,
        )

    def set_focus_provider(self, provider: Callable[[], Any] | None) -> None:
        self._focus_provider = provider

    def context(self, *, window: Any = None, focus: Any = None) -> Context:
        base = self._context
        if focus is None and self._focus_provider is not None:
            try:
                focus = self._focus_provider()
            except Exception:  # noqa: BLE001
                focus = None
        return Context(
            window if window is not None else base.window,
            base.container, base.presenters,
            focus if focus is not None else base.focus,
        )

    def subscribe(self, listener: Callable[[], None]) -> Callable[[], None]:
        return _subscribe(self._listeners, listener)

    def subscribe_trigger(
            self, listener: Callable[[str, Mapping[str, Any]], None],
    ) -> Callable[[], None]:
        return _subscribe(self._trigger_listeners, listener)

    def _notify(self) -> None:
        for listener in tuple(self._listeners):
            with contextlib.suppress(Exception):  # 单个消费者失败不影响其它
                listener()

    def _checked(self, action_id: str, args: Mapping[str, Any] | None) -> Binding:
        action = self._actions.get(str(action_id))
        if action is None:
            raise ValueError(f"unknown shortcut action: {action_id!r}")
        check_arguments(action, args)
        return Binding.build(action.id, args)


def _subscribe(store: list[Any], listener: Any) -> Callable[[], None]:
    if listener not in store:
        store.append(listener)

    def unsubscribe() -> None:
        with contextlib.suppress(ValueError):
            store.remove(listener)

    return unsubscribe


def _sorted(bindings: Iterable[Binding], actions: Mapping[str, Action]) -> tuple[Binding, ...]:
    return tuple(sorted(
        bindings, key=lambda b: actions[b.action].seq if b.action in actions else 10**9))


def check_arguments(
        action: Action, args: Mapping[str, Any] | None, *, allow_partial: bool = True,
) -> None:
    """写入绑定时校验一次参数（标准库 ``bind_partial``，不建反射框架）。"""
    try:
        if allow_partial:
            binding_signature(action).bind_partial(**dict(args or {}))
        else:
            binding_signature(action).bind(**dict(args or {}))
    except TypeError as exc:
        raise ValueError(f"shortcut {action.id!r}: {exc}") from exc


def call_action(action: Action, ctx: Context, args: Mapping[str, Any]) -> Any:
    handler = action.handler
    if action.takes_context:
        return handler(ctx, **dict(args))
    return handler(**dict(args))


def register_action(action: Action) -> Action:
    """函数式注册（装饰器内部也走这里）。"""
    return registry.register(action)


def shortcut(
        action_id: str,
        *,
        keys: str | Iterable[str] | Mapping[str, Mapping[str, Any]] = (),
        label: str | None = None,
        description: str = "",
        component: str = "app",
        scope: str | Scope = Scope.COMPONENT,
        order: int = 100,
        when: Callable[[Context], bool] | None = None,
        allow_in_text_input: bool = False,
        arguments: Mapping[str, Any] | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """把一个函数登记成可绑定按键的动作。

    ``description`` 必填（缺失时退到函数 docstring 首段，都没有就抛
    ``ValueError``）。``arguments`` 是该动作默认绑定携带的参数。

    ``keys`` 可以是单个键、键列表，或 ``{键: 该键的附加参数}``：后者让
    同一个动作的多个默认键携带**不同**参数（例如评分 0–5、快捷槽位 1–9）。
    """

    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        from .bindings import normalize_key

        if not callable(fn):
            raise TypeError("@shortcut expects a callable")
        normalized: list[str] = []
        key_arguments: list[tuple[str, tuple[tuple[str, Any], ...]]] = []
        if isinstance(keys, str):
            raw_items: list[tuple[str, Mapping[str, Any]]] = (
                [(keys, {})] if keys else [])
        elif isinstance(keys, Mapping):
            raw_items = [(str(k), v or {}) for k, v in keys.items()]
        else:
            raw_items = [(str(k), {}) for k in keys]
        for raw_key, raw_args in raw_items:
            text = normalize_key(raw_key)
            if not text:
                continue
            if text not in normalized:
                normalized.append(text)
            if raw_args:
                key_arguments.append((text, freeze_args(raw_args)))

        summary = " ".join(str(description or "").split()) or _doc_summary(fn)
        if not summary:
            raise ValueError(
                f"@shortcut {action_id!r}: description is required "
                f"(pass description=... or write a docstring)")

        args = freeze_args(arguments)
        sig = _signature(fn)
        action = Action(
            id=str(action_id),
            label=label or _humanize(getattr(fn, "__name__", "action")),
            description=summary,
            keys=tuple(normalized),
            component=str(component or "app"),
            scope=Scope(scope),
            order=int(order),
            handler=fn,
            when=when,
            allow_in_text_input=bool(allow_in_text_input),
            arguments=args,
            key_arguments=tuple(key_arguments),
            signature=sig,
            takes_context=_takes_context(sig),
            seq=next(_seq_counter),
        )
        check_arguments(action, action.arguments)
        for key, extra in action.key_arguments:
            check_arguments(action, dict(action.arguments_for(key)))
        registry.register(action)
        with contextlib.suppress(AttributeError, TypeError):
            fn.__shortcut__ = action  # type: ignore[attr-defined]
        return fn

    return deco


class KeymapReader(Protocol):
    """内核 → UI 的只读视图（ViewModel 依赖它，可注入假实现单测）。"""

    def iter_actions(self) -> Iterator[Action]: ...
    def get_action(self, action_id: str) -> Action | None: ...
    def effective_bindings(self) -> Mapping[str, tuple[Binding, ...]]: ...
    def default_bindings(self) -> Mapping[str, tuple[Binding, ...]]: ...
    def subscribe(self, listener: Callable[[], None]) -> Callable[[], None]: ...


registry = Registry()


def clear_actions() -> None:
    """测试用：清空动作表（与 ``clear_components`` 对称）。"""
    registry.clear_actions()
