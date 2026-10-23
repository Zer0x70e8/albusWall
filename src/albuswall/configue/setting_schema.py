#
"""配置结构注册表（软件级基础设施）。

整个软件的配置最终都以 **dict 默认值 / INI 节点** 的形态被消费。上层
（设置页、CLI、TUI、Web、文档生成器……）需要的是一份「带层级分组、
可编辑、能按归属文件回写的结构」。本模块提供这套登记基础设施，**不依赖
任何前端**（Qt / 终端 / Web 都可消费）：

- ``SettingItem``：一个可编辑叶子，绑定 ``(section, key)`` 配置节点；
- ``SettingGroup``：任意深度的分组，可包含子分组 / 叶子；
- ``SettingSchemaRegistry``：全局登记表，支持组合、去重、子分组挂载；
- 装饰器 / 快捷函数：``@setting_group(...)``、``setting_item(...)``、
  ``register_setting_group`` / ``register_setting_item``；
- dump：``dump_setting_schema``（嵌套 dict）、
  ``dump_setting_schema_by_file``（按归属文件）、
  ``format_setting_schema_dump``（给日志 / 终端看的文本）；
- 回写：``save_setting_group`` / ``save_setting_schema``，按每个叶子
  的归属文件分组，交给 ``Configue.flush`` 原子落盘。

``SettingWidget`` / ``label`` / ``description`` 都只是**展示提示**，不是
UI 依赖：前端可以选择忽略，或映射成自己的控件。当前最典型的消费方是
设置页面，但同一份登记表也可以驱动命令行帮助、配置文档、测试夹具等。

设计约定
========

导入期安全
    登记只写内存表，不读配置、不读盘。取值 / 归属文件到 dump / save 时才
    解析，因此本模块可以在配置 phase 之前被 import。

默认值 / 类型
    ``SettingItem`` 可以显式给 ``default`` / ``value_type``，也可以直接
    用 ``SettingItem.from_field(..., ConfigField 实例)`` 从现有声明继承
    path / default。dump 时优先采用 ``Resolver`` 里已注册的转换器类型。

归属文件
    每个叶子的写回文件按以下优先级确定：

    1. ``SettingItem.file``（显式覆盖）
    2. 所在 ``SettingGroup.file``（分组继承）
    3. ``Configue.target_file_of(section, key)``（origin → 可写备选 →
       ``default_write_file``，与 ``flush`` 完全同一套规则）

示例
====

.. code-block:: python

    from albuswall.configue.setting_schema import (
        SettingGroup, SettingItem, register_setting_group,
        dump_setting_schema, format_setting_schema_dump,
    )

    general = SettingGroup("general", "常规").add(
        SettingItem(
            "language", "general", "language",
            label="语言", widget="choice", choices=("zh_CN", "en_US"),
        ),
    )

    appearance = SettingGroup("appearance", "外观").add(
        SettingItem("theme", "ui", "theme", label="主题"),
        SettingGroup("icons", "图标").add(
            SettingItem("icon_dir", "ui", "icon_dir", label="图标目录"),
        ),
    )

    register_setting_group(general)
    register_setting_group(appearance)

    print(format_setting_schema_dump())

装饰器形态（叶子直接写成类属性）::

    from albuswall.configue.setting_schema import (
        setting_group, setting_item,
    )

    @setting_group("appearance", title="外观", order=10)
    class Appearance:
        theme = setting_item("theme", "ui", "theme", label="主题")

也可以直接复用既有声明::

    from albuswall.ui.builtin.config.preference import PreferenceConfig

    language = setting_item("language", PreferenceConfig.language, label="语言")
"""

from __future__ import annotations

import types
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Optional, Union, get_args, get_origin

from albuswall.log import getLogger

__all__ = [
    "SettingGroup",
    "SettingItem",
    "SettingSchemaRegistry",
    "SettingWidget",
    "SettingWriteResult",
    "SETTING_SCHEMA",
    "dump_registered_groups",
    "dump_setting_schema",
    "dump_setting_schema_by_file",
    "format_registered_groups",
    "format_setting_schema_dump",
    "get_setting_group",
    "register_setting_group",
    "register_setting_item",
    "save_setting_group",
    "save_setting_schema",
    "setting_group",
    "setting_item",
]

_logger = getLogger("albuswall.configue.setting_schema")

_UNION_ORIGINS = (Union, getattr(types, "UnionType", Union))


class _Unset:
    """哨兵：区分「没给默认值」与「默认值就是 None」。"""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - 只用于调试
        return "<unset>"

    def __bool__(self) -> bool:
        return False


UNSET = _Unset()


# ══════════════════════════════════════════════════
# 类型 / 控件推导
# ══════════════════════════════════════════════════
class SettingWidget(str, Enum):
    """设置页给 UI 层用的控件提示。"""

    AUTO = "auto"
    TEXT = "text"
    MULTILINE = "multiline"
    INT = "int"
    FLOAT = "float"
    BOOL = "bool"
    CHOICE = "choice"
    PATH = "path"
    FILE = "file"
    DIR = "dir"
    SECRET = "secret"


def _unwrap_optional(value_type: Any) -> Any:
    """展开 ``Optional`` / ``Union``，返回第一个非 None 分支。"""
    origin = get_origin(value_type)
    if origin not in _UNION_ORIGINS:
        return value_type
    for arg in get_args(value_type):
        if arg is not type(None):
            return arg
    return value_type


def type_name(value_type: Any) -> str:
    """把类型 / 注解显示成人类可读的名字。"""
    if value_type is None or value_type is UNSET:
        return "?"
    if value_type is Any:
        return "Any"
    if value_type is type(None):
        return "None"

    origin = get_origin(value_type)
    if origin is None:
        name = getattr(value_type, "__name__", None)
        if name:
            return name
        return str(value_type).replace("typing.", "")

    if origin in _UNION_ORIGINS:
        args = get_args(value_type)
        non_none = [arg for arg in args if arg is not type(None)]
        has_none = len(non_none) != len(args)
        inner = " | ".join(type_name(arg) for arg in non_none) or "None"
        return f"{inner} | None" if has_none else inner

    if origin is list:
        args = get_args(value_type)
        return f"list[{type_name(args[0])}]" if args else "list"
    if origin is tuple:
        args = get_args(value_type)
        if not args:
            return "tuple"
        if len(args) == 2 and args[1] is Ellipsis:
            return f"tuple[{type_name(args[0])}, ...]"
        return "tuple[" + ", ".join(type_name(arg) for arg in args) + "]"
    if origin is dict:
        args = get_args(value_type)
        if len(args) == 2:
            return f"dict[{type_name(args[0])}, {type_name(args[1])}]"
        return "dict"
    return str(value_type).replace("typing.", "")


def _literal_choices(value_type: Any) -> tuple[Any, ...]:
    """``Literal["a", "b"]`` → ``("a", "b")``；其余返回空元组。"""
    if get_origin(value_type) is Literal:
        return tuple(get_args(value_type))
    return ()


def infer_widget(
        value_type: Any = None,
        choices: Sequence[Any] = (),
) -> SettingWidget:
    """按类型 / 枚举选项推导控件。"""
    if choices or _literal_choices(value_type):
        return SettingWidget.CHOICE
    inner = _unwrap_optional(value_type)
    if inner is bool:
        return SettingWidget.BOOL
    if inner is int:
        return SettingWidget.INT
    if inner is float:
        return SettingWidget.FLOAT
    if inner is Path:
        return SettingWidget.PATH
    if inner is str:
        return SettingWidget.TEXT
    return SettingWidget.AUTO


def _normalize_widget(
        widget: Union[SettingWidget, str, None],
        value_type: Any = None,
        choices: Sequence[Any] = (),
) -> SettingWidget:
    if widget is None:
        return infer_widget(value_type, choices)
    if isinstance(widget, SettingWidget):
        return infer_widget(value_type, choices) \
            if widget is SettingWidget.AUTO else widget
    text = str(widget).strip().lower()
    try:
        parsed = SettingWidget(text)
    except ValueError as exc:
        raise ValueError(
            f"unknown setting widget {widget!r}; "
            f"expected one of {[item.value for item in SettingWidget]}"
        ) from exc
    return infer_widget(value_type, choices) \
        if parsed is SettingWidget.AUTO else parsed


# ══════════════════════════════════════════════════
# 值序列化 / 展示
# ══════════════════════════════════════════════════
def _jsonable(value: Any) -> Any:
    """把配置值收敛成 JSON 友好的结构（Path / Enum / 容器）。"""
    if value is UNSET:
        return None
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return repr(value)


def _value_repr(value: Any, limit: int = 48) -> str:
    if value is UNSET:
        text = "<unset>"
    elif isinstance(value, (str, Path)):
        text = repr(str(value))
    else:
        text = repr(_jsonable(value))
    if len(text) > limit:
        text = text[: max(limit - 1, 0)] + "…"
    return text


def _split_path(raw: Sequence[str]) -> tuple[str, str]:
    """与 ``ConfigField._split_path`` 同语义：1 段落 ``__default__``。"""
    parts = tuple(raw)
    if len(parts) == 2:
        return str(parts[0]), str(parts[1])
    if len(parts) == 1:
        return "__default__", str(parts[0])
    raise ValueError(
        f"setting item path must have 1 or 2 parts, got {list(parts)!r}"
    )


# ══════════════════════════════════════════════════
# 叶子
# ══════════════════════════════════════════════════
@dataclass(frozen=True)
class SettingItem:
    """设置页的一个可编辑叶子，绑定一个配置节点。

    Args:
        name: 叶子名（同一分组内唯一；为空时取 ``key``）。
        section: 配置节名；一节 path 的字段请写 ``"__default__"``。
        key: 配置键名。
        label: UI 展示名，默认取 ``name``。
        description: 帮助文本。
        widget: 控件提示（``SettingWidget`` / 字符串），``auto`` 按类型推导。
        choices: 枚举选项；取 ``Literal`` 类型时也可留空。
        choice_labels: 选项值 → 展示文本（例如语言代码 → 母语名）。
            UI 只把它当成「给 choice 穿的显示外套」，写回配置的仍是原值。
        minimum / maximum / step: 数值控件范围（可选）。
        editable: 是否允许用户编辑。
        order: 同层排序权重（小的在前）。
        default: 配置缺失时展示的默认值；不给则尝试从声明继承。
        value_type: 值类型；不给则尝试从 default 推断。
        file: 显式归属文件；不给则继承分组 / origin / 默认落点。
        declaration: 可选 ``ConfigField``，用于继承 path / default。
    """

    name: str = ""
    section: str = ""
    key: str = ""
    label: str = ""
    description: str = ""
    widget: Union[SettingWidget, str, None] = SettingWidget.AUTO
    choices: tuple[Any, ...] = ()
    #: 选项值 → 展示文本；空表示直接显示值本身。
    choice_labels: Mapping[str, str] = field(default_factory=dict)
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    step: Optional[float] = None
    editable: bool = True
    order: int = 0
    default: Any = UNSET
    value_type: Any = None
    file: Optional[Path] = None
    declaration: Any = None

    def __post_init__(self) -> None:
        declaration = self.declaration

        # 1) 从声明继承 path / default（显式给出的字段优先）
        if declaration is not None:
            declared_path = getattr(declaration, "path", None)
            if declared_path:
                decl_section, decl_key = _split_path(declared_path)
                if self.section and self.section != decl_section:
                    raise ValueError(
                        f"setting item {self.name or self.key!r}: section "
                        f"{self.section!r} conflicts with declaration "
                        f"path {tuple(declared_path)!r}"
                    )
                if self.key and self.key != decl_key:
                    raise ValueError(
                        f"setting item {self.name or self.key!r}: key "
                        f"{self.key!r} conflicts with declaration "
                        f"path {tuple(declared_path)!r}"
                    )
                object.__setattr__(self, "section", decl_section)
                object.__setattr__(self, "key", decl_key)
            if self.default is UNSET:
                declared_default = getattr(declaration, "default", UNSET)
                if declared_default is not UNSET:
                    object.__setattr__(self, "default", declared_default)

        if not self.section or not self.key:
            raise ValueError(
                f"setting item {self.name or self.key or '?'!r}: "
                f"section and key are required"
            )
        if not self.name:
            object.__setattr__(self, "name", self.key)

        choices = self.choices
        if choices is None:  # type: ignore[comparison-overlap]
            choices = ()
        elif isinstance(choices, (str, bytes)):
            choices = (choices,)
        else:
            choices = tuple(choices)
        object.__setattr__(self, "choices", choices)

        # 选项展示名：统一成 ``{str(value): label}``，避免列表 / None 混进来
        labels = self.choice_labels or {}
        if not isinstance(labels, Mapping):
            raise TypeError(
                f"setting item {self.name or '?'!r}: choice_labels must be "
                f"a mapping, got {type(labels).__name__}"
            )
        object.__setattr__(
            self, "choice_labels", {str(k): str(v) for k, v in labels.items()}
        )

        # 控件提示在登记期就校验，拼错名字不要拖到 dump / 渲染阶段
        if not isinstance(self.widget, SettingWidget) and self.widget is not None:
            text = str(self.widget).strip().lower()
            try:
                SettingWidget(text)
            except ValueError as exc:
                raise ValueError(
                    f"setting item {self.name!r}: unknown widget "
                    f"{self.widget!r}; expected one of "
                    f"{[item.value for item in SettingWidget]}"
                ) from exc
            object.__setattr__(self, "widget", SettingWidget(text))

        if self.value_type is None and self.default is not UNSET \
                and self.default is not None:
            object.__setattr__(self, "value_type", type(self.default))

        if self.file is not None:
            object.__setattr__(self, "file", Path(self.file))

    # ── 派生视图 ──────────────────────────────────
    @property
    def path(self) -> tuple[str, str]:
        return self.section, self.key

    @property
    def node(self) -> tuple[str, str]:
        return self.section, self.key

    @property
    def display_label(self) -> str:
        return self.label or self.name

    @property
    def path_text(self) -> str:
        return f"[{self.section}]{self.key}"

    def coerce(self, value: Any) -> Any:
        """把 UI 传来的字符串按 ``value_type`` 转成配置值（其他类型原样）。"""
        if value is None or self.value_type is None:
            return value
        if not isinstance(value, str):
            if self.value_type is bool and isinstance(value, int):
                return bool(value)
            return value
        # 复用配置文件那套转换语义（bool / int / Optional / list ...）
        from .utils.ini_parser import _convert

        try:
            return _convert(value, self.value_type, None)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(
                f"setting item {self.name!r}: cannot convert {value!r} "
                f"to {type_name(self.value_type)}: {exc}"
            ) from exc

    @classmethod
    def from_field(
            cls,
            name: str,
            declaration: Any,
            **kwargs: Any,
    ) -> "SettingItem":
        """从 ``ConfigField`` / ``PathField`` 实例继承 path 与默认值。"""
        raw_path = getattr(declaration, "path", None)
        if not raw_path:
            raise TypeError(
                f"SettingItem.from_field({name!r}): expected a ConfigField-like "
                f"object with .path, got {type(declaration).__name__}"
            )
        section, key = _split_path(raw_path)
        kwargs.pop("section", None)
        kwargs.pop("key", None)
        return cls(
            name=name,
            section=section,
            key=key,
            declaration=declaration,
            **kwargs,
        )


# ══════════════════════════════════════════════════
# 分组
# ══════════════════════════════════════════════════
class SettingGroup:
    """设置页分组，可包含任意深度的子分组与叶子。

    ``id`` 是**当前层**的名字；全局键由父链路拼成
    （``appearance.icons``），见 ``qualname``。
    """

    def __init__(
            self,
            id: str,
            title: Optional[str] = None,
            *,
            description: str = "",
            file: Optional[Path] = None,
            order: int = 0,
            editable: bool = True,
    ) -> None:
        text = str(id or "").strip()
        if not text:
            raise ValueError("setting group id must be a non-empty string")
        self.id = text
        self.title = title or self.id
        self.description = description
        self.file = Path(file) if file is not None else None
        self.order = order
        self.editable = editable
        self._children: list[Union["SettingGroup", SettingItem]] = []
        self._parent: Optional["SettingGroup"] = None

    # ── 组装 ──────────────────────────────────────
    def add(self, *nodes: Union["SettingGroup", SettingItem]) -> "SettingGroup":
        """追加子分组 / 叶子，返回自身（支持链式写法）。"""
        for node in nodes:
            if isinstance(node, SettingItem):
                if any(
                    isinstance(child, SettingItem)
                    and child.name == node.name
                    for child in self._children
                ):
                    raise ValueError(
                        f"setting group {self.qualname!r}: duplicate item "
                        f"name {node.name!r}"
                    )
                self._children.append(node)
                continue
            if not isinstance(node, SettingGroup):
                raise TypeError(
                    f"setting group {self.qualname!r}: expected SettingGroup "
                    f"or SettingItem, got {type(node).__name__}"
                )
            if node is self:
                raise ValueError("setting group cannot contain itself")
            if node._parent is not None and node._parent is not self:
                raise ValueError(
                    f"setting group {node.qualname!r} is already attached to "
                    f"{node._parent.qualname!r}"
                )
            ancestor: Optional[SettingGroup] = self
            while ancestor is not None:
                if ancestor is node:
                    raise ValueError("setting group cycle detected")
                ancestor = ancestor._parent
            node._parent = self
            self._children.append(node)
        return self

    extend = add

    # ── 视图 ──────────────────────────────────────
    @property
    def children(self) -> tuple[Union["SettingGroup", SettingItem], ...]:
        return tuple(self._children)

    @property
    def items(self) -> tuple[SettingItem, ...]:
        return tuple(
            child for child in self._children
            if isinstance(child, SettingItem)
        )

    @property
    def groups(self) -> tuple["SettingGroup", ...]:
        return tuple(
            child for child in self._children
            if isinstance(child, SettingGroup)
        )

    @property
    def parent(self) -> Optional["SettingGroup"]:
        return self._parent

    @property
    def qualname(self) -> str:
        parts: list[str] = []
        node: Optional[SettingGroup] = self
        while node is not None:
            parts.append(node.id)
            node = node._parent
        return ".".join(reversed(parts))

    def iter_groups(self) -> Iterator["SettingGroup"]:
        yield self
        for child in self.groups:
            yield from child.iter_groups()

    def walk_items(
            self,
            inherited_file: Optional[Path] = None,
    ) -> Iterator[tuple["SettingGroup", SettingItem, Optional[Path]]]:
        """递归产出 ``(所属分组, 叶子, 继承的归属文件)``。"""
        group_file = self.file if self.file is not None else inherited_file
        for child in self._children:
            if isinstance(child, SettingItem):
                yield self, child, group_file
            else:
                yield from child.walk_items(group_file)

    def get_item(self, name: str) -> Optional[SettingItem]:
        """按叶子名查找（先直接子项，再递归）。"""
        for item in self.items:
            if item.name == name:
                return item
        for child in self.groups:
            found = child.get_item(name)
            if found is not None:
                return found
        return None

    def __iter__(self) -> Iterator[Union["SettingGroup", SettingItem]]:
        return iter(self._children)

    def __len__(self) -> int:
        return len(self._children)

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(id={self.id!r}, "
            f"items={len(self.items)}, groups={len(self.groups)})"
        )


# ══════════════════════════════════════════════════
# 全局登记表
# ══════════════════════════════════════════════════
def _coerce_group(parent: Any, registry: "SettingSchemaRegistry") -> SettingGroup:
    if isinstance(parent, SettingGroup):
        group = parent
    elif isinstance(parent, str):
        group = registry.get(parent)
        if group is None:
            raise KeyError(f"setting group {parent!r} is not registered")
    else:
        raise TypeError(
            f"parent must be a SettingGroup or group id, got "
            f"{type(parent).__name__}"
        )
    if registry.get(group.qualname) is not group:
        raise KeyError(
            f"setting group {group.qualname!r} is not registered"
        )
    return group


class SettingSchemaRegistry:
    """设置页结构登记表：支持分组组合、子分组挂载与重名检测。"""

    def __init__(self) -> None:
        self._roots: list[SettingGroup] = []
        self._groups: dict[str, SettingGroup] = {}

    # ── 查询 ──────────────────────────────────────
    @property
    def roots(self) -> tuple[SettingGroup, ...]:
        return tuple(self._roots)

    @property
    def group_count(self) -> int:
        return len(self._groups)

    @property
    def item_count(self) -> int:
        return sum(1 for _ in self.iter_items())

    def get(self, group_id: str) -> Optional[SettingGroup]:
        return self._groups.get(group_id)

    def __contains__(self, group_id: str) -> bool:
        return group_id in self._groups

    def groups(self) -> tuple[SettingGroup, ...]:
        """按树的前序遍历返回所有分组。"""
        out: list[SettingGroup] = []
        for root in self._roots:
            out.extend(root.iter_groups())
        return tuple(out)

    def iter_items(
            self,
    ) -> Iterator[tuple[SettingGroup, SettingItem, Optional[Path]]]:
        for root in self._roots:
            yield from root.walk_items()

    # ── 登记 ──────────────────────────────────────
    def register(
            self,
            group: SettingGroup,
            *,
            parent: Union[SettingGroup, str, None] = None,
            replace: bool = False,
    ) -> SettingGroup:
        """登记一个分组（连同全部子分组），可挂到已有分组下。"""
        if not isinstance(group, SettingGroup):
            raise TypeError(
                f"register expected SettingGroup, got {type(group).__name__}"
            )

        parent_group = _coerce_group(parent, self) if parent is not None else None
        base = (
            f"{parent_group.qualname}.{group.id}"
            if parent_group is not None else group.id
        )

        # 幂等：同一个对象重复登记不重复挂到父/根列表
        if self._groups.get(base) is group:
            return group

        # 先在真正挂载前检查整棵子树的键冲突，失败时不留半截状态
        pending: list[tuple[SettingGroup, str]] = [(group, base)]
        seen: set[str] = set()
        while pending:
            node, key = pending.pop()
            if key in seen:
                raise ValueError(
                    f"setting group {key!r} appears twice in the same subtree"
                )
            seen.add(key)
            existing = self._groups.get(key)
            if existing is not None and existing is not node:
                if not replace:
                    raise ValueError(
                        f"setting group {key!r} is already registered"
                    )
                self._detach(key)
            pending.extend(
                (child, f"{key}.{child.id}") for child in node.groups
            )

        if parent_group is not None:
            parent_group.add(group)
        else:
            if group._parent is not None:
                raise ValueError(
                    f"setting group {group.qualname!r} is attached to "
                    f"{group._parent.qualname!r}; pass parent=... "
                    f"or detach it first"
                )
            self._roots.append(group)

        for node_group in group.iter_groups():
            self._groups[node_group.qualname] = node_group
        return group

    def register_item(
            self,
            item: SettingItem,
            group: Union[SettingGroup, str],
            *,
            replace: bool = False,
    ) -> SettingItem:
        """把叶子挂到一个已登记的分组下。"""
        if not isinstance(item, SettingItem):
            raise TypeError(
                f"register_item expected SettingItem, got {type(item).__name__}"
            )
        parent_group = _coerce_group(group, self)
        existing = next(
            (
                child for child in parent_group.items
                if child.name == item.name
            ),
            None,
        )
        if existing is not None:
            if existing is item:
                return item
            if not replace:
                raise ValueError(
                    f"setting group {parent_group.qualname!r}: duplicate item "
                    f"name {item.name!r}"
                )
            # 只从直接子项里移除；递归同名但不同层不算冲突
            parent_group._children = [
                child for child in parent_group._children
                if child is not existing
            ]
        parent_group.add(item)
        return item

    def unregister(self, group_id: str) -> Optional[SettingGroup]:
        """移除一个分组（连同其子树）；未登记时返回 None。"""
        return self._detach(group_id)

    def _detach(self, group_id: str) -> Optional[SettingGroup]:
        group = self._groups.get(group_id)
        if group is None:
            return None
        for node in group.iter_groups():
            self._groups.pop(node.qualname, None)
        if group._parent is not None:
            group._parent._children = [
                child for child in group._parent._children
                if child is not group
            ]
            group._parent = None
        else:
            self._roots = [root for root in self._roots if root is not group]
        return group

    def clear(self) -> None:
        """测试 / 重载用：清空整张表。"""
        for group in self._groups.values():
            group._parent = None
        self._groups.clear()
        self._roots.clear()


#: 进程级设置页结构登记表。
SETTING_SCHEMA = SettingSchemaRegistry()


# ══════════════════════════════════════════════════
# 快捷登记 API
# ══════════════════════════════════════════════════
def setting_item(
        name: str,
        section: Any,
        key: Optional[str] = None,
        **kwargs: Any,
) -> SettingItem:
    """快速构造一个叶子，支持三种 path 写法::

        setting_item("theme", "ui", "theme")
        setting_item("theme", ("ui", "theme"))
        setting_item("language", PreferenceConfig.language)

    第一种是 ``section + key``；第二种是路径元组；第三种直接复用现有
    ``ConfigField``（path / default 自动继承）。
    """
    if key is not None:
        if not isinstance(section, str):
            raise TypeError(
                f"setting_item({name!r}): section must be str, "
                f"got {type(section).__name__}"
            )
        return SettingItem(name=name, section=section, key=key, **kwargs)

    if hasattr(section, "path"):
        return SettingItem.from_field(name, section, **kwargs)
    if isinstance(section, (tuple, list)):
        path = tuple(section)
        if len(path) == 2:
            return SettingItem(
                name=name, section=str(path[0]), key=str(path[1]), **kwargs,
            )
        if len(path) == 1:
            return SettingItem(
                name=name, section="__default__", key=str(path[0]), **kwargs,
            )
        raise ValueError(
            f"setting_item({name!r}): path must have 1 or 2 parts, "
            f"got {list(path)!r}"
        )
    raise TypeError(
        f"setting_item({name!r}): expected (section, key), a 1/2-part path, "
        f"or a ConfigField-like object, got {type(section).__name__}"
    )


def register_setting_group(
        group: SettingGroup,
        *,
        parent: Union[SettingGroup, str, None] = None,
        replace: bool = False,
        registry: Optional[SettingSchemaRegistry] = None,
) -> SettingGroup:
    """把分组登记到全局表（或显式指定的 registry）。"""
    return (registry or SETTING_SCHEMA).register(
        group, parent=parent, replace=replace,
    )


def register_setting_item(
        item: SettingItem,
        group: Union[SettingGroup, str],
        *,
        replace: bool = False,
        registry: Optional[SettingSchemaRegistry] = None,
) -> SettingItem:
    """把叶子登记到指定分组。"""
    return (registry or SETTING_SCHEMA).register_item(
        item, group, replace=replace,
    )


def get_setting_group(
        group_id: str,
        *,
        registry: Optional[SettingSchemaRegistry] = None,
) -> Optional[SettingGroup]:
    return (registry or SETTING_SCHEMA).get(group_id)


def setting_group(
        group_id: str,
        *,
        title: Optional[str] = None,
        description: str = "",
        parent: Union[SettingGroup, str, None] = None,
        file: Optional[Path] = None,
        order: int = 0,
        replace: bool = False,
        registry: Optional[SettingSchemaRegistry] = None,
):
    """类装饰器：把类属性里的 ``SettingItem`` / ``SettingGroup`` 收成分组。

    用法::

        @setting_group("general", title="常规")
        class General:
            language = setting_item("language", "general", "language")

    装饰后的类会带上 ``__setting_group__``，便于测试 / 调试时取回。
    """

    def decorator(cls: type) -> type:
        group = SettingGroup(
            group_id,
            title=title,
            description=description,
            file=file,
            order=order,
        )
        for value in vars(cls).values():
            if isinstance(value, (SettingItem, SettingGroup)):
                group.add(value)
        register_setting_group(
            group, parent=parent, replace=replace, registry=registry,
        )
        setattr(cls, "__setting_group__", group)
        return cls

    return decorator


# ══════════════════════════════════════════════════
# 取值 / 归属文件解析
# ══════════════════════════════════════════════════
def _current_config(config: Any = None) -> Any:
    if config is not None:
        return config
    from .declaration import get_config

    return get_config()


def _item_node(item: SettingItem) -> tuple[str, str]:
    return item.node


def _explicit_file(
        item: SettingItem,
        inherited_file: Optional[Path],
) -> Optional[Path]:
    if item.file is not None:
        return item.file
    return inherited_file


def _target_file(
        item: SettingItem,
        inherited_file: Optional[Path],
        config: Any,
) -> Optional[Path]:
    explicit = _explicit_file(item, inherited_file)
    if explicit is not None:
        return explicit
    try:
        return config.target_file_of(item.section, item.key)
    except Exception:  # noqa: BLE001  dump 不应因为旧配置对象报错
        return None


def _resolve_item(
        group: SettingGroup,
        item: SettingItem,
        inherited_file: Optional[Path],
        config: Any,
) -> dict[str, Any]:
    resolver = config.resolver
    node = _item_node(item)
    present = node in resolver

    default = None if item.default is UNSET else item.default
    value: Any = default
    read_error: Optional[str] = None
    converter = None
    origin: Optional[Path] = None
    dirty = False

    if present:
        try:
            value = resolver.get_typed(*node)
        except Exception as exc:  # noqa: BLE001  读失败只记录，不炸 dump
            read_error = str(exc)
            value = default
        converter = resolver.converter_of(*node)
        origin = resolver.origin_of(*node)
        dirty = node in resolver.dirty_nodes()

    value_type = converter or item.value_type
    if value_type is None and value is not None and value is not default:
        value_type = type(value)

    choices = item.choices or _literal_choices(value_type)
    widget = _normalize_widget(item.widget, value_type, choices)
    target = _target_file(item, inherited_file, config)
    explicit_file = _explicit_file(item, inherited_file)

    readonly = False
    if origin is not None:
        try:
            readonly = bool(config.is_readonly_source(origin))
        except Exception:  # noqa: BLE001  旧配置对象可能没有这个方法
            readonly = False

    constraints: dict[str, Any] = {}
    if item.minimum is not None:
        constraints["minimum"] = item.minimum
    if item.maximum is not None:
        constraints["maximum"] = item.maximum
    if item.step is not None:
        constraints["step"] = item.step

    return {
        "key": f"{group.qualname}.{item.name}",
        "name": item.name,
        "group": group.qualname,
        "path": item.path_text,
        "section": item.section,
        "config_key": item.key,
        "label": item.display_label,
        "description": item.description,
        "widget": widget.value,
        "value": _jsonable(value),
        "value_repr": _value_repr(value),
        "default": _jsonable(default),
        "type": type_name(value_type),
        "choices": [_jsonable(choice) for choice in choices],
        "choice_labels": dict(item.choice_labels or {}),
        "constraints": constraints or None,
        # 同层排序权重：前端（设置页）需要把叶子和子分组按 order 混排
        "order": item.order,
        "editable": bool(item.editable and group.editable),
        "present": present,
        "dirty": dirty,
        "readonly": readonly,
        "origin_file": str(origin) if origin is not None else None,
        "target_file": str(target) if target is not None else None,
        "explicit_file": (
            str(explicit_file) if explicit_file is not None else None
        ),
        "read_error": read_error,
    }


# ══════════════════════════════════════════════════
# dump
# ══════════════════════════════════════════════════
def _sorted_groups(group: SettingGroup) -> list[SettingGroup]:
    return sorted(group.groups, key=lambda g: (g.order, g.id))


def _sorted_items(group: SettingGroup) -> list[SettingItem]:
    return sorted(group.items, key=lambda i: (i.order, i.name))


def _dump_group(
        group: SettingGroup,
        inherited_file: Optional[Path],
        config: Any,
) -> dict[str, Any]:
    group_file = group.file if group.file is not None else inherited_file
    items = [
        _resolve_item(group, item, group_file, config)
        for item in _sorted_items(group)
    ]
    groups = [
        _dump_group(child, group_file, config)
        for child in _sorted_groups(group)
    ]
    return {
        "id": group.id,
        "key": group.qualname,
        "title": group.title,
        "description": group.description,
        "order": group.order,
        "editable": group.editable,
        "file": str(group_file) if group_file is not None else None,
        "items": items,
        "groups": groups,
        "item_count": sum(
            [len(items)] + [child["item_count"] for child in groups]
        ),
    }


def _flatten_group_nodes(
        node: dict[str, Any],
) -> Iterator[dict[str, Any]]:
    yield from node["items"]
    for child in node["groups"]:
        yield from _flatten_group_nodes(child)


def dump_setting_schema(
        config: Any = None,
        *,
        registry: Optional[SettingSchemaRegistry] = None,
) -> dict[str, Any]:
    """导出已登记的分组结构（嵌套 dict，可直接 JSON 序列化）。

    Returns:
        ``{"groups": [...], "items": [...], "by_file": {...}, "unbound": [...],
        "summary": {...}}``：

        - ``groups``: 按树组织的分组（含 item_count / file / items / groups）
        - ``items``: 扁平叶子列表，顺序与树的前序遍历一致
        - ``by_file``: ``{归属文件: [叶子 key, ...]}``（按首次出现顺序）
        - ``unbound``: 没有归属文件的叶子 key（既无显式 file，也无默认落点）
    """
    schema = registry or SETTING_SCHEMA
    config = _current_config(config)

    groups = [
        _dump_group(root, None, config) for root in schema.roots
    ]
    items: list[dict[str, Any]] = []
    for group in groups:
        items.extend(_flatten_group_nodes(group))

    by_file: dict[str, list[str]] = {}
    unbound: list[str] = []
    for item in items:
        target = item["target_file"]
        if target is None:
            unbound.append(item["key"])
        else:
            by_file.setdefault(target, []).append(item["key"])

    result = {
        "groups": groups,
        "items": items,
        "by_file": by_file,
        "unbound": unbound,
        "summary": {
            "group_count": len(schema.groups()),
            "item_count": len(items),
            "file_count": len(by_file),
        },
    }
    _logger.debug(
        "setting schema dump: groups=%d items=%d files=%d unbound=%d",
        result["summary"]["group_count"],
        result["summary"]["item_count"],
        result["summary"]["file_count"],
        len(unbound),
    )
    return result


def dump_setting_schema_by_file(
        config: Any = None,
        *,
        registry: Optional[SettingSchemaRegistry] = None,
) -> dict[str, Any]:
    """按归属文件输出叶子（每个文件一组，保留文件首次出现顺序）。

    Returns:
        ``{"files": [{"file": str, "items": [item dict, ...]}], "unbound": [...]}`。
    """
    dump = dump_setting_schema(config, registry=registry)
    by_file: dict[str, list[dict[str, Any]]] = {}
    for item in dump["items"]:
        target = item["target_file"]
        if target is not None:
            by_file.setdefault(target, []).append(item)

    return {
        "files": [
            {"file": file, "items": items}
            for file, items in by_file.items()
        ],
        "unbound": [
            item for item in dump["items"]
            if item["target_file"] is None
        ],
    }


def _item_flags(item: dict[str, Any]) -> str:
    flags: list[str] = []
    if not item["editable"]:
        flags.append("fixed")
    if item["readonly"]:
        flags.append("readonly")
    if item["dirty"]:
        flags.append("dirty")
    if not item["present"]:
        flags.append("missing")
    if item["read_error"]:
        flags.append("read-error")
    return " ".join(f"[{flag}]" for flag in flags)


def _render_item_lines(
        item: dict[str, Any],
        prefix: str,
        is_last: bool,
) -> list[str]:
    branch = "└─ " if is_last else "├─ "
    detail_prefix = prefix + ("   " if is_last else "│  ")
    flags = _item_flags(item)
    head = (
        f"{prefix}{branch}{item['label']} = {item['value_repr']}  "
        f"({item['path']}, {item['type']})"
    )
    if flags:
        head += f"  {flags}"

    details = [f"widget={item['widget']}", f"default={item['default']!r}"]
    if item["choices"]:
        details.append(f"choices={item['choices']!r}")
    if item["constraints"]:
        details.append(f"constraints={item['constraints']!r}")
    if item["explicit_file"]:
        details.append(f"file={item['explicit_file']}")
    elif item["target_file"]:
        details.append(f"file={item['target_file']}")
    else:
        details.append("file=<unbound>")
    if item["origin_file"] and item["origin_file"] != item["target_file"]:
        details.append(f"origin={item['origin_file']}")
    if item["read_error"]:
        details.append(f"error={item['read_error']}")

    return [head] + [f"{detail_prefix}   {line}" for line in details]


def _render_group_lines(
        node: dict[str, Any],
        prefix: str,
        is_last: bool,
) -> list[str]:
    branch = "└─ " if is_last else "├─ "
    title = node["title"] or node["key"]
    head = f"{prefix}{branch}{title} [{node['key']}] ({node['item_count']} items)"
    if node["description"]:
        head += f" — {node['description']}"
    if node["file"]:
        head += f"  file={node['file']}"

    child_prefix = prefix + ("   " if is_last else "│  ")
    entries: list[tuple[str, Any]] = [
        ("group", child) for child in node["groups"]
    ] + [("item", item) for item in node["items"]]

    lines = [head]
    for index, (kind, entry) in enumerate(entries):
        entry_last = index == len(entries) - 1
        if kind == "group":
            lines.extend(_render_group_lines(entry, child_prefix, entry_last))
        else:
            lines.extend(_render_item_lines(entry, child_prefix, entry_last))
    return lines


def format_setting_schema_dump(
        config: Any = None,
        *,
        registry: Optional[SettingSchemaRegistry] = None,
        show_files: bool = True,
) -> str:
    """把已登记的分组 dump 成便于阅读 / 打日志的文本。"""
    dump = dump_setting_schema(config, registry=registry)
    summary = dump["summary"]
    lines = [
        (
            f"Settings schema: {summary['group_count']} groups, "
            f"{summary['item_count']} items, "
            f"{summary['file_count']} files"
        )
    ]

    if not dump["groups"]:
        lines.append("(no setting groups registered)")
    else:
        lines.append("")
        for index, group in enumerate(dump["groups"]):
            lines.extend(
                _render_group_lines(
                    group, "", index == len(dump["groups"]) - 1,
                )
            )

    if show_files:
        lines.append("")
        lines.append("Files:")
        if not dump["by_file"]:
            lines.append("  (none)")
        for file, keys in dump["by_file"].items():
            lines.append(f"  {file}")
            lines.extend(f"    {key}" for key in keys)
        if dump["unbound"]:
            lines.append("  <unbound>")
            lines.extend(f"    {key}" for key in dump["unbound"])

    return "\n".join(lines)


# ══════════════════════════════════════════════════
# 回写（按归属文件分组）
# ══════════════════════════════════════════════════
@dataclass
class SettingWriteResult:
    """一次回写的结果：实际写入的文件 + 各文件归属的计划节点。

    ``dry_run=True`` 时 ``written`` / ``flushed`` / ``applied`` 都是空的，
    真正的信息在 ``changes`` 里（每个叶子的旧值 / 新值 / 将写入的 INI 文本）。
    """

    written: tuple[Path, ...]
    by_file: dict[Path, tuple[str, ...]]
    flushed: tuple[str, ...]
    applied: tuple[str, ...]
    changes: tuple[dict[str, Any], ...] = ()
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "written": [str(path) for path in self.written],
            "by_file": {
                str(path): list(keys) for path, keys in self.by_file.items()
            },
            "flushed": list(self.flushed),
            "applied": list(self.applied),
            "changes": [dict(change) for change in self.changes],
            "dry_run": self.dry_run,
        }


_UNAVAILABLE = object()


def _lookup_value(
        values: Mapping[Any, Any],
        group: SettingGroup,
        item: SettingItem,
        unique_names: Mapping[str, SettingItem],
) -> Any:
    candidates: list[Any] = [
        f"{group.qualname}.{item.name}",
        (item.section, item.key),
        item.path_text,
    ]
    # 叶子名只在全局唯一时才当作键，避免不同分组同名被误用
    if unique_names.get(item.name) is item:
        candidates.append(item.name)

    for candidate in candidates:
        try:
            if candidate in values:
                return values[candidate]
        except TypeError:  # pragma: no cover - 不可哈希的键
            continue
    return _UNAVAILABLE


def _current_value(config: Any, item: SettingItem) -> Any:
    """叶子当前的值（读不到时退回声明默认值），只用于 dry-run 展示。"""
    try:
        if item.node in config.resolver:
            return config.resolver.get_typed(*item.node)
    except Exception:  # noqa: BLE001  预览不该因为读失败而中断
        pass
    return None if item.default is UNSET else item.default


def _describe_change(
        config: Any,
        group: SettingGroup,
        item: SettingItem,
        target: Path,
        coerced: Any,
        old: Any,
) -> dict[str, Any]:
    """dry-run 的一条预览：旧值 / 新值 / 将写入的 INI 文本 / 归属文件。"""
    from .utils.ini_parser import _deconvert

    text: Optional[str]
    try:
        text = _deconvert(
            coerced, config.resolver.converter_of(item.section, item.key),
        )
    except Exception:  # noqa: BLE001  反解失败只影响预览文本
        text = None
    return {
        "key": f"{group.qualname}.{item.name}",
        "path": item.path_text,
        "section": item.section,
        "config_key": item.key,
        "old": _jsonable(old),
        "new": _jsonable(coerced),
        "text": text,
        "file": str(target),
        "changed": old != coerced,
    }


def _save_entries(
        entries: Sequence[tuple[SettingGroup, SettingItem, Optional[Path]]],
        values: Optional[Mapping[Any, Any]],
        config: Any,
        strict: bool,
        dry_run: bool = False,
) -> SettingWriteResult:
    """见 ``save_setting_group``；``dry_run`` 时只预览不写。"""
    by_file: dict[Path, list[str]] = {}

    # 名称唯一性用于 values 简写键（按本次保存范围判定）
    name_counts: dict[str, int] = {}
    for _, item, _ in entries:
        name_counts[item.name] = name_counts.get(item.name, 0) + 1
    unique_names: dict[str, SettingItem] = {}
    for _, item, _ in entries:
        if name_counts[item.name] == 1:
            unique_names[item.name] = item

    applied: list[str] = []
    pending: list[tuple[SettingGroup, SettingItem, Path, Any, Any]] = []

    # 第一遍：只做校验 / 转换，不写内存；任一叶子非法时不会留下半截修改
    for group, item, inherited_file in entries:
        target = _target_file(item, inherited_file, config)
        if target is not None:
            by_file.setdefault(target, []).append(
                f"{group.qualname}.{item.name}"
            )

        if values is None:
            continue
        new_value = _lookup_value(values, group, item, unique_names)
        if new_value is _UNAVAILABLE:
            continue
        if target is None:
            raise ValueError(
                f"setting item {group.qualname}.{item.name}: no owning file; "
                f"set Item.file / Group.file or configure a default write file"
            )
        pending.append((
            group, item, target,
            item.coerce(new_value), _current_value(config, item),
        ))

    by_file_result = {path: tuple(keys) for path, keys in by_file.items()}

    if dry_run:
        return SettingWriteResult(
            written=(),
            by_file=by_file_result,
            flushed=(),
            applied=(),
            changes=tuple(
                _describe_change(config, group, item, target, coerced, old)
                for group, item, target, coerced, old in pending
            ),
            dry_run=True,
        )

    # 第二遍：真正写内存并标脏
    for group, item, target, coerced, _old in pending:
        config.set(
            item.section, item.key, coerced,
            target=target, save="defer",
        )
        applied.append(f"{group.qualname}.{item.name}")

    dirty = config.resolver.dirty_nodes()
    dirty_nodes: list[tuple[str, str]] = []
    seen_nodes: set[tuple[str, str]] = set()
    for _, item, _ in entries:
        if item.node in dirty and item.node not in seen_nodes:
            seen_nodes.add(item.node)
            dirty_nodes.append(item.node)
    written = config.flush(only=dirty_nodes, strict=strict) if dirty_nodes else []

    flushed = [
        f"{group.qualname}.{item.name}"
        for group, item, _ in entries
        if item.node in dirty
    ]
    return SettingWriteResult(
        written=tuple(written),
        by_file=by_file_result,
        flushed=tuple(flushed),
        applied=tuple(applied),
    )


def _scope_entries(
        group: SettingGroup,
) -> list[tuple[SettingGroup, SettingItem, Optional[Path]]]:
    return list(group.walk_items())


def save_setting_group(
        group: Union[SettingGroup, str],
        values: Optional[Mapping[Any, Any]] = None,
        *,
        config: Any = None,
        strict: bool = False,
        dry_run: bool = False,
        registry: Optional[SettingSchemaRegistry] = None,
) -> SettingWriteResult:
    """把分组内的编辑值写回各自归属文件。

    Args:
        group: ``SettingGroup`` 或已登记的分组 id。
        values: 可选的编辑值映射。键支持：
            ``"group.item"`` / ``(section, key)`` / ``"[section]key"``；
            叶子名在本次保存范围内唯一时也可直接用叶子名。
        config: 显式 ``Configue``；默认取当前应用配置。
        strict: 写盘失败时是否立即抛 ``ConfigError``（同 ``flush``）。
        dry_run: True 时只做校验 / 转换 / 预览：返回的 ``changes`` 里是每个
            叶子的旧值、新值、将写入的 INI 文本和归属文件，
            ``written`` / ``flushed`` / ``applied`` 保持为空，内存与磁盘都不动。
        registry: 显式登记表；默认全局。

    Returns:
        ``SettingWriteResult``：``written`` 是实际写入文件，
        ``by_file`` 是「归属文件 → 叶子 key」的计划分组，
        ``flushed`` 是本次真正落盘的叶子，``applied`` 是本次赋值的叶子。

    只有本次赋值（或其它途径）标脏的节点会被写入；没有编辑的分组不会
    产生任何写盘。每个节点用 ``item.file`` / ``group.file`` /
    ``Configue.target_file_of`` 推出的归属文件作为 ``target`` 传给
    ``Configue.set``，因此 ``flush`` 的分组与这里展示的完全一致。
    """
    schema = registry or SETTING_SCHEMA
    if isinstance(group, str):
        resolved = schema.get(group)
        if resolved is None:
            raise KeyError(f"setting group {group!r} is not registered")
        group = resolved
    elif not isinstance(group, SettingGroup):
        raise TypeError(
            f"save_setting_group expected SettingGroup or id, "
            f"got {type(group).__name__}"
        )

    return _save_entries(
        _scope_entries(group), values, _current_config(config), strict,
        dry_run=dry_run,
    )


def save_setting_schema(
        values: Optional[Mapping[Any, Any]] = None,
        *,
        config: Any = None,
        strict: bool = False,
        dry_run: bool = False,
        registry: Optional[SettingSchemaRegistry] = None,
) -> SettingWriteResult:
    """把整张登记表里的编辑值写回（同样按归属文件分组）。

    ``dry_run=True`` 的语义与 :func:`save_setting_group` 一致：只预览。
    """
    schema = registry or SETTING_SCHEMA
    entries = list(schema.iter_items())
    return _save_entries(
        entries, values, _current_config(config), strict,
        dry_run=dry_run,
    )


# 兼容别名：dump / 保存的「整表」语义
dump_registered_groups = dump_setting_schema
format_registered_groups = format_setting_schema_dump
