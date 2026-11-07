"""设置页的**展示规则**：可见性、排序、合成分组（纯函数，不碰 Qt）。

从 ``view.py`` 拆出来的一层：这些判断原来写在视图方法里，导致"哪一行该显示"
既要看范围、又要看高级开关、还要看分组是否为合成分组，散落在三处。现在它们
是纯函数，可以单独测，也方便以后加"搜索过滤"这类新规则。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from . import strings
from .contract import Scope, ScopeKind

__all__ = [
    "AUTO_COLLAPSE_ITEM_COUNT",
    "UNBOUND_GROUP_KEY",
    "UNCOVERED_GROUP_KEY",
    "group_visible",
    "item_visible",
    "ordered_children",
    "scope_from_text",
    "section_visible",
    "unbound_group",
    "uncovered_group",
    "visible_count",
]

#: 折叠阈值：一个分组可见项超过这个数时默认收起。
AUTO_COLLAPSE_ITEM_COUNT = 24

#: 合成分组（不属于 dump 的 groups，但需要锚点 / 折叠）。
UNBOUND_GROUP_KEY = "__unbound__"
UNCOVERED_GROUP_KEY = "__uncovered__"


def item_visible(
        item: Mapping[str, Any], scope: Scope, show_advanced: bool,
) -> bool:
    """普通分组里的一个叶子在当前范围 / 高级开关下是否可见。"""
    if not show_advanced and item.get("advanced"):
        return False
    target = item.get("target_file")
    if scope.kind is ScopeKind.ALL:
        return target is not None
    if scope.kind is ScopeKind.FILE:
        return target == scope.file
    if scope.kind is ScopeKind.UNBOUND:
        return target is None
    return False


def visible_count(
        group: Mapping[str, Any], scope: Scope, show_advanced: bool,
) -> int:
    """分组里可见叶子数（含子分组）。"""
    if not show_advanced and group.get("advanced"):
        return 0
    own = sum(
        1 for item in group.get("items") or ()
        if item_visible(item, scope, show_advanced)
    )
    children = sum(
        visible_count(child, scope, show_advanced)
        for child in group.get("groups") or ()
    )
    return own + children


def group_visible(
        group: Mapping[str, Any], scope: Scope, show_advanced: bool,
) -> bool:
    """分组是否显示：有可见叶子，或者带自定义组件且处于「全部」范围。"""
    if not show_advanced and group.get("advanced"):
        return False
    if visible_count(group, scope, show_advanced) > 0:
        return True
    # 自定义组件分组（例如「关于」里的 label）不绑定文件，只在「全部」
    # 范围里显示；按文件 / 未绑定 / 未覆盖过滤时隐藏。
    return bool(group.get("components")) and scope.kind is ScopeKind.ALL


def section_visible(key: str, scope: Scope) -> bool:
    """合成分组（未绑定 / 未覆盖）在当前范围下是否显示。"""
    if key == UNBOUND_GROUP_KEY:
        return scope.kind in (ScopeKind.ALL, ScopeKind.UNBOUND)
    if key == UNCOVERED_GROUP_KEY:
        return scope.kind in (ScopeKind.ALL, ScopeKind.UNCOVERED)
    return True


def ordered_children(
        group: Mapping[str, Any],
) -> list[tuple[str, Mapping[str, Any]]]:
    """items / groups 按 ``order`` 混排（稳定：同 order 时 items 在前）。"""
    entries: list[tuple[int, int, str, Mapping[str, Any]]] = []
    for index, item in enumerate(group.get("items") or ()):
        entries.append((int(item.get("order") or 0), index, "item", item))
    offset = len(entries)
    for index, child in enumerate(group.get("groups") or ()):
        entries.append(
            (int(child.get("order") or 0), offset + index, "group", child),
        )
    entries.sort(key=lambda entry: (entry[0], entry[1]))
    return [(kind, entry) for _order, _index, kind, entry in entries]


def unbound_group(keys: Sequence[str]) -> dict[str, Any]:
    """「未绑定」合成分组（只读展示用，不参与提交）。"""
    return {
        "key": UNBOUND_GROUP_KEY,
        "id": UNBOUND_GROUP_KEY,
        "title": strings.UNBOUND_TITLE,
        "description": strings.UNBOUND_DESCRIPTION,
        "order": 9000,
        "item_count": len(keys),
        "advanced": False,
        "editable": True,
    }


def uncovered_group(entries: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """「未覆盖」合成分组（只读展示用，没有 binder）。"""
    return {
        "key": UNCOVERED_GROUP_KEY,
        "id": UNCOVERED_GROUP_KEY,
        "title": strings.UNCOVERED_TITLE,
        "description": strings.UNCOVERED_DESCRIPTION,
        "order": 9001,
        "item_count": len(entries),
        "advanced": False,
        "editable": True,
    }


def scope_from_text(text: str) -> Scope:
    """字符串范围的宽松写法（``"all"`` / ``"unbound"`` / 文件路径）。"""
    cleaned = str(text).strip()
    if cleaned in ("", "all", strings.SCOPE_ALL):
        return Scope.all()
    if cleaned in ("unbound", strings.SCOPE_UNBOUND):
        return Scope.unbound()
    if cleaned in ("uncovered", strings.SCOPE_UNCOVERED):
        return Scope.uncovered()
    if cleaned.startswith("file:"):
        return Scope.for_file(cleaned[len("file:"):])
    return Scope.for_file(cleaned)


def iter_items(groups: Iterable[Mapping[str, Any]]) -> Iterable[Mapping[str, Any]]:
    """深度优先遍历叶子（需要时用；视图多数时候走 ``SettingTree`` 索引）。"""
    for group in groups:
        yield from group.get("items") or ()
        yield from iter_items(group.get("groups") or ())
