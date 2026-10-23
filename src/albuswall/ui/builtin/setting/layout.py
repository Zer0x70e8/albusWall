#
"""分组内容的排列规则（纯数据，不依赖 Qt）。

一个分组的内容有三种来源，渲染顺序由**同一个 ``order`` 尺度**决定::

    group ──► items[]   (配置叶子，带 order)
          ├─ extras[]  (注册的自定义行，见 ``extras.py``)
          └─ groups[]  (子分组，带 order)

排序键是 ``(order, 种类, 原始下标)``：

* ``order`` 小的在前 —— 这是唯一需要关心的字段；
* ``order`` 相同时按「叶子 → 自定义行 → 子分组」的固定次序兜底，
  因此不写 ``order`` 的分组仍然保持「先叶子、后子分组」的旧观感；
* 自定义行的默认 ``order`` 是 500（见 ``extras.py``），也就是「没写
  ``order`` 就排在已有内容后面」，不会莫名其妙插到中间；
* 再相同就按原始下标，保证排序稳定。

分成独立模块是为了让「布局规则」脱离 Qt 单独测试：``groups.py`` 只负责
把这些条目变成控件，``overrides.py`` 只负责往分组里挂 ``extras``。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

__all__ = [
    "EXTRA",
    "GROUP",
    "ITEM",
    "Entry",
    "iter_entries",
    "order_of",
]

#: 条目种类
ITEM = "item"
EXTRA = "extra"
GROUP = "group"

#: ``order`` 相同时的种类兜底次序
_RANK: dict[str, int] = {ITEM: 0, EXTRA: 1, GROUP: 2}


@dataclass(frozen=True)
class Entry:
    """分组内容里的一条，按 :attr:`sort_key` 排好序。

    Attributes:
        kind: ``"item"`` / ``"extra"`` / ``"group"``。
        order: 同层排序权重。
        index: 同种类里的原始下标（稳定排序用）。
        payload: 原始数据（item dict / ``ExtraSpec`` / 子分组 dict）。
    """

    kind: str
    order: int
    index: int
    payload: Any

    @property
    def sort_key(self) -> tuple[int, int, int]:
        return (self.order, _RANK.get(self.kind, 99), self.index)

    @property
    def is_item(self) -> bool:
        return self.kind == ITEM

    @property
    def is_extra(self) -> bool:
        return self.kind == EXTRA

    @property
    def is_group(self) -> bool:
        return self.kind == GROUP


def order_of(node: Any, default: int = 0) -> int:
    """取一个条目的 ``order``：dict（schema dump）或对象（``ExtraSpec``）。"""
    raw: Any
    if isinstance(node, Mapping):
        raw = node.get("order")
    else:
        raw = getattr(node, "order", None)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _items_of(group: Mapping[str, Any]) -> Sequence[Any]:
    payload = group.get("items")
    return payload if isinstance(payload, (list, tuple)) else ()


def _extras_of(group: Mapping[str, Any]) -> Sequence[Any]:
    payload = group.get("extras")
    return payload if isinstance(payload, (list, tuple)) else ()


def _groups_of(group: Mapping[str, Any]) -> Sequence[Any]:
    payload = group.get("groups")
    return payload if isinstance(payload, (list, tuple)) else ()


def iter_entries(group: Mapping[str, Any]) -> list[Entry]:
    """把一个分组 dict 摊平成排好序的条目列表。

    Args:
        group: ``dump_setting_schema()`` 里的一层分组 dict（可带 ``extras``，
            由 ``overrides.apply_patches`` 挂上）。

    Returns:
        :class:`Entry` 列表，已按 ``(order, 种类, 下标)`` 排序。
    """
    entries: list[Entry] = []

    for index, item in enumerate(_items_of(group)):
        if isinstance(item, Mapping) and item.get("key"):
            entries.append(Entry(ITEM, order_of(item), index, item))

    for index, spec in enumerate(_extras_of(group)):
        entries.append(Entry(EXTRA, order_of(spec, 500), index, spec))

    for index, child in enumerate(_groups_of(group)):
        if isinstance(child, Mapping):
            entries.append(Entry(GROUP, order_of(child), index, child))

    entries.sort(key=lambda entry: entry.sort_key)
    return entries
