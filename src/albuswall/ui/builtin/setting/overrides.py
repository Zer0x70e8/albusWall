#
"""按 key 修改「已经登记的设置项」——设置页的第二层注册表。

``configue.setting_schema`` 回答的是「有哪些设置、默认怎么展示」；本模块
回答的是「**UI 层面**要怎么把某一项展示 / 写回得更好」。两者是叠加关系：
schema 是事实，override 是呈现与交互。

为什么需要它
============
schema 的 ``widget`` 提示只能是静态的（``text`` / ``choice`` / …），但真实
场景经常是动态的：

* 「UI 主题」应该是**磁盘上真实存在的主题**的下拉框，而不是自由文本框；
* 选一个主题要同时写 ``[ui]theme`` / ``[path]theme`` 多个节点；
* 有些项改完要重启才生效，UI 应该标出来；
* 个别项需要完全自定义的控件（颜色选择器、JSON 树编辑器……）。

这些都是「对已有项的加工」，不该去改 ``services/command.py`` 的登记表。

能力一览
========
:class:`ItemPatch`
    * 改展示：``label`` / ``description`` / ``hint`` / ``order`` / ``editable``；
    * 改控件：``widget`` / ``choices`` / ``choices_provider`` / ``editor``；
    * 改数值范围：``minimum`` / ``maximum`` / ``step`` / ``default``；
    * 改写回：``apply``（一个值 → 多个配置节点）；
    * ``hidden=True`` 整项隐藏。

:class:`GroupPatch`
    改分组标题 / 说明 / 排序，或 ``hidden=True`` 整个分组隐藏。

:mod:`~albuswall.ui.builtin.setting.extras`
    往分组里插「非配置行」（动作按钮 / 说明 / 插件控件）；它们在
    :func:`apply_patches` 里挂到分组的 ``extras`` 字段上，和叶子、子分组
    一起按 ``order`` 混排（见 ``layout.py``）。

用法::

    from albuswall.ui.builtin.setting.overrides import item_patch

    @item_patch(
        "appearance.theme",
        widget="choice",
        choices_provider=discover_theme_names,
        hint="Restart required",
    )
    def apply_theme(name, item):
        return {
            "appearance.theme": name,
            "appearance.theme_details.theme_path": str(theme_dir(name)),
        }

不加 ``apply`` 时它就是一条纯展示补丁::

    item_patch("advanced.runtime.ui", hint="Restart required")(
        lambda value, item: {}
    )

注册表在 UI 每次加载 schema 时应用一次（:func:`apply_patches`），因此补丁
可以随时改、也可以用 ``replace=True`` 覆盖别人的补丁。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from albuswall.log import getLogger

from .extras import ExtraSpec, iter_extras

__all__ = [
    "UNSET",
    "GroupPatch",
    "ItemPatch",
    "apply_patches",
    "get_group_patch",
    "get_item_patch",
    "group_patch",
    "hide_group",
    "hide_item",
    "item_patch",
    "iter_item_patches",
    "register_group_patch",
    "register_item_patch",
    "unregister_group_patch",
    "unregister_item_patch",
]

_logger = getLogger(__name__)


class _Unset:
    """哨兵：区分「没写这个字段」和「字段显式写成 None / False」。"""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - 只用于调试
        return "<unset>"


UNSET = _Unset()


# --------------------------------------------------------------------------- #
# 补丁定义
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ItemPatch:
    """一条「对某个设置项加工」的声明。

    Args:
        key: 目标叶子的 key（``dump_setting_schema()`` 里的 ``key``，
            形如 ``appearance.theme``）。
        label / description / hint: 展示文本；``hint`` 显示在标签后面，
            用来标「重启后生效」这类提示。
        widget: 覆盖控件类型提示（``text`` / ``choice`` / ``dir`` / …）。
        choices: 静态候选值。
        choices_provider: 动态候选值回调（每次建控件时调用，可以是查磁盘）。
        minimum / maximum / step: 数值范围。
        editable: 是否可编辑（``False`` = 只读展示）。
        default: 覆盖「恢复默认」用的默认值。
        order: 同组内的排序权重（小的在前）。
        hidden: 在 UI 里整项隐藏（配置本身仍然存在）。
        editor: 完全自定义编辑器工厂：``fn(item) -> Editor``。
        apply: 写回钩子：``fn(value, item) -> {配置key: 值}``；返回值里的
            每个键都会被写回，因此「选一个主题写三个节点」这类需求不用改
            services 层。
    """

    key: str
    label: Any = UNSET
    description: Any = UNSET
    hint: str = ""
    widget: Any = UNSET
    choices: Any = UNSET
    choices_provider: Callable[[], Sequence[Any]] | None = None
    minimum: Any = UNSET
    maximum: Any = UNSET
    step: Any = UNSET
    editable: Any = UNSET
    default: Any = UNSET
    order: Any = UNSET
    hidden: bool = False
    editor: Callable[[dict[str, Any]], Any] | None = None
    apply: Callable[[Any, dict[str, Any]], Mapping[str, Any]] | None = None

    def __post_init__(self) -> None:
        if not self.key or self.key.startswith("."):
            raise ValueError(f"invalid setting item key: {self.key!r}")
        # 动态候选值 / 显式候选值都意味着「这是个下拉框」
        if self.widget is UNSET and (
                self.choices is not UNSET or self.choices_provider is not None
        ):
            object.__setattr__(self, "widget", "choice")


@dataclass(frozen=True)
class GroupPatch:
    """一条「对某个分组加工」的声明。``key`` 用分组 qualname（根分组就是 id）。"""

    key: str
    title: Any = UNSET
    description: Any = UNSET
    order: Any = UNSET
    hidden: bool = False


# --------------------------------------------------------------------------- #
# 注册表
# --------------------------------------------------------------------------- #
_ITEM_PATCHES: dict[str, ItemPatch] = {}
_GROUP_PATCHES: dict[str, GroupPatch] = {}


def register_item_patch(
        patch: ItemPatch,
        *,
        replace: bool = False,
) -> ItemPatch:
    """登记一条项补丁。

    同一个 key 重复登记默认抛异常（与 ``command`` / ``page`` 一致：静默覆盖
    会让「谁赢了」变成 import 顺序的副产品）；想覆盖就显式 ``replace=True``。
    """
    if not replace and patch.key in _ITEM_PATCHES:
        raise ValueError(f"duplicate item patch: {patch.key!r}")
    _ITEM_PATCHES[patch.key] = patch
    return patch


def register_group_patch(
        patch: GroupPatch,
        *,
        replace: bool = False,
) -> GroupPatch:
    """登记一条分组补丁。"""
    if not replace and patch.key in _GROUP_PATCHES:
        raise ValueError(f"duplicate group patch: {patch.key!r}")
    _GROUP_PATCHES[patch.key] = patch
    return patch


def unregister_item_patch(key: str) -> ItemPatch | None:
    return _ITEM_PATCHES.pop(key, None)


def unregister_group_patch(key: str) -> GroupPatch | None:
    return _GROUP_PATCHES.pop(key, None)


def get_item_patch(key: str) -> ItemPatch | None:
    return _ITEM_PATCHES.get(key)


def get_group_patch(key: str) -> GroupPatch | None:
    return _GROUP_PATCHES.get(key)


def iter_item_patches() -> Iterator[ItemPatch]:
    yield from _ITEM_PATCHES.values()


def item_patch(
        key: str,
        *,
        label: Any = UNSET,
        description: Any = UNSET,
        hint: str = "",
        widget: Any = UNSET,
        choices: Any = UNSET,
        choices_provider: Callable[[], Sequence[Any]] | None = None,
        minimum: Any = UNSET,
        maximum: Any = UNSET,
        step: Any = UNSET,
        editable: Any = UNSET,
        default: Any = UNSET,
        order: Any = UNSET,
        hidden: bool = False,
        editor: Callable[[dict[str, Any]], Any] | None = None,
        replace: bool = False,
):
    """装饰器形态的 :func:`register_item_patch`。

    被装饰的函数作为 ``apply`` 写回钩子；不需要写回时返回 ``None`` 即可
    （比如纯展示补丁）::

        @item_patch("advanced.runtime.ui", hint="Restart required")
        def _noop(value, item):
            return None

    也可以用 ``@item_patch(...)`` 直接修饰一个普通的编辑器工厂，此时用
    ``editor=`` 显式指定不会被误当成 ``apply``。
    """

    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        register_item_patch(
            ItemPatch(
                key=key,
                label=label,
                description=description,
                hint=hint,
                widget=widget,
                choices=choices,
                choices_provider=choices_provider,
                minimum=minimum,
                maximum=maximum,
                step=step,
                editable=editable,
                default=default,
                order=order,
                hidden=hidden,
                editor=editor,
                apply=None if editor is not None else fn,
            ),
            replace=replace,
        )
        return fn

    return deco


def group_patch(
        key: str,
        *,
        title: Any = UNSET,
        description: Any = UNSET,
        order: Any = UNSET,
        hidden: bool = False,
        replace: bool = False,
):
    """装饰器形态的 :func:`register_group_patch`（被修饰函数只作占位）。"""

    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        register_group_patch(
            GroupPatch(
                key=key, title=title, description=description,
                order=order, hidden=hidden,
            ),
            replace=replace,
        )
        return fn

    return deco


def hide_item(key: str, *, replace: bool = True) -> ItemPatch:
    """便捷入口：把某一项从设置页里隐掉。"""
    return register_item_patch(ItemPatch(key=key, hidden=True), replace=replace)


def hide_group(key: str, *, replace: bool = True) -> GroupPatch:
    """便捷入口：把某个分组从设置页里隐掉。"""
    return register_group_patch(GroupPatch(key=key, hidden=True), replace=replace)


# --------------------------------------------------------------------------- #
# 应用到 schema dump
# --------------------------------------------------------------------------- #
def apply_patches(schema: dict[str, Any]) -> dict[str, Any]:
    """把补丁叠到 ``dump_setting_schema()`` 的结果上（就地修改并返回）。

    产出仍然是「组 → 子组 / 叶子」的同一形状，只是：

    * 被隐藏的项 / 组已经从树里摘掉；
    * 被补丁过的叶子带了 ``patch``（``ItemPatch`` 对象，UI 内部用，
      不是 JSON）、``choices_provider``、``order``、``hint`` 等字段；
    * 每个分组多一个 ``extras`` 列表（``extras.py`` 注册的自定义行，
      同样是 UI 内部对象，不是 JSON）；
    * ``items`` / ``by_file`` / ``unbound`` / ``summary`` 按过滤后的树重算。
    """
    extras = _extras_by_group()
    groups: list[dict[str, Any]] = []
    for group in schema.get("groups") or []:
        if not isinstance(group, dict):
            continue
        surviving = _apply_group(group, extras)
        if surviving is not None:
            groups.append(surviving)

    _warn_unmatched_extras(extras, groups)

    schema["groups"] = groups
    items = list(_flatten(groups))
    schema["items"] = items

    by_file: dict[str, list[str]] = {}
    unbound: list[str] = []
    for item in items:
        target = item.get("target_file")
        if target is None:
            unbound.append(item["key"])
        else:
            by_file.setdefault(str(target), []).append(item["key"])
    schema["by_file"] = by_file
    schema["unbound"] = unbound

    summary = dict(schema.get("summary") or {})
    summary["group_count"] = _count_groups(groups)
    summary["item_count"] = len(items)
    summary["file_count"] = len(by_file)
    schema["summary"] = summary
    return schema


def _extras_by_group() -> dict[str, list[ExtraSpec]]:
    """``分组 qualname -> [ExtraSpec]``（每组内按 order 排好）。"""
    by_group: dict[str, list[ExtraSpec]] = {}
    for spec in iter_extras():
        by_group.setdefault(spec.group, []).append(spec)
    for specs in by_group.values():
        specs.sort(key=lambda spec: (spec.order, spec.id))
    return by_group


def _warn_unmatched_extras(
        extras: dict[str, list[ExtraSpec]],
        groups: Sequence[dict[str, Any]],
) -> None:
    """注册到了不存在的分组上：记一条 warning，别让它静默消失。"""
    if not extras:
        return
    known: set[str] = set()

    def walk(nodes: Sequence[dict[str, Any]]) -> None:
        for node in nodes:
            known.add(str(node.get("key") or node.get("id") or ""))
            walk(node.get("groups") or [])

    walk(groups)
    for group_key in sorted(set(extras) - known):
        _logger.warning(
            "setting extras target an unknown group %r: %s",
            group_key,
            [spec.id for spec in extras[group_key]],
        )


def _apply_group(
        group: dict[str, Any],
        extras: dict[str, list[ExtraSpec]] | None = None,
) -> dict[str, Any] | None:
    key = str(group.get("key") or group.get("id") or "")
    patch = _GROUP_PATCHES.get(key)
    if patch is not None:
        if patch.hidden:
            return None
        if patch.title is not UNSET:
            group["title"] = patch.title
        if patch.description is not UNSET:
            group["description"] = patch.description
        if patch.order is not UNSET:
            group["order"] = patch.order

    items = []
    for item in group.get("items") or []:
        if not isinstance(item, dict):
            continue
        patched = _apply_item(item)
        if patched is not None:
            items.append(patched)
    # 允许补丁给「组内顺序」加权重；没补丁的叶子保持 dump 原有顺序
    items.sort(key=lambda it: (it.get("order", 0), it.get("name", "")))
    group["items"] = items

    children: list[dict[str, Any]] = []
    for child in group.get("groups") or []:
        if not isinstance(child, dict):
            continue
        surviving = _apply_group(child, extras)
        if surviving is not None:
            children.append(surviving)
    children.sort(key=lambda g: (g.get("order", 0), str(g.get("title", ""))))
    group["groups"] = children

    # 自定义行（动作 / 说明 / 插件控件）：渲染时与叶子、子分组按 order 混排
    group["extras"] = list((extras or {}).get(key, ()))

    group["item_count"] = len(items) + sum(
        int(child.get("item_count") or 0) for child in children
    )
    return group


def _apply_item(item: dict[str, Any]) -> dict[str, Any] | None:
    patch = _ITEM_PATCHES.get(str(item.get("key") or ""))
    if patch is None:
        return item

    if patch.hidden:
        return None

    if patch.label is not UNSET:
        item["label"] = patch.label
    if patch.description is not UNSET:
        item["description"] = patch.description
    if patch.widget is not UNSET:
        item["widget"] = (
            patch.widget.value
            if hasattr(patch.widget, "value")
            else str(patch.widget).strip().lower()
        )
    if patch.choices is not UNSET:
        item["choices"] = list(patch.choices or ())
    if patch.editable is not UNSET:
        item["editable"] = bool(patch.editable)
    if patch.default is not UNSET:
        item["default"] = patch.default
    if patch.order is not UNSET:
        item["order"] = patch.order

    constraints = dict(item.get("constraints") or {})
    for name in ("minimum", "maximum", "step"):
        value = getattr(patch, name)
        if value is not UNSET:
            constraints[name] = value
    item["constraints"] = constraints or None

    if patch.hint:
        item["hint"] = patch.hint
    if patch.choices_provider is not None:
        item["choices_provider"] = patch.choices_provider
    item["patch"] = patch
    return item


def _flatten(groups: Sequence[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    for group in groups:
        yield from group.get("items") or []
        yield from _flatten(group.get("groups") or [])


def _count_groups(groups: Sequence[dict[str, Any]]) -> int:
    return sum(
        1 + _count_groups(group.get("groups") or []) for group in groups
    )
