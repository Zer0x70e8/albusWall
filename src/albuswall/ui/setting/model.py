"""设置树的**只读模型**（纯数据，不 import Qt，不做 IO）。

一份 ``dump_schema_dict()`` + ``apply_present()`` 的产物被解析成一个
:class:`SettingTree`：

* :class:`Leaf` / :class:`Group` / :class:`Component` / :class:`UncoveredItem`
  是**不可变记录**（``Mapping`` 子类：既支持 ``leaf.get("widget")`` 这种旧式
  字典访问，也支持 ``leaf.widget`` 这种属性访问）；
* 派生字段（``by_file`` / ``unbound`` / ``summary`` / ``item_count``）
  **只在构造时算一次**，别处不再重算——这是 "派生字段两份实现" 的收口点；
* 索引（叶子表、分组表、父子关系）挂在树上，视图 / presenter / 布局再也不用
  自己递归遍历 ``dump["groups"]``。

为什么不是裸 dict
=================

裸 dict 的问题是"形状只活在注释里"：加一个字段没人检查、改一个名字没人发现、
不同副本之间（``view.dump`` 与 ``binder.leaf``）会静默分叉。这里的记录
**构造时归一化 + 之后不可写**，于是"谁改了它"这个问题消失。
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from copy import deepcopy
from types import MappingProxyType
from typing import Any

from albuswall.common.enums import SettingWidget
from albuswall.common.setting_widget import infer_widget

__all__ = [
    "SUPPORTED_SCHEMA_VERSIONS",
    "Component",
    "Group",
    "Leaf",
    "SettingSchemaError",
    "SettingTree",
    "UncoveredItem",
    "UnsupportedSchemaVersionError",
]

#: 模型认识的 dump 结构版本。``configue.schema.SCHEMA_DUMP_VERSION`` 变了
#: 就同步 +1；不认识就抛 :class:`UnsupportedSchemaVersionError`。
SUPPORTED_SCHEMA_VERSIONS: frozenset[int] = frozenset({2})

_WIDGET_VALUES: frozenset[str] = frozenset(member.value for member in SettingWidget)


class SettingSchemaError(ValueError):
    """dump 结构不合法（缺字段 / 重复 key / 版本不认识）。"""


class UnsupportedSchemaVersionError(SettingSchemaError):
    """``schema_version`` 不在 :data:`SUPPORTED_SCHEMA_VERSIONS` 里。"""


def _strip_none(value: Any) -> str | None:
    return str(value) if value else None


def _int_order(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


class _Record(Mapping[str, Any]):
    """不可变记录：内部 ``MappingProxyType``，对外同时是 Mapping 和属性袋。

    保留 Mapping 协议是为了让"旧式字典访问"（``leaf.get("value")``、
    ``leaf["key"]``、``dict(leaf)``）继续工作，迁移可以按调用点逐步进行；
    ``leaf.key`` 这类属性只是同一份数据的便捷读法。
    """

    __slots__ = ("_data",)

    def __init__(self, data: Mapping[str, Any]) -> None:
        self._data = MappingProxyType(dict(data))

    # ---------------- Mapping ----------------
    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __repr__(self) -> str:
        body = ", ".join(f"{key}={value!r}" for key, value in self._data.items())
        return f"{type(self).__name__}({body})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Mapping):
            return dict(self._data) == dict(other)
        return NotImplemented

    __hash__ = None  # type: ignore[assignment]  可变语义的容器不做 dict key

    # ---------------- 属性读法 ----------------
    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            return self._data[name]
        except KeyError:
            raise AttributeError(name) from None

    # ---------------- 派生 ----------------
    def replace(self, **changes: Any) -> _Record:
        """同类型的副本（不可变记录的"改一个字段"）。"""
        return type(self)({**self._data, **changes})


class Component(_Record):
    """自定义展示组件（当前只有 ``label``），不对应任何配置节点。"""

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> Component:
        return cls({
            "type": str(raw.get("type") or "label").strip().lower(),
            "text": str(raw.get("text") or ""),
            "object_name": str(raw.get("object_name") or ""),
            "word_wrap": bool(raw.get("word_wrap", True)),
        })


class Leaf(_Record):
    """一个设置项叶子（已归一化：字段齐全、``widget`` 已推导）。"""

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> Leaf:
        """补全展示字段；值的字段原样保留（只做深拷贝）。

        缺失字段的兜底全部显式写在这一个地方，视图不再做第二套推断：

        * ``type`` → ``"str"``；
        * ``widget`` → :func:`~albuswall.common.setting_widget.infer_widget`；
        * ``present`` → ``True``；``editable`` → ``True``；
        * ``target_file`` → ``None``（于是它会进 ``unbound``，而不是假装可写）。
        """
        out = deepcopy(dict(raw))
        key = str(out.get("key") or "")
        if not key:
            raise SettingSchemaError("leaf is missing required field 'key'")

        section = str(out.get("section") or "")
        config_key = str(out.get("config_key") or out.get("name") or "")
        if not config_key:
            config_key = key.rsplit(".", 1)[-1] if key else ""
        name = str(out.get("name") or config_key or key.rsplit(".", 1)[-1])
        group = str(out.get("group") or key.rsplit(".", 1)[0])

        choices = deepcopy(list(out.get("choices") or []))
        constraints = out.get("constraints")
        widget = _resolve_widget(out, choices)

        return cls({
            "key": key,
            "section": section,
            "config_key": config_key,
            "name": name,
            "group": group,
            "path": str(
                out.get("path")
                or (f"[{section}]{config_key}" if section else key)
            ),
            "label": str(out.get("label") or name),
            "description": str(out.get("description") or ""),
            "type": str(out.get("type") or "str"),
            "choices": choices,
            "choice_labels": deepcopy(dict(out.get("choice_labels") or {})),
            "constraints": deepcopy(dict(constraints)) if constraints else None,
            "widget": widget,
            "value": deepcopy(out.get("value")),
            "default": deepcopy(out.get("default")),
            "raw": deepcopy(
                out["value"] if "raw" not in out else out.get("raw"),
            ),
            "interpolated": bool(out.get("interpolated", False)),
            "present": bool(out.get("present", True)),
            "dirty": bool(out.get("dirty", False)),
            "editable": bool(out.get("editable", True)),
            "readonly": bool(out.get("readonly", False)),
            "read_error": _strip_none(out.get("read_error")),
            "advanced": bool(out.get("advanced", False)),
            "order": _int_order(out.get("order")),
            "origin_file": _strip_none(out.get("origin_file")),
            "target_file": _strip_none(out.get("target_file")),
            "source": str(out.get("source") or "declared"),
        })

    @property
    def node(self) -> tuple[str, str]:
        """外部通知的寻址键 ``(section, config_key)``。"""
        return str(self._data["section"]), str(self._data["config_key"])


def _resolve_widget(raw: Mapping[str, Any], choices: Sequence[Any]) -> str:
    """取 ``widget``；缺失 / 非法 / ``choice`` 却没选项时按类型兜底。"""
    widget = str(raw.get("widget") or "").strip().lower()
    if widget == SettingWidget.CHOICE.value and not choices:
        widget = ""
    if widget in _WIDGET_VALUES:
        return widget
    return infer_widget(str(raw.get("type") or ""), choices)


class Group(_Record):
    """一个设置分组（可嵌套，可携带自定义组件）。"""

    @property
    def leaves(self) -> tuple[Leaf, ...]:
        return tuple(self._data["items"])

    @property
    def child_groups(self) -> tuple[Group, ...]:
        return tuple(self._data["groups"])

    @classmethod
    def from_raw(
            cls,
            raw: Mapping[str, Any],
            *,
            seen_items: set[str],
            seen_groups: set[str],
    ) -> Group:
        key = str(raw.get("key") or raw.get("id") or "")
        if not key:
            raise SettingSchemaError("group is missing required field 'key'")
        if key in seen_groups:
            raise SettingSchemaError(f"duplicate group key {key!r}")
        seen_groups.add(key)

        items: list[Leaf] = []
        for item in raw.get("items") or ():
            leaf = Leaf.from_raw(item)
            if leaf.key in seen_items:
                raise SettingSchemaError(f"duplicate leaf key {leaf.key!r}")
            seen_items.add(leaf.key)
            items.append(leaf)

        children = [
            cls.from_raw(child, seen_items=seen_items, seen_groups=seen_groups)
            for child in raw.get("groups") or ()
        ]
        components = [
            Component.from_raw(component)
            for component in raw.get("components") or ()
            if isinstance(component, Mapping)
        ]
        editable = bool(raw.get("editable", True))
        item_count = len(items) + sum(
            int(child["item_count"]) for child in children
        )
        group = cls({
            "key": key,
            "id": str(raw.get("id") or key),
            "title": str(raw.get("title") or key),
            "description": str(raw.get("description") or ""),
            "order": _int_order(raw.get("order")),
            "editable": editable,
            "advanced": bool(raw.get("advanced", False)),
            "items": tuple(items),
            "groups": tuple(children),
            "components": tuple(components),
            "item_count": item_count,
        })
        if not editable:
            # 分组 editable=False 是**继承**给所有后代的（叶子的 editable 可能
            # 还是声明里的 True，视图不能只信叶子自己的字段）。
            group = _mark_uneditable(group)
        return group


def _mark_uneditable(group: Group) -> Group:
    """把一棵子树的 ``editable`` 全部压成 False（分组继承）。"""
    return group.replace(
        editable=False,
        items=tuple(item.replace(editable=False) for item in group.leaves),
        groups=tuple(_mark_uneditable(child) for child in group.child_groups),
    )


class UncoveredItem(_Record):
    """文件里有、但设置树枚举不到的节点（只读展示，无 binder）。"""

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> UncoveredItem:
        section = str(raw.get("section") or "")
        key = str(raw.get("key") or "")
        return cls({
            "section": section,
            "key": key,
            "path": str(
                raw.get("path") or (f"[{section}]{key}" if section else key)
            ),
            "typed": bool(raw.get("typed", False)),
            "value_repr": raw.get("value_repr"),
            "origin_file": _strip_none(raw.get("origin_file")),
            "target_file": _strip_none(raw.get("target_file")),
        })


class SettingTree(Mapping[str, Any]):
    """一份设置树：分组 + 扁平叶子 + 派生索引，**构造后不可变**。

    Mapping 协议暴露 ``schema_version`` / ``groups`` / ``items`` / ``by_file`` /
    ``unbound`` / ``uncovered`` / ``summary`` / ``source`` 这八个键，兼容
    ``view.dump["groups"]`` 这类旧式访问；其余索引走属性。
    """

    __slots__ = (
        "_by_file",
        "_flat_groups",
        "_groups",
        "_index",
        "_leaves",
        "_leaves_by_node",
        "_parent_of",
        "_source",
        "_summary",
        "_unbound",
        "_uncovered",
        "_version",
    )

    def __init__(
            self,
            *,
            version: int,
            groups: Sequence[Group],
            uncovered: Sequence[UncoveredItem],
            source: Mapping[str, Any],
    ) -> None:
        self._version = version
        self._groups = tuple(groups)
        self._uncovered = tuple(uncovered)
        self._source = deepcopy(dict(source))
        self._flat_groups = tuple(_iter_groups(self._groups))
        self._index = {group["key"]: group for group in self._flat_groups}
        self._leaves: dict[str, Leaf] = {}
        self._parent_of: dict[str, str] = {}
        for group in self._flat_groups:
            for child in group.child_groups:
                self._parent_of[child["key"]] = group["key"]
            for leaf in group.leaves:
                self._leaves[leaf.key] = leaf
                self._parent_of[leaf.key] = group["key"]
        self._leaves_by_node = {
            leaf.node: leaf.key for leaf in self._leaves.values()
        }
        by_file: dict[str, list[str]] = {}
        unbound: list[str] = []
        for leaf in self._leaves.values():
            if leaf["target_file"] is None:
                unbound.append(leaf.key)
            else:
                by_file.setdefault(str(leaf["target_file"]), []).append(leaf.key)
        self._by_file = MappingProxyType(
            {path: tuple(keys) for path, keys in by_file.items()},
        )
        self._unbound = tuple(unbound)
        self._summary = MappingProxyType({
            "group_count": len(self._flat_groups),
            "item_count": len(self._leaves),
            "file_count": len(self._by_file),
            "uncovered_count": len(self._uncovered),
        })

    # ---------------- 构造 ----------------
    @classmethod
    def parse(cls, dump: Mapping[str, Any]) -> SettingTree:
        """校验版本 + 归一化，返回不可变设置树。

        Raises:
            UnsupportedSchemaVersionError: ``schema_version`` 缺失或不在
                :data:`SUPPORTED_SCHEMA_VERSIONS` 里。
            SettingSchemaError: 重复 key / 缺 key / 不是 dict。
        """
        if not isinstance(dump, Mapping):
            raise SettingSchemaError(
                f"schema dump must be a mapping, got {type(dump).__name__}",
            )
        version = dump.get("schema_version")
        if isinstance(version, bool) or not isinstance(version, int):
            raise UnsupportedSchemaVersionError(
                f"schema_version must be an int, got {version!r}",
            )
        if version not in SUPPORTED_SCHEMA_VERSIONS:
            supported = ", ".join(
                str(item) for item in sorted(SUPPORTED_SCHEMA_VERSIONS)
            )
            raise UnsupportedSchemaVersionError(
                f"unsupported schema_version {version}; supported: {supported}",
            )

        groups_raw = dump.get("groups")
        if groups_raw is None:
            groups_raw = []
        if not isinstance(groups_raw, Sequence) or isinstance(
                groups_raw, (str, bytes),
        ):
            raise SettingSchemaError("dump field 'groups' must be a sequence")

        seen_items: set[str] = set()
        seen_groups: set[str] = set()
        groups = [
            Group.from_raw(
                group, seen_items=seen_items, seen_groups=seen_groups,
            )
            for group in groups_raw
        ]
        uncovered = [
            UncoveredItem.from_raw(entry)
            for entry in dump.get("uncovered") or ()
            if isinstance(entry, Mapping)
        ]
        return cls(
            version=version,
            groups=groups,
            uncovered=uncovered,
            source=dump.get("source") or {},
        )

    # ---------------- Mapping（兼容旧式访问） ----------------
    def __getitem__(self, key: str) -> Any:
        if key == "groups":
            return self._groups
        if key == "items":
            return tuple(self._leaves.values())
        if key == "by_file":
            return self._by_file
        if key == "unbound":
            return self._unbound
        if key == "uncovered":
            return self._uncovered
        if key == "summary":
            return self._summary
        if key == "schema_version":
            return self._version
        if key == "source":
            return self._source
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        yield from (
            "schema_version", "groups", "items", "by_file",
            "unbound", "uncovered", "summary", "source",
        )

    def __len__(self) -> int:
        return 8

    # ---------------- 读取 ----------------
    @property
    def schema_version(self) -> int:
        return self._version

    @property
    def groups(self) -> tuple[Group, ...]:
        """顶层分组（深优先序请用 :attr:`flat_groups`）。"""
        return self._groups

    @property
    def flat_groups(self) -> tuple[Group, ...]:
        """全部分组（深度优先，父在子前）。"""
        return self._flat_groups

    @property
    def items(self) -> tuple[Leaf, ...]:
        return tuple(self._leaves.values())

    @property
    def leaves(self) -> Mapping[str, Leaf]:
        return MappingProxyType(self._leaves)

    @property
    def by_file(self) -> Mapping[str, tuple[str, ...]]:
        return self._by_file

    @property
    def unbound(self) -> tuple[str, ...]:
        return self._unbound

    @property
    def uncovered(self) -> tuple[UncoveredItem, ...]:
        return self._uncovered

    @property
    def summary(self) -> Mapping[str, int]:
        return self._summary

    @property
    def source(self) -> dict[str, Any]:
        return self._source

    # ---------------- 索引 ----------------
    def leaf(self, key: str) -> Leaf | None:
        return self._leaves.get(key)

    def group(self, key: str) -> Group | None:
        return self._index.get(key)

    def parent_of(self, key: str) -> str | None:
        """叶子 / 分组的父分组 key；顶层分组返回 ``None``。"""
        return self._parent_of.get(key)

    def group_of(self, key: str) -> Group | None:
        """叶子所在分组；key 是分组时返回它自己。"""
        if key in self._index:
            return self._index[key]
        return self._index.get(self._parent_of.get(key, ""))

    def leaf_by_node(self, node: tuple[str, str]) -> Leaf | None:
        key = self._leaves_by_node.get(tuple(node))
        return self._leaves.get(key) if key else None

    def group_titles(self) -> dict[str, str]:
        return {group["key"]: str(group["title"]) for group in self._flat_groups}

    # ---------------- 序列化（调试 / 导出用） ----------------
    def to_dump(self) -> dict[str, Any]:
        """还原成普通 dict（与 ``dump_schema_dict`` 同形）。"""
        return {
            "schema_version": self._version,
            "groups": [_group_to_dict(group) for group in self._groups],
            "items": [dict(leaf) for leaf in self.items],
            "by_file": {path: list(keys) for path, keys in self._by_file.items()},
            "unbound": list(self._unbound),
            "uncovered": [dict(entry) for entry in self._uncovered],
            "summary": dict(self._summary),
            "source": deepcopy(self._source),
        }


def _iter_groups(groups: Iterable[Group]) -> Iterator[Group]:
    for group in groups:
        yield group
        yield from _iter_groups(group.child_groups)


def _group_to_dict(group: Group) -> dict[str, Any]:
    return {
        "key": group["key"],
        "id": group["id"],
        "title": group["title"],
        "description": group["description"],
        "order": group["order"],
        "editable": group["editable"],
        "advanced": group["advanced"],
        "items": [dict(leaf) for leaf in group.leaves],
        "groups": [_group_to_dict(child) for child in group.child_groups],
        "components": [dict(component) for component in group["components"]],
        "item_count": group["item_count"],
    }
