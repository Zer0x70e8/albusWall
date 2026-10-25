"""绑定策略（Qt-free）：系统保留键、修饰键要求、冲突查询。

三条规则只在这里定义，设置页 / ViewModel 都通过本模块判断：

1. **系统保留键**：``Delete`` 永远是「移到回收站」，``Ctrl+Shift+Delete``
   永远是「清空回收站」；不能改绑到别的动作。
2. **带参数的移动动作**（``asset.move_to_album``）必须带 Ctrl / Alt / Meta，
   避免和单键浏览冲突。
3. **冲突检测**：一个键可以挂多个操作（既定模型），但设置页需要知道
   「这个键已经被谁占用」，由 :func:`conflicts` 提供只读查询。

策略只描述「不允许什么」，不负责展示；文案由 UI 层给。
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .bindings import normalize_key
from .model import Action

__all__ = [
    "MODIFIER_REQUIRED_ACTIONS", "RESERVED_KEYS", "conflicts",
    "reserved_action", "validate_binding",
]

#: 保留键 → 允许挂在上面的动作 id（空集合表示不允许任何动作）。
RESERVED_KEYS: Mapping[str, frozenset[str]] = {
    "delete": frozenset({"asset.trash"}),
    "ctrl+shift+delete": frozenset({"asset.empty_trash"}),
}

#: 这些动作绑到自定义键时必须带 Ctrl / Alt / Meta（Shift 不算）。
MODIFIER_REQUIRED_ACTIONS: frozenset[str] = frozenset({
    "asset.move_to_album",
})

_COMMAND_MODIFIERS = frozenset({"ctrl", "alt", "meta"})


def reserved_action(key: str) -> str | None:
    """保留键唯一的合法动作 id；不是保留键返回 ``None``。"""
    allowed = RESERVED_KEYS.get(normalize_key(key))
    if not allowed:
        return None
    return next(iter(allowed), None)


def _has_command_modifier(key: str) -> bool:
    parts = set(normalize_key(key).split("+"))
    return bool(parts & _COMMAND_MODIFIERS)


def validate_binding(key: str, action_id: str) -> None:
    """校验「键 + 动作」是否允许；不允许时抛 ``ValueError``（可展示的文案）。"""
    norm = normalize_key(key)
    allowed = RESERVED_KEYS.get(norm)
    if allowed is not None and action_id not in allowed:
        target = next(iter(allowed), None)
        if target is None:
            raise ValueError(
                f"{norm} is reserved by the system and cannot be rebound")
        raise ValueError(
            f"{norm} is reserved by the system (only {target} can use it)")
    if action_id in MODIFIER_REQUIRED_ACTIONS and not _has_command_modifier(norm):
        raise ValueError(
            f"{action_id} needs Ctrl / Alt / Meta in its key")
    if norm in RESERVED_KEYS and action_id not in RESERVED_KEYS[norm]:
        raise ValueError(f"{norm} cannot be used for {action_id}")


def conflicts(
        store: Any,
        key: str,
        *,
        action_id: str | None = None,
) -> tuple[Action, ...]:
    """``key`` 上已经存在的动作（排除 ``action_id`` 自己）。

    ``store`` 只要求实现 ``bindings(key)``：返回 ``(action, binding)`` 序列。
    传入普通 ``Registry`` 即可；测试里可以给假实现。
    """
    norm = normalize_key(key)
    binder = getattr(store, "bindings", None)
    if not callable(binder):
        return ()
    result: list[Action] = []
    for action, _binding in binder(norm):
        if action_id is not None and action.id == action_id:
            continue
        if action not in result:
            result.append(action)
    return tuple(result)
