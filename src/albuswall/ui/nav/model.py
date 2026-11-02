#
"""``albuswall.ui.nav`` 的数据模型。

四个概念：

==============  ==========================================================
:class:`Node`   一个可落点：id 全局唯一，``widget`` 是不透明对象（本层不
                做类型检查、不 import Qt）。
:class:`Domain` 一组节点的集合，带「有根 / 无根」「能否退出」「方向别名」
                等导航语义。
:class:`NavPlacement` 快捷键槽上的导航声明，由
                :meth:`NavRegistry.register_from_slot` 转成 :class:`Node`。
:class:`PageState` 一个页面的域栈（``stack`` 外部只读）。
==============  ==========================================================

本模块只依赖 ``collections`` / ``dataclasses`` / ``enum`` / ``typing``，
不 import Qt。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from collections import namedtuple
from typing import Any, Callable

from albuswall.ui.nav.errors import InvalidNavGrid

logger = logging.getLogger(__package__)


class Direction(Enum):
    """四个正交方向。"""

    UP = "up"
    DOWN = "down"
    LEFT = "left"
    RIGHT = "right"


# 导航键位字符串常量。与 Direction.value 对齐（up/down/left/right），
# 额外覆盖非方向键 enter/esc/tab。nav 不 import Qt，Qt 键值到这些字符串的
# 转换由调用方（通常经 keymap 层）完成。
NAV_UP = "up"
NAV_DOWN = "down"
NAV_LEFT = "left"
NAV_RIGHT = "right"
NAV_ENTER = "enter"
NAV_ESC = "esc"
NAV_TAB = "tab"

# 让渡全集：控件自治的默认键位集合。
# 注意：模块级可变常量，调用方若需修改请先 .copy()。
DEFAULT_PASS_THROUGH_KEYS: set[str] = {
    NAV_UP, NAV_DOWN, NAV_LEFT, NAV_RIGHT, NAV_ENTER, NAV_ESC, NAV_TAB,
}


_GridPlacementBase = namedtuple(
    "GridPlacement", ("row", "col", "rowspan", "colspan"), defaults=(1, 1))


class GridPlacement(_GridPlacementBase):
    """网格坐标。``rowspan`` / ``colspan`` 默认 1。

    负值或 ``span < 1`` 抛 :class:`InvalidNavGrid`——网格坐标写错属于声明
    错误，应该在注册期炸掉，而不是留到方向键按下时才表现成「找不到邻居」。

    实现说明：用 :func:`collections.namedtuple` 派生而不是 ``typing.NamedTuple``，
    因为后者禁止在子类里自定义的 ``__new__`` 中调用 ``super().__new__``
    （Python 3.14 起直接 ``TypeError``），而构造期校验必须发生在 ``__new__``。
    """

    __slots__ = ()

    def __new__(cls, row, col, rowspan=1, colspan=1):
        if row < 0 or col < 0:
            raise InvalidNavGrid(
                f"grid.row/col must be >= 0, got ({row}, {col})")
        if rowspan < 1 or colspan < 1:
            raise InvalidNavGrid(
                f"rowspan/colspan must be >= 1, got ({rowspan}, {colspan})")
        return _GridPlacementBase.__new__(cls, row, col, rowspan, colspan)


@dataclass(frozen=True, slots=True)
class Node:
    """导航节点。``id`` 全局唯一。

    Attributes:
        widget: 任意对象。nav 层不做类型检查、不 import Qt；焦点执行由
            :meth:`NavRegistry.focus_node` 完成。
        grid: 网格导航场景必填；纯方向映射导航（如 navbar 的 next/prev）
            可为 ``None``。
        child_domain: 该节点是「容器」时的子域 id；``focusable=False`` +
            ``child_domain`` 的组合即「容器踏板」。
        action: ``how="enter"`` 且无 ``child_domain`` 时调用。
        focusable: 是否参与 move / enter 落点。
        focus_fn: 自定义焦点执行函数，优先于 ``widget.setFocus()``。
        visible_fn: 返回 ``False`` 时该节点不参与落点判定（entry / 第一个
            focusable / 网格扫描 / 线性导航一律跳过）。``None`` = 恒可见。
            抛异常视为隐藏并 warning 一次。不阻止 :meth:`NavRegistry.set_current`
            强制设置（外部权威，仅 warning）。
        pass_through_keys: 声明该节点让渡哪些键给控件自治，key ∈ ``NAV_UP`` /
            ``NAV_DOWN`` / ... 字符串。``None`` = 不显式声明，回退读
            ``widget.keymap_passthrough``（``True`` 则让渡全集）；``set()`` =
            显式空集（覆盖 ``widget.keymap_passthrough=True``，即不让渡）；
            非空 ``set`` = 只让渡其中列出的键。
    """

    id: str
    widget: Any
    grid: GridPlacement | None = None
    child_domain: str | None = None
    action: Callable[[], None] | None = None
    focusable: bool = True
    focus_fn: Callable[[], None] | None = None
    # 行为配置不属于节点身份：``set`` 不可 hash，且这两个字段不该影响相等性。
    visible_fn: Callable[[], bool] | None = field(
        default=None, hash=False, compare=False,
    )
    pass_through_keys: set[str] | None = field(
        default=None, hash=False, compare=False,
    )


@dataclass
class Domain:
    """导航域。

    有根 / 无根：

    - ``has_root=True``：Enter 进入落虚拟根（``current=None``）；Tab 落
      ``entry`` / 第一个 ``focusable``。
    - ``has_root=False``：Tab 和 Enter 都落 ``entry`` / 第一个
      ``focusable``。无根域 ``entry`` 必填。

    ``entry`` 优先级：``entry`` 优先于 ``focusable`` 判定，即使 entry 节点
    ``focusable=False`` 也作为落点候选。落点（entry / 第一个 focusable /
    fallback 第一个节点）若 ``focusable=False`` 且有 ``child_domain`` →
    容器踏板，自动推入子域（最多 4 层递归）。落点 ``focusable=False`` 且无
    ``child_domain`` → 视为不可落点，move / enter 返回 ``None``。

    栈语义：进入子域时父帧 ``current`` 不更新；退出时恢复进入前的值。
    ``focusable=False`` 的域代表格可以出现在栈帧 ``current`` 中，但不参与
    move / enter 落点。
    """

    id: str
    has_root: bool = True
    root_action: Callable[[], None] | None = None
    entry: str | None = None
    """优先于 ``focusable``。无根域必填（缺失 → 注册时 warning）。"""

    allow_exit: bool = True
    """Shift+Tab 能否从此域弹出。``False`` = 卡住。esc / shift_esc 不受约束。"""

    is_real_page: bool = False
    is_page_focus: bool = False
    nodes: dict[str, Node] = field(default_factory=dict)
    parent: str | None = None
    """逻辑父；域 id 或节点 id 都行。仅做记录，退出落点靠
    ``PageState.stack`` 父帧。"""

    esc_clears_current: bool = False
    """仅 ``has_root=True`` 的域有意义。``True`` 时 Esc 先 ``current=None``，
    再 Esc 弹栈。"""

    direction_aliases: dict[Direction, str] = field(default_factory=dict)
    """光标在此域内时生效。value ∈ ``{"enter", "next", "prev"}``。"""

    exit_directions: set[Direction] = field(default_factory=set)
    """沿这些方向找不到网格邻居时弹栈回父域（navbar 用 ``{UP, DOWN}``）。"""


@dataclass(frozen=True, slots=True)
class NavPlacement:
    """导航声明（用于 ``@shortcut_slot(nav=...)``）。

    :meth:`NavRegistry.register_from_slot` 读取本对象并注册等价 :class:`Node`。
    节点 id 取自槽自身的 id（``slot_id`` / ``id``）。
    """

    widget: Any
    domain: str
    grid: GridPlacement | None = None
    parent: str | None = None
    child_domain: str | None = None
    focusable: bool = True
    action: Callable[[], None] | None = None
    focus_fn: Callable[[], None] | None = None
    visible_fn: Callable[[], bool] | None = None
    pass_through_keys: set[str] | None = None


@dataclass
class DomainFrame:
    """域栈里的一帧：域 id + 该域内的光标。"""

    domain_id: str
    current: str | None


@dataclass
class PageState:
    """页面状态。``stack`` 外部只读——修改会导致栈语义崩。"""

    page_id: str
    stack: list[DomainFrame] = field(default_factory=list)


__all__ = [
    "DEFAULT_PASS_THROUGH_KEYS",
    "Direction",
    "Domain",
    "DomainFrame",
    "GridPlacement",
    "NAV_DOWN",
    "NAV_ENTER",
    "NAV_ESC",
    "NAV_LEFT",
    "NAV_RIGHT",
    "NAV_TAB",
    "NAV_UP",
    "NavPlacement",
    "Node",
    "PageState",
]
