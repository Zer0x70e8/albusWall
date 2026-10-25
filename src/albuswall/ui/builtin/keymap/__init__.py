"""keymap 门面。

内核（model / components / bindings / config / dispatch / viewmodel，全部
Qt-free）→ Qt 绑定层（qt_binder / focus）→ 薄 model（ui.binding_model）→
View（ui.page）。模块级不 import Qt，Qt 名字由 ``__getattr__`` 延迟导入。
"""
from __future__ import annotations

from typing import Any

from . import components, policy
from .bindings import normalize_key
from .components import (
    COMPONENTS,
    Component,
    clear_components,
    get_component,
    register_component,
)
from .config import (
    SECTION,
    install,
    load_from_config,
    reset_action,
    reset_all,
    save_to_config,
)
from .dispatch import DispatchPolicy
from .model import (
    Action,
    Binding,
    Context,
    Registry,
    Scope,
    clear_actions,
    registry,
    shortcut,
)

__all__ = [
    "Action", "Binding", "Component", "Context", "DispatchPolicy", "Registry",
    "Scope", "components", "policy", "registry", "shortcut",
    "COMPONENTS", "clear_actions", "clear_components", "get_component",
    "register_component", "normalize_key",
    "SECTION", "install", "load_from_config", "save_to_config",
    "reset_action", "reset_all",
    "attach", "detach", "install_text_input_guard", "set_context",
    "set_enabled", "is_enabled", "subscribe", "subscribe_trigger",
    "trigger_action",
]

_QT_NAMES = frozenset({"attach", "detach", "install_text_input_guard"})


def __getattr__(name: str):
    if name in _QT_NAMES:
        from . import qt_binder

        return getattr(qt_binder, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def get_action(action_id: str) -> Action | None:
    return registry.get_action(action_id)


def set_context(**kwargs: Any) -> None:
    registry.set_context(**kwargs)


def trigger_action(action_id: str, **args: Any) -> bool:
    return registry.trigger_action(action_id, **args)


def is_enabled() -> bool:
    return registry.enabled


def set_enabled(enabled: bool) -> None:
    registry.set_enabled(enabled)


def subscribe(listener) -> Any:
    return registry.subscribe(listener)


def subscribe_trigger(listener) -> Any:
    return registry.subscribe_trigger(listener)
