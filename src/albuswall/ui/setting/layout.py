#
"""设置页的**布局再处理**（presentation-only，不写回配置）。

在 ``dump_schema_dict()`` + ``apply_present()`` 之后、交给
:class:`~albuswall.ui.setting.SettingView` 之前，应用一份用户布局：

1. **认领**（claims）—— 把「未覆盖」节点登记进设置树。真正的注册动作在
   presenter（``register_setting_item``），本模块只保存 claim 记录；
2. **移动**（moves）—— 把叶子换到别的分组（例如把 ``ui.theme`` 从「ui」
   分到独立的「外观」组）。移动**只改 dump 的树结构**，叶子的 ``key`` 保持
   原 qualname，因此写回键不变；
3. **自定义组件**（groups[].components）—— 不绑定配置节点的展示块，例如
   「关于」分组里的一段 label。

生命周期
========

布局**不是配置文件**，而是**容器注入的运行时对象**（``container.set(
"setting_layout", SettingLayout())``）：presenter 通过依赖注入拿到它，
和窗口状态 / 主题一样"由装配根提供"，不需要用户去编辑任何文件、也不落盘。

:meth:`SettingLayout.to_dict` / :meth:`SettingLayout.from_dict` 仍保留，
只作为**纯数据**的序列化形态（调试导出、将来接外部状态存储时用），
本模块自己做 IO 的事情已经删掉了。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "BUILTIN_THEME_GROUP",
    "BUILTIN_THEME_ORDER",
    "LAYOUT_VERSION",
    "LayoutClaim",
    "LayoutGroup",
    "LayoutMove",
    "SettingLayout",
    "apply_layout",
    "builtin_layout",
    "merge_layouts",
]

logger = logging.getLogger(__package__)

#: 布局对象的结构版本（序列化形态变了就 +1；不认识的版本按空布局处理）。
LAYOUT_VERSION = 1


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass
class LayoutMove:
    """把 ``key``（叶子的 qualname）移动到 ``group``（目标分组 key）。"""

    key: str
    group: str
    order: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "group": self.group, "order": self.order}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> LayoutMove | None:
        key = str(data.get("key") or "").strip()
        group = str(data.get("group") or "").strip()
        if not key or not group:
            return None
        return cls(key=key, group=group, order=_int(data.get("order")))


@dataclass
class LayoutComponent:
    """自定义展示组件（目前只有 label）。"""

    type: str = "label"
    text: str = ""
    object_name: str = ""
    word_wrap: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "text": self.text,
            "object_name": self.object_name,
            "word_wrap": self.word_wrap,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> LayoutComponent:
        return cls(
            type=str(data.get("type") or "label"),
            text=str(data.get("text") or ""),
            object_name=str(data.get("object_name") or ""),
            word_wrap=bool(data.get("word_wrap", True)),
        )


@dataclass
class LayoutGroup:
    """一个**展示用**分组（只承载 components，或作为 move 的目标）。"""

    key: str
    title: str = ""
    description: str = ""
    order: int = 900
    components: list[LayoutComponent] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title or self.key,
            "description": self.description,
            "order": self.order,
            "components": [component.to_dict() for component in self.components],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> LayoutGroup | None:
        key = str(data.get("key") or "").strip()
        if not key:
            return None
        components = [
            LayoutComponent.from_dict(component)
            for component in data.get("components") or ()
            if isinstance(component, Mapping)
        ]
        return cls(
            key=key,
            title=str(data.get("title") or key),
            description=str(data.get("description") or ""),
            order=_int(data.get("order"), 900),
            components=components,
        )


@dataclass
class LayoutClaim:
    """一条「认领未声明节点」的记录（真正登记在 presenter 里做）。"""

    section: str
    key: str
    group: str
    name: str = ""
    label: str = ""
    description: str = ""
    widget: str = ""
    advanced: bool = False
    order: int = 0
    group_title: str = ""
    group_order: int = 900
    typed: bool = False
    target_file: str | None = None

    def __post_init__(self) -> None:
        self.name = self.name or self.key
        self.label = self.label or self.name

    @property
    def node(self) -> tuple[str, str]:
        return self.section, self.key

    @property
    def qualname(self) -> str:
        return f"{self.group}.{self.name}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "section": self.section,
            "key": self.key,
            "group": self.group,
            "name": self.name,
            "label": self.label,
            "description": self.description,
            "widget": self.widget,
            "advanced": self.advanced,
            "order": self.order,
            "group_title": self.group_title,
            "group_order": self.group_order,
            "typed": self.typed,
            "target_file": self.target_file,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> LayoutClaim | None:
        section = str(data.get("section") or "")
        key = str(data.get("key") or "").strip()
        group = str(data.get("group") or "").strip()
        if not key or not group:
            return None
        return cls(
            section=section,
            key=key,
            group=group,
            name=str(data.get("name") or key),
            label=str(data.get("label") or ""),
            description=str(data.get("description") or ""),
            widget=str(data.get("widget") or ""),
            advanced=bool(data.get("advanced", False)),
            order=_int(data.get("order")),
            group_title=str(data.get("group_title") or ""),
            group_order=_int(data.get("group_order"), 900),
            typed=bool(data.get("typed", False)),
            target_file=(
                str(data["target_file"]) if data.get("target_file") else None
            ),
        )


@dataclass
class SettingLayout:
    """一份完整的设置页再处理布局。"""

    version: int = LAYOUT_VERSION
    moves: list[LayoutMove] = field(default_factory=list)
    groups: list[LayoutGroup] = field(default_factory=list)
    claims: list[LayoutClaim] = field(default_factory=list)

    # ------------------------------------------------------------------
    # 序列化
    # ------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "moves": [move.to_dict() for move in self.moves],
            "groups": [group.to_dict() for group in self.groups],
            "claims": [claim.to_dict() for claim in self.claims],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SettingLayout:
        version = _int(data.get("version"), 0)
        if version != LAYOUT_VERSION:
            logger.warning(
                "setting layout: unsupported version %r; ignoring", version,
            )
            return cls()
        moves = [
            move for move in (
                LayoutMove.from_dict(item)
                for item in data.get("moves") or ()
                if isinstance(item, Mapping)
            ) if move is not None
        ]
        groups = [
            group for group in (
                LayoutGroup.from_dict(item)
                for item in data.get("groups") or ()
                if isinstance(item, Mapping)
            ) if group is not None
        ]
        claims = [
            claim for claim in (
                LayoutClaim.from_dict(item)
                for item in data.get("claims") or ()
                if isinstance(item, Mapping)
            ) if claim is not None
        ]
        return cls(version=version, moves=moves, groups=groups, claims=claims)

    # ------------------------------------------------------------------
    # 变更
    # ------------------------------------------------------------------
    def move_for(self, key: str) -> LayoutMove | None:
        return next((move for move in self.moves if move.key == key), None)

    def set_move(
            self,
            key: str,
            group: str,
            *,
            order: int = 0,
            group_title: str = "",
            group_description: str = "",
            group_order: int = 0,
    ) -> LayoutMove:
        """登记/替换一个叶子的位置；给了新分组信息就一并登记展示分组。"""
        if group_title or group_description or group_order:
            self.ensure_group(
                group,
                title=group_title,
                description=group_description,
                order=group_order or 900,
            )
        self.remove_move(key)
        move = LayoutMove(key=key, group=group, order=int(order))
        self.moves.append(move)
        return move

    def remove_move(self, key: str) -> bool:
        before = len(self.moves)
        self.moves = [move for move in self.moves if move.key != key]
        return len(self.moves) != before

    def ensure_group(
            self,
            key: str,
            *,
            title: str = "",
            description: str = "",
            order: int = 900,
    ) -> LayoutGroup:
        group = next((item for item in self.groups if item.key == key), None)
        if group is None:
            group = LayoutGroup(
                key=key,
                title=title or key,
                description=description,
                order=int(order),
            )
            self.groups.append(group)
        else:
            if title:
                group.title = title
            if description:
                group.description = description
            if order:
                group.order = int(order)
        return group

    def add_component_group(
            self,
            key: str,
            *,
            title: str = "",
            description: str = "",
            order: int = 900,
            label: str = "",
    ) -> LayoutGroup:
        group = self.ensure_group(
            key, title=title, description=description, order=order,
        )
        if label:
            group.components.append(LayoutComponent(text=label))
        return group

    def add_claim(self, claim: LayoutClaim) -> None:
        """登记 claim（同一个 ``(section, key)`` 只保留最后一条）。"""
        self.claims = [
            item for item in self.claims
            if item.node != claim.node
        ]
        self.claims.append(claim)

    def has_claim(self, section: str, key: str) -> bool:
        return any(item.node == (section, key) for item in self.claims)

    def has_group(self, key: str) -> bool:
        return any(item.key == key for item in self.groups)


# ============================================================================
# 内置默认布局
# ============================================================================

#: 内置「主题」分组：把 ``[ui] theme``（主题名）与 ``[path] theme``（主题目录）
#: 收进同一个展示分组。这是**默认展示**，不写回配置，也随时可被用户移动覆盖。
BUILTIN_THEME_GROUP = "theme"

#: 内置分组在顶层的排序位（现有段全是 0，所以它排在最后几个常识分组之后、
#: 「快捷键」(900) / 「关于」(9999) 之前）。
BUILTIN_THEME_ORDER = 50


def builtin_layout() -> SettingLayout:
    """内置默认布局：先于用户布局应用，用户布局按 key 覆盖它。"""
    layout = SettingLayout()
    layout.ensure_group(
        BUILTIN_THEME_GROUP,
        title="主题",
        description="外观主题与主题目录（默认收在一起，可右键移动到别处）。",
        order=BUILTIN_THEME_ORDER,
    )
    layout.set_move("ui.theme", BUILTIN_THEME_GROUP, order=0)
    layout.set_move("path.theme", BUILTIN_THEME_GROUP, order=1)
    return layout


def merge_layouts(
        base: SettingLayout, overrides: SettingLayout,
) -> SettingLayout:
    """把用户布局叠加到内置默认布局上（同 key 的 move 用用户的）。

    语义：内置默认只提供"初始摆放"，用户在界面上的移动 / 组件 / 认领永远优先。
    """
    merged = SettingLayout.from_dict(base.to_dict())
    for group in overrides.groups:
        target = merged.ensure_group(
            group.key,
            title=group.title,
            description=group.description,
            order=group.order,
        )
        target.components = [
            LayoutComponent.from_dict(component.to_dict())
            for component in group.components
        ]
    for move in overrides.moves:
        merged.moves = [item for item in merged.moves if item.key != move.key]
        merged.moves.append(move)
    merged.claims = [
        LayoutClaim.from_dict(claim.to_dict()) for claim in overrides.claims
    ]
    return merged


# ============================================================================
# dump 变换
# ============================================================================

def apply_layout(
        dump: Mapping[str, Any], layout: SettingLayout,
) -> dict[str, Any]:
    """把布局应用到 dump（深拷贝；叶子 ``key`` 保持不变）。"""
    result = deepcopy(dict(dump))
    groups = result.get("groups") or []
    group_index = _index_groups(groups)

    # 1) 自定义分组（只含 components 的展示块 / move 的目标）
    for definition in layout.groups:
        target = group_index.get(definition.key)
        if target is None:
            target = _new_group(definition)
            groups.append(target)
            group_index[definition.key] = target
        else:
            if definition.title:
                target["title"] = definition.title
            if definition.description:
                target["description"] = definition.description
            if definition.order:
                target["order"] = definition.order
        existing = target.setdefault("components", [])
        existing.extend(
            component.to_dict() for component in definition.components
        )

    # 2) 移动叶子
    leaf_index = _index_leaves(groups)
    for move in layout.moves:
        entry = leaf_index.get(move.key)
        if entry is None:
            logger.debug("setting layout: move target %r not found", move.key)
            continue
        leaf, parent = entry
        target = group_index.get(move.group)
        if target is None:
            # 布局里没声明目标组：建一个最简分组，不丢条目。
            target = _new_group(LayoutGroup(key=move.group, title=move.group))
            groups.append(target)
            group_index[move.group] = target
        if parent is not target:
            parent["items"] = [
                item for item in parent.get("items") or ()
                if item.get("key") != move.key
            ]
            leaf["group"] = move.group
            target.setdefault("items", []).append(leaf)
        leaf["order"] = int(move.order)
        leaf_index[move.key] = (leaf, target)

    # 3) **不重算派生字段**：items / by_file / unbound / item_count / summary
    #    由 ``SettingTree.parse`` 在归一化时统一算一次（唯一实现）。
    #    本函数只负责"树怎么摆"，算统计不是它的职责。
    # 顶层分组按 ``order`` 稳定排序：内置「主题」分组(50) 因此排在常识分组之后、
    # 「快捷键」(900) / 「关于」(9999) 之前；未写 order 的段保持原顺序。
    groups = [
        group
        for _index, group in sorted(
            enumerate(groups),
            key=lambda entry: (int(entry[1].get("order") or 0), entry[0]),
        )
    ]
    result["groups"] = groups
    return result


def _index_groups(
        groups: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}

    def walk(group: Mapping[str, Any]) -> None:
        key = str(group.get("key") or "")
        if key:
            index[key] = dict(group) if not isinstance(group, dict) else group
        for child in group.get("groups") or ():
            walk(child)

    for group in groups:
        walk(group)
    return index


def _index_leaves(
        groups: list[dict[str, Any]],
) -> dict[str, tuple[dict[str, Any], dict[str, Any]]]:
    index: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}

    def walk(group: dict[str, Any]) -> None:
        for item in group.get("items") or ():
            index[str(item.get("key") or "")] = (item, group)
        for child in group.get("groups") or ():
            walk(child)

    for group in groups:
        walk(group)
    return index


def _new_group(definition: LayoutGroup) -> dict[str, Any]:
    return {
        "id": definition.key,
        "key": definition.key,
        "title": definition.title or definition.key,
        "description": definition.description,
        "order": int(definition.order),
        "editable": True,
        "advanced": False,
        "items": [],
        "groups": [],
        "components": [],
        "item_count": 0,
    }


