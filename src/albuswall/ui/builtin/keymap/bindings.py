"""绑定的纯函数层（Qt-free）：按键规范化、默认表、合并、序列化。

按键内部统一用小写规范形式（``"ctrl+q"``）；展示成 ``Ctrl+Q`` 是 UI 层的事。
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from .model import Action, Binding

__all__ = [
    "default_bindings", "merge_bindings", "normalize_key",
    "binding_from_raw", "render_ops",
]

_MODIFIERS = {
    "ctrl": "ctrl", "control": "ctrl", "shift": "shift", "alt": "alt",
    "meta": "meta", "super": "meta", "win": "meta", "cmd": "meta",
    "command": "meta",
}
_NAMED_KEYS = {
    "esc": "escape", "escape": "escape",
    "return": "return", "enter": "enter",
    "tab": "tab", "backtab": "backtab", "space": "space",
    "backspace": "backspace", "delete": "delete", "del": "delete",
    "insert": "insert", "ins": "insert",
    "home": "home", "end": "end",
    "left": "left", "right": "right", "up": "up", "down": "down",
    "pageup": "pgup", "pgup": "pgup", "pagedown": "pgdown", "pgdown": "pgdown",
    "print": "print", "pause": "pause", "menu": "menu",
}
_BAD_KEY_CHARS = ("=", ":", "\n", "\r")


def _token(part: str) -> str:
    low = part.lower()
    return _MODIFIERS.get(low, _NAMED_KEYS.get(low, low))


def normalize_key(text: object) -> str:
    """任意写法的按键文本 → 小写规范形式；空串 / 非法文本抛 ``ValueError``。"""
    raw = str(text or "").strip()
    if not raw:
        raise ValueError("empty key")
    if raw == "+":
        # 单独的「+」键：split 会把它拆成两个空串，这里显式兜住。
        parts = ["+"]
    elif raw.endswith("++"):
        parts = [p.strip() for p in raw[:-2].split("+") if p.strip()] + ["+"]
    else:
        parts = [p.strip() for p in raw.split("+") if p.strip()]
    if not parts or any(ch in raw for ch in _BAD_KEY_CHARS):
        raise ValueError(f"unsupported key sequence: {raw!r}")
    return "+".join(_token(p) for p in parts)


def binding_from_raw(raw: Any) -> Binding:
    return Binding.from_raw(raw)


def render_ops(bindings: Iterable[Binding]) -> str:
    """配置值文本：始终写 JSON 数组（``list[Any]`` 读回无歧义）。"""
    return json.dumps(
        [b.to_raw() for b in bindings], ensure_ascii=False)


def default_bindings(actions: Iterable[Action]) -> dict[str, tuple[Binding, ...]]:
    """``@shortcut(keys=...)`` → ``{键: (绑定, ...)}``（按动作顺序）。

    每个键取 ``action.arguments_for(key)``：默认键可以带自己的参数。
    """
    table: dict[str, list[Binding]] = {}
    for action in actions:
        for key in action.keys:
            table.setdefault(key, []).append(
                Binding(action.id, action.arguments_for(key)))
    return {key: tuple(items) for key, items in table.items()}


def merge_bindings(
        defaults: Mapping[str, tuple[Binding, ...]],
        overrides: Mapping[str, Iterable[Binding]],
) -> dict[str, tuple[Binding, ...]]:
    """默认值 + 用户覆盖 → 最终表；空覆盖表示「这个键被用户清空」。"""
    final = {key: tuple(items) for key, items in defaults.items()}
    for key, ops in overrides.items():
        ops = tuple(ops)
        if ops:
            final[key] = ops
        else:
            final.pop(key, None)
    return final
