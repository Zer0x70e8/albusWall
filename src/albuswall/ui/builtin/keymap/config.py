"""``[shortcuts]`` 段：唯一落盘形态（Qt-free）。

每条 = **键 → 操作列表**（元素为 ``"action_id"`` 或
``{"action": …, "args": {…}}``）。默认值来自 ``@shortcut(keys=...)``；
写盘与默认表比较，相同就删掉那一行（单账）。
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

import albuswall
from albuswall.configue import register_load_hook
from albuswall.configue.declaration import unregister_load_hook
from albuswall.log import getLogger

from .bindings import binding_from_raw, normalize_key
from .model import Binding, registry

if TYPE_CHECKING:
    from albuswall.configue import Configue

__all__ = [
    "SECTION", "VALUE_TYPE", "attach_persistence", "install", "load_from_config",
    "read_bindings", "reset_action", "reset_all", "resolve_config",
    "save_to_config", "uninstall",
]

SECTION = "shortcuts"
#: 段内每条的类型；JSON 数组可含 ``{"action", "args"}`` 对象，所以元素是 Any。
VALUE_TYPE = list[Any]

_logger = getLogger(f"{albuswall.__name__}.ui.keymap2.config")
_persistence_unsub = None


def ensure_types(resolver, keys: Iterable[str] | None = None) -> list[str]:
    """给段里的按键登记 ``list[Any]`` 类型（读 / 写前都要先补）。"""
    targets = list(resolver.keys_of(SECTION) if keys is None else keys)
    for key in targets:
        if key:
            resolver.register_type(SECTION, key, VALUE_TYPE)
    return targets


def _on_config_load(resolver) -> None:
    ensure_types(resolver)


def install() -> None:
    """配置阶段登记加载钩子（必须早于静态配置加载）。"""
    register_load_hook(_on_config_load)


def uninstall() -> None:
    unregister_load_hook(_on_config_load)


def resolve_config(config: Configue | None = None) -> Configue:
    if config is not None:
        return config
    from albuswall.configue import get_config

    return get_config()


def read_bindings(config: Configue | None = None) -> dict[str, tuple[Binding, ...]]:
    """读 ``[shortcuts]`` → ``{键: (绑定, ...)}``；坏行只跳过自己。"""
    resolver = resolve_config(config).resolver
    if not resolver.has_section(SECTION):
        return {}
    ensure_types(resolver)
    result: dict[str, tuple[Binding, ...]] = {}
    for raw_key in resolver.keys_of(SECTION):
        try:
            key = normalize_key(raw_key)
            values = resolver.get_typed(SECTION, raw_key) or []
        except (KeyError, ValueError) as exc:
            _logger.warning("[%s] %s: %s", SECTION, raw_key, exc)
            continue
        ops: list[Binding] = []
        for value in values:
            try:
                ops.append(binding_from_raw(value))
            except ValueError as exc:
                _logger.warning("[%s] %s: %s", SECTION, raw_key, exc)
        result[key] = tuple(ops)
    return result


def load_from_config(
        config: Configue | None = None, *, store=registry,
) -> list[str]:
    """把配置里的用户值覆盖到注册中心；返回未应用的 ``key:action``。"""
    missing = store.apply_bindings(read_bindings(config))
    if missing:
        _logger.info("shortcuts: %d entries skipped: %s", len(missing), missing)
    return missing


def save_to_config(
        config: Configue | None = None,
        *,
        keys: Iterable[str] | None = None,
        store=registry,
) -> None:
    """把最终值与默认值比较：相同删行，不同写行。"""
    target = resolve_config(config)
    resolver = target.resolver
    defaults = store.default_bindings()
    final = store.effective_bindings()
    if keys is None:
        known = {action.id for action in store.iter_actions()}
        # 配置里出现过、且动作仍可识别的键也要参与比较，否则「改回默认」的
        # 旧键（已不在最终表里）永远删不掉。
        existing = {
            key for key, ops in read_bindings(target).items()
            if any(binding.action in known for binding in ops)
        }
        targets = sorted(set(defaults) | set(final) | existing)
    else:
        targets = list(keys)

    dirty: list[tuple[str, str]] = []
    for key in targets:
        default_ops = defaults.get(key, ())
        final_ops = final.get(key, ())
        exists = resolver.has_section(SECTION) and key in resolver.keys_of(SECTION)
        if final_ops == default_ops:
            if exists:
                target.remove(SECTION, key, save="defer")
                dirty.append((SECTION, key))
            continue
        ensure_types(resolver, [key])
        target.set(
            SECTION, key, [binding.to_raw() for binding in final_ops],
            save="defer",
        )
        dirty.append((SECTION, key))

    if dirty:
        try:
            target.flush(only=dirty)
        except Exception as exc:  # noqa: BLE001  写盘失败只记日志
            _logger.error("shortcuts: flush failed: %s", exc)


def reset_action(action_id: str, config: Configue | None = None) -> None:
    registry.reset_action(action_id)
    save_to_config(config)


def reset_all(config: Configue | None = None) -> None:
    registry.reset_all()
    save_to_config(config)


def attach_persistence(
        config: Configue | None = None, *, store=registry,
) -> Any:
    """订阅注册中心变更并落盘；返回取消函数（重复调用幂等）。"""
    global _persistence_unsub
    if _persistence_unsub is not None:
        return _persistence_unsub

    def _save() -> None:
        try:
            save_to_config(config, store=store)
        except Exception as exc:  # noqa: BLE001
            _logger.error("shortcuts: save failed: %s", exc)

    _persistence_unsub = store.subscribe(_save)
    return _persistence_unsub
