"""只读视图模型（Qt-free），只依赖 ``KeymapStore`` 协议，可无 Qt 单测。"""
from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from .bindings import normalize_key
from .components import component_path, iter_components
from .model import Action, Binding
from .policy import conflicts as _conflicts
from .policy import validate_binding

__all__ = ["KeyRow", "KeymapStore", "KeymapViewModel", "OperationRow"]


class KeymapStore(Protocol):
    """ViewModel 依赖的最小接口（内核 ``Registry`` 满足它）。"""

    def iter_actions(self) -> Iterator[Action]: ...
    def get_action(self, action_id: str) -> Action | None: ...
    def effective_bindings(self) -> Mapping[str, tuple[Binding, ...]]: ...
    def default_bindings(self) -> Mapping[str, tuple[Binding, ...]]: ...
    def bindings(self, key: str) -> tuple[tuple[Action, Binding], ...]: ...
    def subscribe(self, listener: Callable[[], None]) -> Callable[[], None]: ...

    def add_operation(self, key: str, action_id: str, args: Any = None) -> Binding: ...
    def remove_operation(self, key: str, binding: Binding) -> bool: ...
    def update_operation(self, key: str, old: Binding, args: Any = None) -> Binding: ...
    def move_key(self, old: str, new: str) -> None: ...
    def remove_key(self, key: str) -> None: ...
    def reset_key(self, key: str) -> None: ...
    def reset_all(self) -> None: ...
    def set_enabled(self, enabled: bool) -> None: ...
    @property
    def enabled(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class OperationRow:
    """一个键上的一个操作（动作 + 本次参数）。"""

    action: Action
    args: tuple[tuple[str, Any], ...] = ()
    tokens: tuple[str, ...] = ()

    @property
    def action_id(self) -> str:
        return self.action.id

    @property
    def haystack(self) -> str:
        return " ".join(self.tokens)


@dataclass(frozen=True, slots=True)
class KeyRow:
    """设置页的一行：一个键 + 它上面的全部操作（按 seq 顺序）。"""

    key: str
    operations: tuple[OperationRow, ...]
    customized: bool
    tokens: tuple[str, ...] = ()

    @property
    def haystack(self) -> str:
        parts = [*self.tokens]
        for operation in self.operations:
            parts.extend(operation.tokens)
        return " ".join(parts)


def _words(*parts: Any) -> tuple[str, ...]:
    return tuple(" ".join(str(part) for part in parts if part).lower().split())


def _operation_tokens(row_args: tuple[tuple[str, Any], ...], action: Action) -> tuple[str, ...]:
    parts: list[Any] = [action.id, action.label, action.description,
                        action.component, *component_labels(action.component)]
    parts += [f"{name} {value}" for name, value in row_args]
    return _words(*parts)


def component_labels(component_id: str) -> list[str]:
    return [c.label for c in component_path(component_id)]


class KeymapViewModel:
    """从注册中心拉取快照；所有写操作原样转发给注册中心。"""

    def __init__(self, store: KeymapStore) -> None:
        self._store = store

    def snapshot(self) -> tuple[KeyRow, ...]:
        final = self._store.effective_bindings()
        defaults = self._store.default_bindings()
        ranks = {c.id: index for index, c in enumerate(iter_components())}
        rows: list[KeyRow] = []
        for key, bindings in final.items():
            operations = tuple(
                OperationRow(
                    action, binding.args,
                    _operation_tokens(binding.args, action),
                )
                for binding in bindings
                if (action := self._store.get_action(binding.action)) is not None
            )
            if not operations:
                continue
            rows.append(KeyRow(
                key=key,
                operations=operations,
                customized=tuple(bindings) != defaults.get(key, ()),
                tokens=_words(key),
            ))
        rows.sort(key=lambda row: (
            min(ranks.get(op.action.component, 10**6) for op in row.operations),
            min(op.action.seq for op in row.operations),
            row.key,
        ))
        return tuple(rows)

    def iter_actions(self) -> Iterator[Action]:
        return self._store.iter_actions()

    def get_action(self, action_id: str) -> Action | None:
        return self._store.get_action(action_id)

    def subscribe(self, listener: Callable[[], None]) -> Callable[[], None]:
        return self._store.subscribe(listener)

    @property
    def enabled(self) -> bool:
        return self._store.enabled

    def set_enabled(self, enabled: bool) -> None:
        self._store.set_enabled(enabled)

    def rebind_key(self, old: str, new: str) -> None:
        norm = normalize_key(new)
        for action, _binding in self._store.bindings(old):
            validate_binding(norm, action.id)
        self._store.move_key(old, norm)

    def add_operation(
            self, key: str, action_id: str, args: Mapping[str, Any] | None = None,
    ) -> Binding:
        norm = normalize_key(key)
        validate_binding(norm, action_id)
        return self._store.add_operation(norm, action_id, args)

    def remove_operation(self, key: str, binding: Binding | OperationRow) -> bool:
        if isinstance(binding, OperationRow):
            binding = Binding(binding.action_id, binding.args)
        return self._store.remove_operation(key, binding)

    def update_operation(
            self, key: str, old: Binding | OperationRow,
            args: Mapping[str, Any] | None,
    ) -> Binding:
        if isinstance(old, OperationRow):
            old = Binding(old.action_id, old.args)
        validate_binding(key, old.action)
        return self._store.update_operation(key, old, args)

    def conflicts(
            self, key: str, *, action_id: str | None = None,
    ) -> tuple[Action, ...]:
        """``key`` 上已占用的动作（给设置页的「会一起触发」提示用）。"""
        return _conflicts(self._store, key, action_id=action_id)

    def remove_key(self, key: str) -> None:
        self._store.remove_key(key)

    def reset_key(self, key: str) -> None:
        self._store.reset_key(key)

    def reset_all(self) -> None:
        self._store.reset_all()
