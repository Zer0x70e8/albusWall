#
"""设置结构层：把 ``@configclass`` 声明变成「可枚举 / 可校验 / 可写回」的设置树。

这一层解决的问题
================

``registry`` 里已经有一份完整的**声明**（哪些 section、哪些 key、什么类型、
什么默认值、哪个文件），``resolver`` + ``configue`` 里有一份完整的**读写**
（取值、类型转换、归属文件推导、原子写回）。缺的是中间那一层：

* 上层（CLI / 设置页 / 文档生成器）要的是一棵**带分组、带顺序、带展示文案**
  的树，而不是 ``_CLASSES`` 这张表；
* 改配置要「**先全量校验、后统一写**」，不能写一半炸一半，也不能写进
  「没人声明过的 key」；
* dry-run 要能看到「将会写进哪个文件的哪一行」。

所以本模块做三件事，全部**无 Qt / 无仓储 / 不 import service**：

1. **自动建树**：:func:`build_schema` 遍历 :func:`registry.declarations`，
   每个 ``section`` 一个分组、每个字段一个叶子；
2. **查询**：:func:`dump_schema` 的四种结构化视图（tree / basic / by_file /
   flat），只输出**声明事实 + 值 + 状态**——展示策略（控件类型、控件约束、
   选项文案、``advanced`` 分级、树形文本排版）由消费者自己决定
   （CLI 在 ``service/command/handlers/_present.py``）；
3. **写回**：:func:`save_values` / :func:`save_group` 两遍算法——
   第一遍只读校验（任何一个值非法就整体失败，内存与磁盘都不动），
   第二遍才 ``resolver.set`` + ``cfg.flush(only=...)``。

三条与 ``configue`` 其余部分共享的约定
=====================================

* **配置节点 = ``(section, key)``**，``section`` 取 ``FieldInfo.path[0]``、
  ``key`` 取 ``FieldInfo.path[-1]``——这与 ``_FIELD_OWNER`` 和
  ``_ConfigSlot.__get__`` 完全一致，因此本层生成的节点与运行期读写的是
  同一批格子，不会出现「dump 里有、业务读不到」的错位；
* **写回目标一律由 ``Configue.target_file_of`` 推导**，与 ``flush`` 同源；
  「归属文件」在本层只用于**展示**（``target_file`` / ``by_file`` / ``unbound``）；
* **类型转换一律走 ``resolver.coerce_value``**（即读配置文件那套
  ``_convert``），保证「写进去的值」与「读出来的值」对称。

降级约定：声明所在的文件没 ``reg()`` / 没 ``load()``、值读失败、节点无归属
文件——这三种情况 dump 都**不抛异常**，叶子 ``target_file=None`` 进
``unbound``，只有 ``save_*`` 才报错。``Configue`` 尚未构造时 dump 同样可用
（此时所有值都退回声明默认值）。
"""

from __future__ import annotations

import logging
import types
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Optional, Union, get_args, get_origin

from .backends import get_backend
from .errors import ConfigError
from .registry import (
    ClassInfo,
    FieldInfo,
    declarations,
    registered_files,
)
from .resolver import coerce_value

logger = logging.getLogger(__name__)

__all__ = [
    # 数据结构
    "UNSET",
    "SettingItem",
    "SettingGroup",
    "SettingSchemaRegistry",
    "SettingWriteResult",
    # 自动建树 + 缓存
    "build_schema",
    "get_schema",
    "invalidate_schema",
    # 手写覆盖入口
    "setting_group",
    "setting_item",
    "register_setting_group",
    "register_setting_item",
    # 查询
    "dump_schema",
    "dump_schema_dict",
    "dump_schema_by_file",
    "jsonable",
    "type_name",
    # 写回
    "save_values",
    "save_group",
]

_UNION_ORIGINS = (Union, getattr(types, "UnionType", Union))


# ══════════════════════════════════════════════════════════════
# 哨兵 / 枚举
# ══════════════════════════════════════════════════════════════

class _Unset:
    """哨兵：区分「没给默认值」与「默认值就是 ``None``」。"""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - 只用于调试
        return "<unset>"

    def __bool__(self) -> bool:
        return False


#: ``SettingItem.default`` 的哨兵值：表示「继承声明里的默认值」。
UNSET = _Unset()


# ══════════════════════════════════════════════════════════════
# 类型推导（只推「选项」这类事实；值类别是展示策略，在 CLI 侧）
# ══════════════════════════════════════════════════════════════

def _unwrap_optional(value_type: Any) -> Any:
    """展开 ``Optional`` / ``Union``，返回第一个非 ``None`` 分支。"""
    if get_origin(value_type) not in _UNION_ORIGINS:
        return value_type
    for arg in get_args(value_type):
        if arg is not type(None):
            return arg
    return value_type


def _literal_choices(value_type: Any) -> tuple[Any, ...]:
    """``Literal["a", "b"]`` → ``("a", "b")``；其余返回空元组。"""
    if get_origin(value_type) is Literal:
        return tuple(get_args(value_type))
    return ()


def _is_enum_subclass(value_type: Any) -> bool:
    """``value_type`` 是否是一个 ``Enum`` 子类（``Literal`` 之外的另一类枚举）。"""
    return isinstance(value_type, type) and issubclass(value_type, Enum)


def _choices_for(value_type: Any) -> tuple[Any, ...]:
    """按声明类型推导选项：``Literal`` 取参数，``Enum`` 取成员值。"""
    inner = _unwrap_optional(value_type)
    literal = _literal_choices(inner)
    if literal:
        return literal
    if _is_enum_subclass(inner):
        return tuple(member.value for member in inner)
    return ()


def type_name(value_type: Any) -> str:
    """把类型 / 注解显示成人类可读的名字（dump 的 ``type`` 字段）。"""
    if value_type is None or value_type is UNSET:
        return "?"
    if value_type is Any:
        return "Any"
    if value_type is type(None):
        return "None"

    origin = get_origin(value_type)
    if origin is None:
        name = getattr(value_type, "__name__", None)
        if isinstance(name, str):
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


# ══════════════════════════════════════════════════════════════
# 值归一 / 展示
# ══════════════════════════════════════════════════════════════

def jsonable(value: Any) -> Any:
    """把配置值收敛成 JSON 友好的结构（``Path`` / ``Enum`` / 容器 / 元组）。

    设置 GUI 直接吃 dump 的结果，所以任何非 JSON 原生的东西都必须在这里
    归一；认不出来的对象退化成 ``repr``，**绝不抛异常**。
    """
    if value is UNSET or value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return jsonable(value.value)
    if isinstance(value, Mapping):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(item) for item in value]
    if isinstance(value, (str, int, float)):
        return value
    return repr(value)


def _value_repr(value: Any, limit: int = 48) -> str:
    """一行可读的值摘要（dump 的 ``value_repr`` 字段）。"""
    if value is UNSET:
        text = "<unset>"
    elif isinstance(value, (str, Path)):
        text = repr(str(value))
    else:
        text = repr(jsonable(value))
    if len(text) > limit:
        text = text[: max(limit - 1, 0)] + "…"
    return text


def _is_advanced_id(group_id: str) -> bool:
    """``advanced`` 组的约定 id（``advanced`` / ``advanced.xxx``）。"""
    return group_id == "advanced" or group_id.startswith("advanced.")


# ══════════════════════════════════════════════════════════════
# 叶子
# ══════════════════════════════════════════════════════════════

def _resolve_text(value: Any) -> str:
    """把 ``label`` / ``description`` 解析成字符串（**dump 时**调用）。

    - ``None`` → ``""``
    - ``Callable[[], str]`` → 调它；抛异常 → ``""``（翻译缺失不能炸 dump）
    - 其它 → ``str(value)``

    刻意**不**在 ``SettingItem.__post_init__`` / ``_auto_item`` 里求值：那里是
    构造期，语言切了不会重跑，文案就会被冻在第一次 dump 的语言上。
    """
    if value is None:
        return ""
    if callable(value):
        try:
            return str(value())
        except Exception:  # noqa: BLE001 - 翻译失败不该让 dump 失败
            logger.warning("setting text callable failed", exc_info=True)
            return ""
    return str(value)


@dataclass
class SettingItem:
    """一个可编辑叶子，绑定一个配置节点 ``(section, key)``。

    ``label`` / ``description`` / ``choices`` 属于**声明层**（前两个可以是
    i18n callable，dump 时求值；``choices`` 由 ``Literal`` / ``Enum`` 反射
    而来）。``editable`` / ``file`` / ``default`` 影响行为（是否可改、写回
    哪个文件、默认值）。

    **展示策略不在这里**：控件类别、控件约束（min/max/step）、选项文案、
    高级分级都不属于声明事实。需要覆盖就登记到 CLI 侧的纯数据表——
    ``service/command/handlers/_present.register_present_override``。

    ``default`` 为 :data:`UNSET` 表示「继承声明里的默认值」——自动建树时
    已经填好，手写覆盖时才需要它。
    """

    name: str
    section: str
    key: str
    #: 展示名：``str`` 或 ``Callable[[], str]``（**dump 时**求值，见
    #: :func:`_resolve_text`）。切语言后重新 ``dump_schema`` 即得新文案。
    label: str | Callable[[], str] = ""
    #: 说明文案，同上。
    description: str | Callable[[], str] = ""
    #: 类型反射出的事实：``Literal`` 参数 / ``Enum`` 成员值（dump 的
    #: ``choices`` 字段**只**来自这里与 ``value_type``，不再读手写覆盖）。
    choices: tuple[Any, ...] = ()
    editable: bool = True
    order: int = 0
    default: Any = UNSET
    value_type: Any = None
    file: Path | None = None

    def __post_init__(self) -> None:
        if not self.key:
            raise ValueError(
                f"setting item {self.name or '?'!r}: key is required"
            )
        if not self.name:
            self.name = self.key

        choices = self.choices
        if choices is None:  # type: ignore[comparison-overlap]
            choices = ()
        elif isinstance(choices, (str, bytes)):
            choices = (choices,)
        else:
            choices = tuple(choices)
        self.choices = choices

        if self.file is not None:
            self.file = Path(self.file)

        if self.value_type is None and self.default is not UNSET \
                and self.default is not None:
            self.value_type = type(self.default)

    # ── 派生视图 ──────────────────────────────────────────────
    @property
    def node(self) -> tuple[str, str]:
        """对应的配置节点 ``(section, key)``。"""
        return self.section, self.key

    @property
    def path_text(self) -> str:
        """``[section]key`` 形式的展示路径。"""
        return f"[{self.section}]{self.key}" if self.section else self.key

    @property
    def display_label(self) -> str:
        """展示名：``label`` 求值后为空时回落 ``name``。

        **不在这里**做 i18n 求值的唯一入口：``dump`` 走 :func:`_resolve_text`，
        本属性只是「不打印 callable 对象」的兜底。
        """
        return _resolve_text(self.label) or self.name

    def coerce(
            self,
            value: Any,
            *,
            custom_converters: Mapping[Any, Any] | None = None,
    ) -> Any:
        """把外部输入按 ``value_type`` 转成配置值。

        字符串 / 容器一律走 :func:`resolver.coerce_value`（即读配置文件那套
        语义）；非字符串标量原样返回，只把 ``bool`` 的 ``int`` 写法归一
        （``1`` → ``True``），与运行期赋值一致。

        Args:
            value: 外部输入（CLI 字面量 / IPC 的 JSON / 已类型化的值）。
            custom_converters: 运行期注册的自定义转换器；不给则从当前
                ``Configue`` 单例取（取不到就只用内置语义）。

        Raises:
            ValueError: 字符串转不过去（消息里带原值与目标类型名）。
        """
        type_ = self.value_type
        if type_ is None or type_ is Any:
            return value
        converters = (
            custom_converters
            if custom_converters is not None else _custom_converters()
        )
        if isinstance(value, str):
            return coerce_value(
                value, type_, custom_converters=converters,
            )
        if type_ is bool and isinstance(value, int):
            return bool(value)
        if get_origin(type_) is not None:
            return coerce_value(
                value, type_, custom_converters=converters,
            )
        return value


# ══════════════════════════════════════════════════════════════
# 分组
# ══════════════════════════════════════════════════════════════

class SettingGroup:
    """设置分组，可包含任意深度的子分组与叶子（混排）。

    ``id`` 是**当前层**的名字；全局键由父链路拼成（见 :attr:`qualname`，
    如 ``advanced.http``）。``groups`` / ``items`` 两个视图都按
    ``(order, id/name)`` 排序，所以列表顺序就是渲染顺序。
    """

    def __init__(
            self,
            id: str,
            title: str = "",
            *,
            description: str = "",
            order: int = 0,
            editable: bool = True,
            file: Path | None = None,
    ) -> None:
        text = str(id or "").strip()
        if not text:
            raise ValueError("setting group id must be a non-empty string")
        self.id = text
        self.title = title or text
        self.description = description
        self.order = order
        self.editable = editable
        self.file = Path(file) if file is not None else None
        self._children: list[SettingGroup | SettingItem] = []
        self._parent: Optional[SettingGroup] = None

    # ── 组装 ─────────────────────────────────────────────────
    def add(self, *children: SettingGroup | SettingItem) -> SettingGroup:
        """追加子分组 / 叶子，返回自身（支持链式写法）。

        Raises:
            ValueError: 同层叶子重名、子分组已挂到别处、或形成环。
        """
        for node in children:
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

    # ── 视图 ─────────────────────────────────────────────────
    @property
    def children(self) -> tuple[SettingGroup | SettingItem, ...]:
        """全部直接子项（分组 / 叶子，登记顺序）。"""
        return tuple(self._children)

    @property
    def items(self) -> tuple[SettingItem, ...]:
        """直接子叶子，按 ``(order, name)`` 排序。"""
        return tuple(sorted(
            (child for child in self._children
             if isinstance(child, SettingItem)),
            key=lambda item: (item.order, item.name),
        ))

    @property
    def groups(self) -> tuple[SettingGroup, ...]:
        """直接子分组，按 ``(order, id)`` 排序。"""
        return tuple(sorted(
            (child for child in self._children
             if isinstance(child, SettingGroup)),
            key=lambda group: (group.order, group.id),
        ))

    @property
    def parent(self) -> Optional[SettingGroup]:
        """父分组；根分组为 ``None``。"""
        return self._parent

    @property
    def qualname(self) -> str:
        """点分全局 id，如 ``thumbnail`` / ``advanced.http``。"""
        parts: list[str] = []
        node: Optional[SettingGroup] = self
        while node is not None:
            parts.append(node.id)
            node = node._parent
        return ".".join(reversed(parts))

    @property
    def item_count(self) -> int:
        """子树里的叶子总数。"""
        return sum(1 for _group, _item in self.walk_items())

    def iter_groups(self) -> Iterator[SettingGroup]:
        """前序遍历自身与全部子分组。"""
        yield self
        for child in self.groups:
            yield from child.iter_groups()

    def walk_items(self) -> Iterator[tuple[SettingGroup, SettingItem]]:
        """递归产出 ``(所属分组, 叶子)``，顺序与树的前序遍历一致。"""
        for _group, item, _file in self._walk_entries(self.file):
            yield _group, item

    def _walk_entries(
            self,
            inherited_file: Path | None = None,
    ) -> Iterator[tuple[SettingGroup, SettingItem, Path | None]]:
        """递归产出 ``(所属分组, 叶子, 继承的归属文件)``。

        ``inherited_file`` 沿父链路向下继承：分组显式给了 ``file`` 就用它，
        否则沿用父分组的（根为空）。叶子的 ``file`` 仍然优先。
        """
        group_file = self.file if self.file is not None else inherited_file
        for child in self._children:
            if isinstance(child, SettingItem):
                yield self, child, group_file
            else:
                yield from child._walk_entries(group_file)

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

    def _replace(self, old: SettingGroup | SettingItem, new: Any) -> None:
        """把自己子项里的 ``old`` 换成 ``new``（覆盖登记用）。"""
        for index, child in enumerate(self._children):
            if child is old:
                self._children[index] = new
                return

    def _remove(self, child: SettingGroup | SettingItem) -> None:
        """摘掉一个直接子项（``unregister`` / ``replace`` 用）。"""
        for index, node in enumerate(self._children):
            if node is child:
                del self._children[index]
                if isinstance(child, SettingGroup):
                    child._parent = None
                return

    def __iter__(self) -> Iterator[SettingGroup | SettingItem]:
        return iter(self._children)

    def __len__(self) -> int:
        return len(self._children)

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(id={self.id!r}, "
            f"items={len(self.items)}, groups={len(self.groups)})"
        )


def _clone_group(group: SettingGroup) -> SettingGroup:
    """浅克隆一个分组（子树克隆、叶子共享）。

    只用于把「手写登记表」里的分组挂进自动建出来的树：克隆之后两边的
    ``_parent`` 互不影响，``_MANUAL_SCHEMA`` 不会被污染。
    """
    clone = SettingGroup(
        group.id,
        group.title,
        description=group.description,
        order=group.order,
        editable=group.editable,
        file=group.file,
    )
    for child in group.children:
        clone.add(_clone_group(child) if isinstance(child, SettingGroup) else child)
    return clone


# ══════════════════════════════════════════════════════════════
# 登记表
# ══════════════════════════════════════════════════════════════

class SettingSchemaRegistry:
    """设置结构登记表：一棵或多棵树 + ``qualname → 分组`` 索引。

    它同时被两种用途复用：

    * :func:`build_schema` 用它装**自动建出来的**树；
    * 模块级的 :data:`_MANUAL_SCHEMA` 用它装**手写覆盖**（第二期设置 GUI 的
      入口），最后挂到自动树上。
    """

    def __init__(self) -> None:
        self._roots: list[SettingGroup] = []
        self._groups: dict[str, SettingGroup] = {}

    # ── 查询 ─────────────────────────────────────────────────
    @property
    def roots(self) -> tuple[SettingGroup, ...]:
        """根分组，按 ``(order, id)`` 排序。"""
        return tuple(sorted(
            self._roots, key=lambda group: (group.order, group.id),
        ))

    def groups(self) -> tuple[SettingGroup, ...]:
        """按树的前序遍历返回所有分组（含子分组）。"""
        out: list[SettingGroup] = []
        for root in self.roots:
            out.extend(root.iter_groups())
        return tuple(out)

    def get(self, group_id: str) -> Optional[SettingGroup]:
        """按 ``qualname`` 取分组；未登记返回 ``None``。"""
        return self._groups.get(group_id)

    def __contains__(self, group_id: object) -> bool:
        return group_id in self._groups

    @property
    def group_count(self) -> int:
        """分组总数（含子分组）。"""
        return len(self._groups)

    @property
    def item_count(self) -> int:
        """叶子总数。"""
        return sum(1 for _ in self.iter_items())

    def iter_items(self) -> Iterator[tuple[SettingGroup, SettingItem]]:
        """前序遍历全部 ``(所属分组, 叶子)``。"""
        for root in self.roots:
            yield from root.walk_items()

    # ── 登记 ─────────────────────────────────────────────────
    def register(
            self,
            *groups: SettingGroup,
            replace: bool = False,
    ) -> tuple[SettingGroup, ...]:
        """把若干分组登记为根分组（连同其子树）。

        Raises:
            TypeError: 传进来的不是 ``SettingGroup``。
            ValueError: 同 id 已登记（``replace=False``）或该分组已挂在别处。
        """
        registered: list[SettingGroup] = []
        for group in groups:
            if not isinstance(group, SettingGroup):
                raise TypeError(
                    f"register expected SettingGroup, got {type(group).__name__}"
                )
            existing = self._groups.get(group.qualname)
            if existing is group:
                registered.append(group)
                continue
            if existing is not None:
                if not replace:
                    raise ValueError(
                        f"setting group {group.qualname!r} is already registered"
                    )
                self.unregister(group.qualname)
            if group.parent is not None:
                raise ValueError(
                    f"setting group {group.qualname!r} is attached to "
                    f"{group.parent.qualname!r}; detach it first"
                )
            self._roots.append(group)
            self._index(group)
            registered.append(group)
        return tuple(registered)

    def register_item(
            self,
            group_id: str,
            *items: SettingItem,
            replace: bool = False,
    ) -> tuple[SettingItem, ...]:
        """把若干叶子挂到已登记的 ``group_id`` 分组下。

        Raises:
            KeyError: 分组未登记。
            TypeError: 传进来的不是 ``SettingItem``。
            ValueError: 同层叶子重名（``replace=False``）。
        """
        group = self._groups.get(group_id)
        if group is None:
            raise KeyError(f"setting group {group_id!r} is not registered")

        registered: list[SettingItem] = []
        for item in items:
            if not isinstance(item, SettingItem):
                raise TypeError(
                    f"register_item expected SettingItem, "
                    f"got {type(item).__name__}"
                )
            existing = next(
                (child for child in group.items if child.name == item.name),
                None,
            )
            if existing is not None:
                if existing is item:
                    registered.append(item)
                    continue
                if not replace:
                    raise ValueError(
                        f"setting group {group.qualname!r}: duplicate item "
                        f"name {item.name!r}"
                    )
                group._remove(existing)
            group.add(item)
            registered.append(item)
        return tuple(registered)

    def unregister(self, group_id: str) -> Optional[SettingGroup]:
        """摘掉一个分组（连同子树）；未登记返回 ``None``。"""
        group = self._groups.get(group_id)
        if group is None:
            return None
        for node in group.iter_groups():
            self._groups.pop(node.qualname, None)
        if group.parent is not None:
            group.parent._remove(group)
        else:
            self._roots = [root for root in self._roots if root is not group]
        return group

    def clear(self) -> None:
        """清空整张表（测试 / 重载用）。"""
        for root in self._roots:
            root._parent = None
        self._roots.clear()
        self._groups.clear()

    def _index(self, group: SettingGroup) -> None:
        self._groups[group.qualname] = group
        for child in group.groups:
            self._index(child)

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(groups={self.group_count}, "
            f"items={self.item_count})"
        )


#: 进程级「手写覆盖」登记表（自动建树之后挂上去）。
_MANUAL_SCHEMA = SettingSchemaRegistry()

#: :func:`get_schema` 的进程内缓存。
_CACHED_SCHEMA: Optional[SettingSchemaRegistry] = None


# ══════════════════════════════════════════════════════════════
# 快捷登记 API（手写覆盖）
# ══════════════════════════════════════════════════════════════

def setting_item(
        name: str,
        section: Any = "",
        key: Optional[str] = None,
        **kwargs: Any,
) -> SettingItem:
    """快速构造一个叶子，支持四种写法::

        setting_item("batch_size", "thumbnail", "batch_size")
        setting_item("batch_size", ("thumbnail", "batch_size"))
        setting_item("batch_size")            # 只按名字覆盖自动叶子
        setting_item("batch_size", "batch_size", label="批量大小")

    只给名字时 ``section`` 留空，覆盖阶段按「同分组内同名的自动叶子」匹配
    （第二期设置 GUI 只需要改 ``label`` / ``widget`` 这类展示信息，不必重复
    写路径）。
    """
    if key is not None:
        if not isinstance(section, str):
            raise TypeError(
                f"setting_item({name!r}): section must be str, "
                f"got {type(section).__name__}"
            )
        return SettingItem(name=name, section=section, key=key, **kwargs)

    if isinstance(section, str):
        # 只给名字：section 未知，key 用 name 占位（覆盖时按名字匹配）
        return SettingItem(
            name=name, section=section, key=kwargs.pop("key", name) or name,
            **kwargs,
        )
    if isinstance(section, (tuple, list)):
        path = tuple(section)
        if len(path) != 2:
            raise ValueError(
                f"setting_item({name!r}): path must have exactly 2 parts, "
                f"got {list(path)!r}"
            )
        return SettingItem(
            name=name, section=str(path[0]), key=str(path[1]), **kwargs,
        )
    raise TypeError(
        f"setting_item({name!r}): expected (section, key), a 2-part path, "
        f"or a bare name, got {type(section).__name__}"
    )


def setting_group(
        group_id: str,
        *,
        title: str = "",
        description: str = "",
        order: int = 0,
        editable: bool = True,
        file: Path | None = None,
) -> SettingGroup:
    """构造一个手写分组（函数式入口，第二期设置 GUI 用）。

    与自动建树的分组同 id 时按 id 合并（标题 / 说明 / 顺序 / 归属文件按
    「非空即覆盖」处理），不会替换掉自动叶子。

    这里**只有声明层**：分组是否算「高级」是展示分级，由 CLI 侧按分组 id
    约定（``advanced`` / ``advanced.*``）判定。
    """
    return SettingGroup(
        group_id,
        title or group_id,
        description=description,
        order=order,
        editable=editable,
        file=file,
    )


def register_setting_group(
        *groups: SettingGroup,
        replace: bool = False,
) -> tuple[SettingGroup, ...]:
    """把手写分组登记到进程级覆盖表（``build_schema`` 时挂到自动树）。"""
    return _MANUAL_SCHEMA.register(*groups, replace=replace)


def register_setting_item(
        group_id: str,
        *items: SettingItem,
        replace: bool = False,
) -> tuple[SettingItem, ...]:
    """把手写叶子登记到进程级覆盖表的某个分组下。

    分组还不存在时**自动建一个占位分组**（``title`` 缺省等于 id，合并阶段
    不会覆盖自动分组的标题）：自动树是在 ``build_schema`` 里才建出来的，
    这里没有理由要求调用方先手工登记一个空分组。
    """
    if _MANUAL_SCHEMA.get(group_id) is None:
        _MANUAL_SCHEMA.register(setting_group(group_id))
    return _MANUAL_SCHEMA.register_item(
        group_id, *items, replace=replace,
    )


# ══════════════════════════════════════════════════════════════
# 当前配置实例（惰性 / 可缺省）
# ══════════════════════════════════════════════════════════════

def _current_config(config: Any = None) -> Any:
    """取 ``Configue`` 实例；显式传入优先，否则取单例（未构造则为 ``None``）。

    延迟 import：``configue.py`` 不依赖本模块，本模块却要用它，放模块顶层
    会把这条边变成环（``configue/__init__.py`` 先 import ``configue``，
    再 import 本模块）。
    """
    if config is not None:
        return config
    from .configue import Configue

    return Configue._instance


def _require_config(config: Any = None) -> Any:
    """``_current_config`` 的强制版：没有实例就是调用方的错。"""
    cfg = _current_config(config)
    if cfg is None:
        raise ValueError(
            "Configue is not initialized; construct/config it before saving"
        )
    return cfg


def _custom_converters() -> dict[Any, Any] | None:
    """运行期注册的自定义转换器（配置未就绪时返回 ``None``）。"""
    cfg = _current_config()
    if cfg is None:
        return None
    try:
        return cfg.resolver.custom_converters()
    except Exception:  # noqa: BLE001  半初始化状态：退回内置语义
        return None


# ══════════════════════════════════════════════════════════════
# 自动建树
# ══════════════════════════════════════════════════════════════

def _auto_group_order(
        orders: dict[str, int], section: str, order: int,
) -> None:
    """同一 section 被多个类声明时，组 ``order`` 取最小值。"""
    current = orders.get(section)
    orders[section] = order if current is None else min(current, order)


def _auto_groups(cfg: Any) -> list[SettingGroup]:
    """按 ``registry.declarations()`` 建出自动分组（每个 section 一组）。"""
    buckets: dict[str, SettingGroup] = {}
    orders: dict[str, int] = {}
    seen: set[tuple[str, str]] = set()

    for info in _declaration_infos():
        for declared in info.fields:
            section = declared.path[0]
            key = declared.path[-1]
            node = (section, key)
            if node in seen:
                continue
            seen.add(node)

            group = buckets.get(section)
            if group is None:
                group = SettingGroup(
                    section,
                    section,
                    order=info.order,
                )
                buckets[section] = group
                orders[section] = info.order
            else:
                _auto_group_order(orders, section, info.order)
            group.add(_auto_item(declared, section, key))

    for section, group in buckets.items():
        group.order = orders.get(section, 0)

    return [_attach_group_file(group, cfg) for group in buckets.values()]


def _declaration_infos() -> tuple[ClassInfo, ...]:
    """``registry.declarations()`` 的薄包装（便于测试替换）。"""
    return declarations()


def _auto_item(declared: FieldInfo, section: str, key: str) -> SettingItem:
    """把一条 ``FieldInfo`` 转成自动叶子。

    ``default`` 用 ``FieldInfo.has_default`` 判断——``FieldInfo.default`` 可能
    是 ``MISSING`` 哨兵，直接拿它当值会让 dump 把哨兵当配置值。
    """
    return SettingItem(
        name=key,
        section=section,
        key=key,
        label=declared.label or key,
        description=declared.description,
        default=declared.default if declared.has_default else UNSET,
        value_type=declared.type,
    )


def _attach_group_file(group: SettingGroup, cfg: Any) -> SettingGroup:
    """组级 ``file``：仅当组内所有叶子归属同一文件时才填。

    「归属文件」按 ``Configue.target_file_of`` 算（与 ``flush`` 同源）；
    任一个叶子推不出来或彼此不同 → 组级 ``file`` 为 ``None``，叶子各自带
    自己的 ``target_file``。
    """
    if cfg is None:
        return group
    targets: set[Path] = set()
    for _group, item in group.walk_items():
        try:
            targets.add(cfg.target_file_of(item.section, item.key))
        except ConfigError:
            return group
        except Exception:  # noqa: BLE001  半初始化状态：不阻断 dump
            return group
    if len(targets) == 1:
        group.file = next(iter(targets))
    return group


def _find_subgroup(
        root: SettingGroup, group_id: str,
) -> Optional[SettingGroup]:
    """在 ``root`` 的子树里按 id 找分组（先直接子层，再递归）。"""
    for child in root.groups:
        if child.id == group_id:
            return child
    for child in root.groups:
        found = _find_subgroup(child, group_id)
        if found is not None:
            return found
    return None


def _merge_group(target: SettingGroup, source: SettingGroup) -> None:
    """把 ``source``（手写）的展示信息与子项合并进 ``target``（自动）。"""
    if source.title and source.title != source.id:
        target.title = source.title
    if source.description:
        target.description = source.description
    if source.order:
        target.order = source.order
    if not source.editable:
        target.editable = False
    if source.file is not None:
        target.file = source.file

    for child in source.children:
        if isinstance(child, SettingItem):
            _merge_item(target, child)
        else:
            existing = _find_subgroup(target, child.id)
            if existing is None:
                target.add(_clone_group(child))
            else:
                _merge_group(existing, child)


def _find_auto_item(
        group: SettingGroup, manual: SettingItem,
) -> Optional[SettingItem]:
    """在手写叶子对应的分组里找自动叶子。

    匹配优先级：``(section, key)`` 完全相同 → 同 ``name``。自动叶子的
    ``section`` 一定非空，所以「手写只给了名字」时自然落到按名字匹配。
    """
    for item in group.items:
        if manual.section and manual.key and item.node == manual.node:
            return item
    for item in group.items:
        if item.name == manual.name:
            return item
    return None


def _merge_item(group: SettingGroup, manual: SettingItem) -> None:
    """把手写叶子的「非默认字段」覆盖到自动叶子上。

    覆盖规则是**非空即覆盖**：``label`` / ``description`` 空串、``default``
    为 ``UNSET`` 都表示「不关心，用自动值」。这里只合**声明层**字段；控件
    类别 / 约束 / 选项文案 / 高级分级属于展示策略，归 CLI 侧的覆盖表。
    找不到对应自动叶子时把手写叶子直接挂进分组（新增一个叶子）。
    """
    target = _find_auto_item(group, manual)
    if target is None:
        if any(item.name == manual.name for item in group.items):
            return
        group.add(manual)
        return

    if manual.label:
        target.label = manual.label
    if manual.description:
        target.description = manual.description
    if manual.choices:
        target.choices = tuple(manual.choices)
    if not manual.editable:
        target.editable = False
    if manual.order:
        target.order = manual.order
    if manual.default is not UNSET:
        target.default = manual.default
    if manual.value_type is not None:
        target.value_type = manual.value_type
    if manual.file is not None:
        target.file = manual.file
    if manual.name and manual.name != target.name:
        target.name = manual.name


def _apply_manual(
        schema: SettingSchemaRegistry,
        manual: SettingSchemaRegistry,
) -> None:
    """把手写登记表挂到自动树上（手写分组 / 叶子优先于自动生成）。"""
    for source in manual.roots:
        target = schema.get(source.id)
        if target is None:
            schema.register(_clone_group(source))
        else:
            _merge_group(target, source)


def build_schema(
        config: Any = None,
        *,
        registry: Optional[SettingSchemaRegistry] = None,
) -> SettingSchemaRegistry:
    """建一棵完整的设置树：先自动枚举声明，再挂上手写覆盖。

    Args:
        config: 显式 ``Configue``；默认取当前单例（未构造时也能建树，只是
            所有叶子都 ``target_file=None``、值退回声明默认值）。
        registry: 手写覆盖登记表；默认用进程级 :data:`_MANUAL_SCHEMA`。

    Returns:
        ``SettingSchemaRegistry``：根分组 → 子分组 / 叶子，叶子已带
        ``section`` / ``key`` / 类型 / 默认值 / 展示文案。
    """
    cfg = _current_config(config)
    schema = SettingSchemaRegistry()
    for group in _auto_groups(cfg):
        schema.register(group)
    _apply_manual(schema, _MANUAL_SCHEMA if registry is None else registry)
    return schema


def get_schema() -> SettingSchemaRegistry:
    """进程内缓存的 :func:`build_schema` 结果。

    声明表变化（插件晚注册、手写覆盖新增）后必须调 :func:`invalidate_schema`，
    否则读到的还是旧树。
    """
    global _CACHED_SCHEMA
    if _CACHED_SCHEMA is None:
        _CACHED_SCHEMA = build_schema()
    return _CACHED_SCHEMA


def invalidate_schema() -> None:
    """丢弃 :func:`get_schema` 的缓存。"""
    global _CACHED_SCHEMA
    _CACHED_SCHEMA = None


# ══════════════════════════════════════════════════════════════
# dump：解析每个叶子
# ══════════════════════════════════════════════════════════════

def _resolve_item(
        group: SettingGroup,
        item: SettingItem,
        inherited_file: Path | None,
        cfg: Any,
) -> dict[str, Any]:
    """把一个叶子解析成 dump 用的 dict（读失败也不抛异常）。"""
    node = item.node
    default = None if item.default is UNSET else item.default

    present = False
    dirty = False
    converter = None
    origin: Path | None = None
    read_error: str | None = None
    value: Any = default
    target: Path | None = None

    explicit_file = item.file if item.file is not None else inherited_file

    if cfg is not None:
        resolver = cfg.resolver
        present = node in resolver
        if present:
            try:
                value = resolver.get_typed(*node)
            except Exception as exc:  # noqa: BLE001  dump 不能因为坏值炸掉
                read_error = str(exc)
                value = default
            converter = resolver.converter_of(*node)
            origin = resolver.origin_of(*node)
        try:
            dirty = node in cfg.dirty_nodes()
        except Exception:  # noqa: BLE001  半初始化状态
            dirty = False
        if explicit_file is not None:
            target = explicit_file
        else:
            try:
                target = cfg.target_file_of(*node)
            except Exception:  # noqa: BLE001  未 load / 无归属：进 unbound
                target = None
    else:
        target = explicit_file

    value_type = converter or item.value_type
    if value_type is None and value is not None:
        value_type = type(value)

    choices = _choices_for(value_type)

    readonly = False
    if origin is not None and cfg is not None:
        try:
            readonly = bool(cfg.is_readonly_path(origin))
        except Exception:  # noqa: BLE001  只读判定失败按「可写」处理
            readonly = False

    return {
        "key": f"{group.qualname}.{item.name}",
        "name": item.name,
        "group": group.qualname,
        "path": item.path_text,
        "section": item.section,
        "config_key": item.key,
        # dump 时才求值：``label`` / ``description`` 可以是 i18n callable
        "label": _resolve_text(item.label) or item.name,
        "description": _resolve_text(item.description),
        "value": jsonable(value),
        "value_repr": _value_repr(value),
        "default": jsonable(default),
        "type": type_name(value_type),
        "choices": [jsonable(choice) for choice in choices],
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


def _dump_group(
        group: SettingGroup,
        inherited_file: Path | None,
        cfg: Any,
) -> dict[str, Any]:
    """递归导出一个分组（叶子 + 子分组 + 计数）。"""
    group_file = group.file if group.file is not None else inherited_file
    items = [
        _resolve_item(group, item, group_file, cfg)
        for item in group.items
    ]
    children = [
        _dump_group(child, group_file, cfg)
        for child in group.groups
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
        "groups": children,
        "item_count": sum(
            [len(items)] + [child["item_count"] for child in children]
        ),
    }


def _flatten_groups(
        node: dict[str, Any],
) -> Iterator[dict[str, Any]]:
    """按前序遍历摊平分组树里的叶子。"""
    yield from node["items"]
    for child in node["groups"]:
        yield from _flatten_groups(child)


def _grouping(
        items: Iterable[dict[str, Any]],
) -> tuple[dict[str, list[str]], list[str]]:
    """按 ``target_file`` 分组（``by_file``）并收集 ``unbound``。"""
    by_file: dict[str, list[str]] = {}
    unbound: list[str] = []
    for item in items:
        target = item["target_file"]
        if target is None:
            unbound.append(item["key"])
        else:
            by_file.setdefault(target, []).append(item["key"])
    return by_file, unbound


def dump_schema_dict(
        config: Any = None,
        *,
        registry: Optional[SettingSchemaRegistry] = None,
) -> dict[str, Any]:
    """``view="tree"`` 的完整 dict（键名锁死，设置 GUI 直接消费）。

    Returns:
        ``{"groups": [...], "items": [...], "by_file": {...}, "unbound": [...],
        "summary": {"group_count", "item_count", "file_count"}}``。
    """
    cfg = _current_config(config)
    schema = build_schema(cfg, registry=registry)

    groups = [_dump_group(root, None, cfg) for root in schema.roots]
    items: list[dict[str, Any]] = []
    for group in groups:
        items.extend(_flatten_groups(group))

    by_file, unbound = _grouping(items)
    return {
        "groups": groups,
        "items": items,
        "by_file": by_file,
        "unbound": unbound,
        "summary": {
            "group_count": schema.group_count,
            "item_count": len(items),
            "file_count": len(by_file),
        },
    }


def dump_schema_by_file(
        config: Any = None,
        *,
        registry: Optional[SettingSchemaRegistry] = None,
) -> dict[str, Any]:
    """按归属文件分组输出叶子。

    Returns:
        ``{"files": [{"file": str, "items": [item dict, ...]}], "unbound": [...]}``
        ——文件顺序是「首次出现顺序」，``unbound`` 是推不出归属文件的叶子。
    """
    dump = dump_schema_dict(config, registry=registry)
    by_file: dict[str, list[dict[str, Any]]] = {}
    for item in dump["items"]:
        target = item["target_file"]
        if target is not None:
            by_file.setdefault(target, []).append(item)
    return {
        "files": [
            {"file": path, "items": items} for path, items in by_file.items()
        ],
        "unbound": [
            item for item in dump["items"] if item["target_file"] is None
        ],
    }


def _strip_advanced_group(
        node: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """丢掉 ``advanced`` 子树（只按约定 id 判：``advanced`` / ``advanced.*``）。

    分组 dict 里**没有** ``advanced`` 字段（那是展示分级，已迁到 CLI 侧），
    所以这里只看 id 约定——与旧实现里「自动分组按约定 id 打标记」的判定同源。
    """
    if _is_advanced_id(node["id"]):
        return None
    children = [
        kept for kept in (
            _strip_advanced_group(child) for child in node["groups"]
        )
        if kept is not None
    ]
    kept_node = dict(node)
    kept_node["groups"] = children
    kept_node["item_count"] = sum(
        [len(kept_node["items"])]
        + [child["item_count"] for child in children]
    )
    return kept_node


def _basic_dump(dump: dict[str, Any]) -> dict[str, Any]:
    """``view="basic"``：去掉高级分组，并按过滤后的叶子重算派生字段。"""
    groups = [
        kept for kept in (_strip_advanced_group(group) for group in dump["groups"])
        if kept is not None
    ]
    items: list[dict[str, Any]] = []
    for group in groups:
        items.extend(_flatten_groups(group))
    by_file, unbound = _grouping(items)
    return {
        "groups": groups,
        "items": items,
        "by_file": by_file,
        "unbound": unbound,
        "summary": {
            "group_count": sum(1 for _g in _iter_group_nodes(groups)),
            "item_count": len(items),
            "file_count": len(by_file),
        },
    }


def _iter_group_nodes(
        groups: Iterable[dict[str, Any]],
) -> Iterator[dict[str, Any]]:
    for group in groups:
        yield group
        yield from _iter_group_nodes(group["groups"])


def dump_schema(
        *,
        view: str = "tree",
        config: Any = None,
        registry: Optional[SettingSchemaRegistry] = None,
) -> dict[str, Any] | list[dict[str, Any]] | str:
    """导出设置结构（唯一对外查询入口）。

    只输出**声明事实 + 值 + 状态**：分组 / 叶子 / 归属文件 / 当前值 / 脏标记。
    展示策略（控件类型、控件约束、选项文案、``advanced`` 分级、树形文本）由
    消费者自己算——CLI 在 ``service/command/handlers/_present.py``。

    ``view`` 语义：

    * ``"tree"``：完整嵌套 dict（``groups`` / ``items`` / ``by_file`` /
      ``unbound`` / ``summary``）；
    * ``"basic"``：丢掉 ``advanced`` 子树（按约定 id 判），派生字段按过滤后
      的叶子重算；
    * ``"by_file"``：只按归属文件分组（``{"files": [...], "unbound": [...]}``）；
    * ``"flat"``：扁平的叶子列表（``tree["items"]``）。

    默认 ``"tree"``；命令行请显式传 ``view``（``setting schema`` 命令默认
    ``text``，由 CLI 侧 ``format_text`` 渲染，见 ``handlers/_present.py``）。
    """
    if view == "by_file":
        return dump_schema_by_file(config, registry=registry)
    if view not in ("tree", "basic", "flat"):
        raise ValueError(
            f"unknown view {view!r}; expected tree / basic / by_file / flat"
        )

    dump = dump_schema_dict(config, registry=registry)
    if view == "flat":
        return dump["items"]
    if view == "basic":
        return _basic_dump(dump)
    return dump


# ══════════════════════════════════════════════════════════════
# 写回
# ══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class SettingWriteResult:
    """一次写回的结果。

    ``dry_run=True`` 时 ``written`` / ``flushed`` / ``applied`` 都是空的，
    真正的信息在 ``changes`` 里（每个叶子的旧值 / 新值 / 将写入的文本 /
    归属文件）。
    """

    written: tuple[Path, ...]
    by_file: dict[Path, tuple[str, ...]]
    flushed: tuple[str, ...]
    applied: tuple[str, ...]
    changes: tuple[dict[str, Any], ...] = ()
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        """CLI / IPC / GUI 直接序列化的形态（键名与旧实现一致）。"""
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


def _item_key(group: SettingGroup, item: SettingItem) -> str:
    """叶子的全局键：``<group.qualname>.<name>``。"""
    return f"{group.qualname}.{item.name}"


def _describe_key(key: Any) -> str:
    """报错用的键文本（元组写成 ``"a.b"`` 更好认）。"""
    if isinstance(key, tuple):
        return ".".join(str(part) for part in key)
    return str(key)


def _lookup_value(
        values: Mapping[Any, Any],
        group: SettingGroup,
        item: SettingItem,
        unique_names: Mapping[str, SettingItem],
        used: set[Any] | None = None,
) -> Any:
    """在 ``values`` 里找这个叶子的新值。

    支持四种键写法（任一命中即用）：``"group.item"``、``(section, key)``
    二元组、``"[section]key"``、以及**本次保存范围内唯一**的裸叶子名。
    重名叶子不给裸名匹配——宁可「没提供值」，也不能把值写进别的组。

    ``used`` 非空时记录已命中的键，调用方据此发现「谁都没命中」的键
    （拼错的 key 必须报错，不能静默当没给）。
    """
    candidates: list[Any] = [
        _item_key(group, item),
        (item.section, item.key),
        item.path_text,
    ]
    if unique_names.get(item.name) is item:
        candidates.append(item.name)

    for candidate in candidates:
        try:
            if candidate in values:
                if used is not None:
                    used.add(candidate)
                return values[candidate]
        except TypeError:  # pragma: no cover - 不可哈希的键
            continue
    return _UNAVAILABLE


def _normalize_values(values: Mapping[Any, Any]) -> dict[Any, Any]:
    """键归一：``["demo", "x"]``（JSON 里的数组）→ ``("demo", "x")`` 元组。"""
    normalized: dict[Any, Any] = {}
    for key, value in values.items():
        normalized[tuple(key) if isinstance(key, list) else key] = value
    return normalized


def _finfo_for_path(path: Path) -> Any:
    """已加载文件里 ``resolved_path == path`` 的 ``FileInfo``（找不到为 ``None``）。"""
    for finfo in registered_files():
        if finfo.loaded and finfo.resolved_path == path:
            return finfo
    return None


def _current_value(cfg: Any, item: SettingItem) -> Any:
    """叶子当前的值（读不到时退回声明默认值，只用于判断「有没有变」）。"""
    try:
        if item.node in cfg.resolver:
            return cfg.resolver.get_typed(*item.node)
    except Exception:  # noqa: BLE001  读失败按「无值」处理
        pass
    return None if item.default is UNSET else item.default


def _same_value(old: Any, new: Any) -> bool:
    """``old == new``，比较不出来就当作「不同」（宁可多写一次）。"""
    try:
        return bool(old == new)
    except Exception:  # noqa: BLE001
        return False


def _explicit_file(
        item: SettingItem, inherited_file: Path | None,
) -> Path | None:
    return item.file if item.file is not None else inherited_file


def _entry_target(
        cfg: Any,
        item: SettingItem,
        inherited_file: Path | None,
) -> Path | None:
    """叶子的写回目标；推不出来返回 ``None``。

    优先用 ``Configue.target_file_of``（与 ``flush`` 完全同源）；推不出来时
    才退回显式 ``file``（``Item.file`` / 分组继承）。两者都没有说明这个
    声明所在的文件还没 ``reg()`` / 没 ``load()``——**只要没人给它新值就不算
    错**（dump 里它进 ``unbound``）；真的有人要写它才在调用处抛 ``ValueError``。
    """
    try:
        return cfg.target_file_of(item.section, item.key)
    except Exception:  # noqa: BLE001  未 load / 无归属：交给调用方判定
        return _explicit_file(item, inherited_file)


def _serialize_preview(cfg: Any, target: Path, item: SettingItem, value: Any) -> Any:
    """dry-run 用的「将写入的文本」；算不出来返回 ``None``（不影响结论）。"""
    finfo = _finfo_for_path(target)
    if finfo is None:
        return None
    try:
        backend = get_backend(finfo.backend)
        return backend.serialize(
            value,
            cfg.resolver.converter_of(*item.node),
            cfg.resolver.custom_converters(),
            cfg.resolver.custom_serializers(),
        )
    except Exception:  # noqa: BLE001  预览失败只影响 text 字段
        return None


def _describe_change(
        cfg: Any,
        group: SettingGroup,
        item: SettingItem,
        target: Path,
        coerced: Any,
        old: Any,
) -> dict[str, Any]:
    """一条 change 记录（``text`` 是「将写入该文件的文本」）。"""
    return {
        "key": _item_key(group, item),
        "path": item.path_text,
        "section": item.section,
        "config_key": item.key,
        "old": jsonable(old),
        "new": jsonable(coerced),
        "text": _serialize_preview(cfg, target, item, coerced),
        "file": str(target),
        "changed": not _same_value(old, coerced),
    }


def _save_entries(
        entries: Sequence[tuple[SettingGroup, SettingItem, Path | None]],
        values: Mapping[Any, Any] | None,
        cfg: Any,
        *,
        strict: bool,
        dry_run: bool,
) -> SettingWriteResult:
    """两遍写回算法（见模块 docstring）。

    第一遍**只读**：算归属文件、转换新值、取旧值；任何一步失败立刻抛
    ``ValueError`` / ``TypeError``，内存与磁盘都不动（不留半截修改）。
    第二遍才 ``resolver.set(persist=True)`` + ``cfg.flush(only=...)``。
    """
    name_counts = Counter(item.name for _g, item, _f in entries)
    unique_names = {
        item.name: item for _g, item, _f in entries
        if name_counts[item.name] == 1
    }

    provided = None if values is None else _normalize_values(values)
    used: set[Any] = set()

    by_file: dict[Path, list[str]] = {}
    planned: list[tuple[SettingGroup, SettingItem, Path, Any, Any]] = []

    # ── 第一遍：只读校验 ──────────────────────────────────────
    for group, item, inherited in entries:
        target = _entry_target(cfg, item, inherited)
        if target is not None:
            by_file.setdefault(target, []).append(_item_key(group, item))
        if provided is None:
            continue
        raw = _lookup_value(provided, group, item, unique_names, used)
        if raw is _UNAVAILABLE:
            continue
        if target is None:
            raise ValueError(
                f"setting item {item.path_text}: no owning file "
                f"(declared file is not registered / not loaded)"
            )
        coerced = item.coerce(
            raw, custom_converters=cfg.resolver.custom_converters(),
        )
        planned.append((group, item, target, coerced, _current_value(cfg, item)))

    if provided is not None:
        leftovers = [key for key in provided if key not in used]
        ambiguous = [
            key for key in leftovers
            if isinstance(key, str) and name_counts.get(key, 0) > 1
        ]
        if ambiguous:
            raise ValueError(
                "ambiguous setting key(s): "
                + ", ".join(sorted(ambiguous))
                + " (that name exists in several groups; "
                "use '<group>.<name>')"
            )
        if leftovers:
            unknown = [_describe_key(key) for key in leftovers]
            raise ValueError(
                "unknown setting key(s): " + ", ".join(sorted(unknown))
            )

    by_file_result = {path: tuple(keys) for path, keys in by_file.items()}
    changes = tuple(
        _describe_change(cfg, group, item, target, coerced, old)
        for group, item, target, coerced, old in planned
    )

    if dry_run:
        return SettingWriteResult(
            written=(),
            by_file=by_file_result,
            flushed=(),
            applied=(),
            changes=changes,
            dry_run=True,
        )

    # ── 第二遍：写内存 + 按归属文件统一落盘 ────────────────────
    applied: list[str] = []
    nodes: list[tuple[str, str]] = []
    wrote: list[tuple[str, tuple[str, str]]] = []
    for group, item, _target, coerced, old in planned:
        if _same_value(old, coerced):
            continue
        cfg.resolver.set(item.section, item.key, coerced, persist=True)
        key = _item_key(group, item)
        applied.append(key)
        nodes.append(item.node)
        wrote.append((key, item.node))

    written = tuple(cfg.flush(only=nodes, strict=strict)) if nodes else ()
    dirty = cfg.dirty_nodes()
    flushed = tuple(key for key, node in wrote if node not in dirty)

    return SettingWriteResult(
        written=written,
        by_file=by_file_result,
        flushed=flushed,
        applied=tuple(applied),
        changes=changes,
    )


def save_values(
        values: Mapping[Any, Any] | None = None,
        *,
        config: Any = None,
        registry: Optional[SettingSchemaRegistry] = None,
        strict: bool = False,
        dry_run: bool = False,
) -> SettingWriteResult:
    """把一批「叶子 → 新值」写回各自的归属文件。

    Args:
        values: ``{键: 值}``。键支持 ``"group.item"`` / ``(section, key)`` /
            ``"[section]key"`` / 本次范围内唯一的裸叶子名；值可以是字符串
            （按声明类型转换）或已类型化的值。
        config: 显式 ``Configue``（默认取单例）。
        registry: 手写覆盖登记表（默认进程级）。
        strict: 写盘失败时立即抛 ``ConfigError``；默认吞掉、保留 dirty。
        dry_run: 只校验 + 预览，不写内存也不写盘，不 ``mark_clean``。

    Returns:
        :class:`SettingWriteResult`。

    Raises:
        ValueError: 值转不过去 / 键不属于任何已声明叶子 / 叶子无归属文件。
        ConfigError: ``strict=True`` 且写盘失败（由 ``flush`` 抛出）。
    """
    cfg = _require_config(config)
    schema = build_schema(cfg, registry=registry)
    entries = [
        (group, item, group.file)
        for group, item in schema.iter_items()
    ]
    return _save_entries(entries, values, cfg, strict=strict, dry_run=dry_run)


def save_group(
        group: SettingGroup | str,
        values: Mapping[Any, Any] | None = None,
        *,
        config: Any = None,
        registry: Optional[SettingSchemaRegistry] = None,
        strict: bool = False,
        dry_run: bool = False,
) -> SettingWriteResult:
    """把某个分组（含子分组）里的编辑值写回各自归属文件。

    ``values`` / ``strict`` / ``dry_run`` 的语义与 :func:`save_values` 一致，
    区别只有保存范围：裸叶子名在这里按**组内**唯一性判定。

    Raises:
        KeyError: ``group`` 是字符串且未登记。
        TypeError: ``group`` 既不是 ``SettingGroup`` 也不是字符串。
    """
    cfg = _require_config(config)
    schema = build_schema(cfg, registry=registry)

    if isinstance(group, str):
        resolved = schema.get(group)
        if resolved is None:
            raise KeyError(f"setting group {group!r} is not registered")
        group = resolved
    elif not isinstance(group, SettingGroup):
        raise TypeError(
            f"save_group expected SettingGroup or group id, "
            f"got {type(group).__name__}"
        )

    entries = list(group._walk_entries(group.file))
    return _save_entries(entries, values, cfg, strict=strict, dry_run=dry_run)
