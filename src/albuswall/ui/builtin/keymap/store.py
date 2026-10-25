#
"""键位配置的读写门面（设置页 / 按键分发共用）。

本模块是「配置子系统」与「按键功能」之间唯一的接缝：

* 读：``dynamic.keymap`` 里的 ``bindings`` 覆盖值 + 每个动作的默认值；
* 写：把覆盖值写回子配置文件（``autosave=True``，改一次落一次）；
* 变更通知：写的是 ``StatefulNamespace`` 字段，赋值本身就会触发观察者，
  设置页和 ``KeymapManager`` 都订阅同一个观察者，不需要互相认识。

失败语义
========
配置系统还没就绪（headless / 单元测试 / 启动早期）时不抛异常：退回一份
**内存里**的 :class:`KeymapState`，读写照常可用，只是不落盘。真正需要文件
的功能（设置页里显示「写入到哪个文件」）拿到的是 ``None``。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from albuswall.log import getLogger

from .keys import normalize_sequence
from .registry import KeyAction, get_action, iter_actions

__all__ = [
    "Binding",
    "binding_for",
    "binding_of",
    "conflicts",
    "effective_bindings",
    "entries",
    "is_enabled",
    "keymap_file",
    "keymap_state",
    "known_ids",
    "overrides",
    "reset_all",
    "reset_binding",
    "set_binding",
    "set_enabled",
]

_logger = getLogger(__name__)

#: 配置系统不可用时的内存兜底
_fallback: Any = None


# ══════════════════════════════════════════════════
# 状态子树
# ══════════════════════════════════════════════════
def keymap_state() -> Any:
    """取 ``dynamic.keymap`` 子树；配置不可用时返回内存兜底实例。

    每次调用都会尝试经 ``DynamicConfig`` 取（命中缓存时只是一次 dict 查找），
    这样配置系统后初始化好之后会自动切到真实子树。
    """
    global _fallback
    try:
        from albuswall.configue.declaration import get_config

        state = get_config().dynamic.keymap
        if state is not None:
            return state
    except Exception:  # noqa: BLE001  早期 / headless：用内存兜底
        _logger.debug(
            "dynamic.keymap unavailable; using in-memory state",
            exc_info=True,
        )

    if _fallback is None:
        from .state import KeymapState

        _fallback = KeymapState()
    return _fallback


def keymap_file() -> Optional[Path]:
    """键位子配置文件的路径；内存兜底时返回 ``None``。"""
    path = getattr(keymap_state(), "path", None)
    return Path(path) if path is not None else None


def _raw_overrides() -> dict[str, str]:
    """``bindings`` 的清洗副本：只保留 ``str -> str``。"""
    raw = getattr(keymap_state(), "bindings", None)
    if not isinstance(raw, dict):
        return {}
    cleaned: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key.strip():
            continue
        cleaned[key] = value if isinstance(value, str) else str(value)
    return cleaned


def overrides() -> dict[str, str]:
    """用户显式写进配置文件的覆盖值（不解析、不改写）。"""
    return dict(_raw_overrides())


def is_enabled() -> bool:
    """按键映射总开关。"""
    try:
        return bool(getattr(keymap_state(), "enabled", True))
    except Exception:  # noqa: BLE001
        return True


# ══════════════════════════════════════════════════
# 生效值
# ══════════════════════════════════════════════════
def _safe_normalize(text: object) -> str:
    """解析失败时记日志并当成「不绑定」，不让一个坏值拖垮整个键位表。"""
    try:
        return normalize_sequence(text)
    except ValueError as exc:
        _logger.warning("invalid key binding %r: %s", text, exc)
        return ""


def binding_for(action: KeyAction) -> str:
    """一个动作当前生效的按键（覆盖值优先，回落到声明默认值）。"""
    raw = _raw_overrides().get(action.id)
    if raw is None:
        return _safe_normalize(action.default_keys)
    return _safe_normalize(raw)


def binding_of(action_id: str) -> str:
    """按 id 取生效按键（未注册的 id 只认配置里的覆盖值）。"""
    action = get_action(action_id)
    if action is not None:
        return binding_for(action)
    raw = _raw_overrides().get(action_id, "")
    return _safe_normalize(raw)


def effective_bindings() -> dict[str, str]:
    """``{动作 id: 生效按键}``，按登记顺序；未绑定的动作也在里面（值为空串）。"""
    return {action.id: binding_for(action) for action in iter_actions()}


def conflicts() -> dict[str, list[str]]:
    """``{按键: [动作 id, ...]}``，只返回被多个**已注册**动作占用的键。"""
    table: dict[str, list[str]] = {}
    for action_id, keys in effective_bindings().items():
        if keys:
            table.setdefault(keys, []).append(action_id)
    return {keys: ids for keys, ids in table.items() if len(ids) > 1}


# ══════════════════════════════════════════════════
# 写
# ══════════════════════════════════════════════════
def _write_bindings(bindings: dict[str, str]) -> None:
    """整体替换 ``bindings``（赋值才会让 ``StatefulNamespace`` 标脏 / 落盘）。"""
    state = keymap_state()
    state.bindings = dict(bindings)


def set_binding(action_id: str, keys: object) -> str:
    """给一个动作绑定按键，返回规范化后的值。

    Args:
        action_id: 动作 id；未注册的 id 也允许写（插件卸载后残留的旧配置
            仍然可以管理 / 清除）。
        keys: 任意写法的按键文本；空串表示显式解绑。

    Returns:
        规范化的 PortableText；空串表示解绑。

    Raises:
        ValueError: 动作 id 为空 / 按键无法解析。
    """
    action_id = str(action_id or "").strip()
    if not action_id:
        raise ValueError("binding target action id must be a non-empty string")

    normalized = normalize_sequence(keys)
    bindings = _raw_overrides()
    default = ""
    action = get_action(action_id)
    if action is not None:
        default = _safe_normalize(action.default_keys)

    if normalized == default:
        # 和默认值一样就不写覆盖：默认值以后可以自由演进
        bindings.pop(action_id, None)
    else:
        bindings[action_id] = normalized
    _write_bindings(bindings)
    return normalized


def reset_binding(action_id: str) -> None:
    """删掉一个动作的覆盖值（恢复默认绑定）。"""
    bindings = _raw_overrides()
    if bindings.pop(str(action_id), None) is None:
        return
    _write_bindings(bindings)


def reset_all() -> None:
    """删掉全部覆盖值（所有动作恢复默认）。"""
    if not _raw_overrides():
        return
    _write_bindings({})


def set_enabled(enabled: bool) -> None:
    """打开 / 关闭按键映射总开关。"""
    state = keymap_state()
    state.enabled = bool(enabled)


# ══════════════════════════════════════════════════
# 设置页用的视图
# ══════════════════════════════════════════════════
@dataclass(frozen=True)
class Binding:
    """设置页里的一行：已注册动作 + 当前生效按键 + 是否覆盖。

    ``action`` 为 ``None`` 表示「配置里有、但当前没有对应动作」的残留键
    （插件被卸载 / 动作改名）。这些行只允许查看和删除，不允许编辑绑定。
    """

    id: str
    label: str
    description: str
    group: str
    order: int
    keys: str
    default_keys: str
    overridden: bool
    action: Optional[KeyAction] = None

    @property
    def is_known(self) -> bool:
        return self.action is not None


def entries(*, include_unknown: bool = True) -> list[Binding]:
    """设置页要展示的全部「动作 ↔ 按键」条目。

    已注册动作按 ``registry.iter_actions()`` 的顺序在前；``include_unknown``
    为 True 时，把配置里残留的未知 id 追加到最后（按 id 排序）。
    """
    raw = _raw_overrides()
    rows: list[Binding] = []

    for action in iter_actions():
        keys = binding_for(action)
        rows.append(
            Binding(
                id=action.id,
                label=action.label,
                description=action.description,
                group=action.group,
                order=action.order,
                keys=keys,
                default_keys=_safe_normalize(action.default_keys),
                overridden=action.id in raw,
                action=action,
            )
        )

    if include_unknown:
        known = {row.id for row in rows}
        for action_id in sorted(raw):
            if action_id in known:
                continue
            rows.append(
                Binding(
                    id=action_id,
                    label=action_id,
                    description="",
                    group="unknown",
                    order=10_000,
                    keys=_safe_normalize(raw[action_id]),
                    default_keys="",
                    overridden=True,
                    action=None,
                )
            )
    return rows


def _iter_known_ids() -> Iterator[str]:
    return (action.id for action in iter_actions())


def known_ids() -> set[str]:
    """当前已登记的动作 id 集合（测试 / 调试用）。"""
    return set(_iter_known_ids())


def iter_bindings(
        actions: Optional[Iterable[KeyAction]] = None,
) -> Iterator[tuple[KeyAction, str]]:
    """``(动作, 生效按键)`` 迭代器；不传 ``actions`` 时用全部登记动作。"""
    source = iter_actions() if actions is None else actions
    for action in source:
        yield action, binding_for(action)
