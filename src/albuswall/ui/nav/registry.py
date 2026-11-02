#
"""``albuswall.ui.nav`` 的注册表：注册（域 / 节点）+ 决策（返回落点）。

本模块是**受控执行层**：不 import Qt、不装事件过滤器、不接 ``QShortcut``。
「往哪跳」的答案以返回值 / ``PageState`` 的形式交给调用方；「真的跳过去」
通过 :meth:`NavRegistry.focus_node` 执行，执行能力外包给
:class:`~albuswall.ui.nav.backend.FocusBackend`：

- 决策（本模块）决定落点、维护域栈与 ``current``；
- 执行（后端）负责 ``setFocus`` / 焦点策略 / 焦点链读取；
- 于是 ``focus_node`` 可以**验证**执行结果，并在调用方（分发器）需要时被
  回滚——逻辑光标与 Qt 真实焦点不再各自为政。

动作返回 ``None`` 的多种含义，调用方需结合 ``stack`` / ``current`` 区分：

- ``move``：未移动（边界 + 不在 ``exit_directions``），或无 grid 节点且方向
  未声明退出；
- ``enter``：落到虚拟根（``current=None``），或触发 ``root_action`` /
  ``node.action`` 后返回；
- ``exit_back``：栈深 ≤ 1，或 ``allow_exit=False``；
- ``esc``：``esc_clears_current`` 命中（取消选中），或栈深 ≤ 1；
- ``shift_esc`` / ``jump_to_page_root``：停在该域虚拟根或栈深 1。

返回 ``None`` 不等于「什么都没发生」（例如 Esc 取消选中、Tab 推入一个有根
子域）。要区分「动作是否有进展」请读 :attr:`NavRegistry.last_action_ok`：
它在每次动作后记录「拿到了落点，或页面状态真的变了」。

v2 待办（本次不实现，代码里不留 stub）：

- 叶域类型 ``Domain.is_leaf``：把「叶域」约定升级为一等类型（见
  ``__init__.py`` 定位节）；
- 动态网格提供者：列数运行期变化时用回调代替静态 ``GridPlacement``；
- 页面栈管理层：从 ``WindowPresenter`` 手写 ``setFocus`` 兜底中抽象；
- 页面↔域声明绑定：``PAGE_ROOTS = {page_id: root_domain}`` 显式声明表；
- ``unregister_*`` 级联：换图 / 换模型时清理悬空 ``current``；
- 网格 span 出口、网格碰撞检测、跨域 Tab 跳跃。

已实现（v1 收尾）：``FocusBackend`` 受控执行（``backend.py``）、模态
suspend / resume、域内 Tab 语义（``Domain.tab_order`` /
:meth:`NavRegistry.tab_next` / :meth:`NavRegistry.tab_prev`）。
"""

from __future__ import annotations

import functools
import logging
from typing import TYPE_CHECKING, Any, Callable, Literal, TypeVar

from albuswall.ui.nav.backend import FocusBackend, NullFocusBackend
from albuswall.ui.nav.errors import (
    DuplicateNavDomain,
    DuplicateNavNode,
    NavError,
    UnknownNavDomain,
    UnknownNavNode,
    UnknownNavPage,
)
from albuswall.ui.nav.model import (
    DEFAULT_PASS_THROUGH_KEYS,
    Direction,
    Domain,
    DomainFrame,
    NavPlacement,
    Node,
    PageState,
)

if TYPE_CHECKING:
    from albuswall.ui.keymap.model import ShortcutSlot
    # 只依赖槽的两个字段：节点 id 与 ``nav`` 声明。nav 层对 keymap 是单向
    # 依赖，且只在类型检查期可见。

logger = logging.getLogger(__package__)

_MAX_PEDAL_DEPTH = 4

_T = TypeVar("_T")

#: 一次动作的快照：``((domain_id, current), ...)``。
Snapshot = tuple[tuple[str, str | None], ...]


def _acting(method: Callable[..., Node | None]) -> Callable[..., Node | None]:
    """装饰 nav 动作方法：处理 ``suspend`` + 记录 :attr:`last_action_ok`。

    动作方法一律 ``(page_id, ...) -> Node | None``，所以这里能统一包起来，
    不必在每个方法里手写一遍「有没有进展」的判定。

    * 页面被 :meth:`NavRegistry.suspend` 挂起 → 直接 ``None``（不决策、不动
      状态），调用方据此把按键冒泡出去；
    * 否则先取状态快照，动作后判定：返回了落点，或快照变了 → ``True``。
    """
    @functools.wraps(method)
    def wrapper(self: NavRegistry, page_id: str, *args: Any,
                **kwargs: Any) -> Node | None:
        if page_id in self._suspended:
            self._last_action_ok = False
            logger.debug(
                "%s(%s) skipped: page suspended", method.__name__, page_id)
            return None
        before = self.snapshot(page_id)
        node = method(self, page_id, *args, **kwargs)
        self._last_action_ok = node is not None or self.snapshot(page_id) != before
        return node

    return wrapper


def _slot_node_id(slot: "ShortcutSlot") -> str:
    """取槽的 id 作为节点 id。

    优先级：``slot.slot_id`` → ``slot.id``。前者是 ``keymap.register_slot()``
    的形参名（未来若 ``ShortcutSlot`` 加 ``slot_id`` 字段会优先用）；后者是
    ``ShortcutSlot`` 当前的实际字段名（``keymap/model.py``）。两者都存在时用
    ``slot_id``（更正式）；都没有时 raise :class:`UnknownNavNode`。
    """
    for attr in ("slot_id", "id"):
        value = getattr(slot, attr, None)
        if isinstance(value, str) and value:
            return value
    raise UnknownNavNode(
        f"shortcut slot {slot!r} exposes neither 'slot_id' nor 'id'"
    )


class NavRegistry:
    """导航注册表。单一实例用 :func:`get_default_registry`。

    ``backend`` 是焦点执行后端（:class:`~albuswall.ui.nav.backend.FocusBackend`）。
    不给就是 :class:`~albuswall.ui.nav.backend.NullFocusBackend`：只调
    ``widget.setFocus()``、不做验证，行为与 v1 一致（无 Qt 的用例无需改动）。
    真实 Qt 应用由 ``NavPresenter`` 注入
    :class:`~albuswall.ui.nav.qt.QtFocusBackend`，于是 :meth:`focus_node`
    能验证执行结果。
    """

    def __init__(self, backend: FocusBackend | None = None) -> None:
        self._domains: dict[str, Domain] = {}
        self._nodes: dict[str, Node] = {}
        self._pages: dict[str, PageState] = {}
        self._backend: FocusBackend = backend or NullFocusBackend()
        #: 被挂起的页面（模态 / 窗口失活期间）：动作方法直接返回 ``None``。
        self._suspended: set[str] = set()
        self._last_action_ok = False

    # -- 执行后端 ------------------------------------------------------
    @property
    def backend(self) -> FocusBackend:
        """当前的焦点执行后端（只读；替换走 :meth:`set_backend`）。"""
        return self._backend

    def set_backend(self, backend: FocusBackend | None) -> None:
        """替换焦点执行后端；``None`` 恢复默认的 ``NullFocusBackend``。

        由 bootstrap / 分发器在界面起来之后调用（那时才知道 Qt 侧怎么读焦点
        链）。替换不影响已注册的域 / 节点 / 页面状态。
        """
        self._backend = backend or NullFocusBackend()
        logger.debug(
            "nav backend set to %r", type(self._backend).__name__)

    @property
    def last_action_ok(self) -> bool:
        """最近一次动作方法是否有进展（拿到落点，或页面状态真的变了）。

        动作方法仍返回 ``Node | None``（兼容旧调用方），但「返回 ``None``」
        有多种含义（边界、虚拟根、Esc 取消选中、推入有根子域……）。分发器要
        判断「这一下该不该消费事件」时读这个属性，不要去猜返回值。

        被 :meth:`suspend` 挂起时恒 ``False``。
        """
        return self._last_action_ok


    # -- 注册 ----------------------------------------------------------
    def register_domain(self, domain: Domain, *, replace: bool = False) -> Domain:
        """注册域。id 重复且 ``replace=False`` → :class:`DuplicateNavDomain`。

        ``parent`` 若给出必须已注册（域或节点）。``has_root=False`` 且
        ``entry=None`` → ``logger.warning``（无根域 entry 是落点前提）。
        """
        if domain.id in self._domains:
            if not replace:
                raise DuplicateNavDomain(
                    f"domain {domain.id!r} already registered")
            logger.debug("replacing domain %s", domain.id)
        if domain.parent is not None \
                and domain.parent not in self._domains \
                and domain.parent not in self._nodes:
            raise UnknownNavDomain(f"parent {domain.parent!r} not registered")
        if not domain.has_root and domain.entry is None:
            logger.warning(
                "domain %s has_root=False but entry is None; "
                "entry resolution may be undefined", domain.id,
            )
        self._domains[domain.id] = domain
        return domain

    def register_node(
            self, node: Node, *, domain_id: str, replace: bool = False,
    ) -> Node:
        """注册节点到指定域。校验：

        - ``domain_id`` 必须已注册 → :class:`UnknownNavDomain`
        - id 已存在且 ``replace=False`` → :class:`DuplicateNavNode`
        - ``replace=True`` 时先从旧域 ``nodes`` 移除，再写入新域
        - ``child_domain`` 若给出必须已注册 → :class:`UnknownNavDomain`
        - grid span > 1 时 ``logger.warning``（v1 网格扫描不支持 span）
        """
        if domain_id not in self._domains:
            raise UnknownNavDomain(f"domain {domain_id!r} not registered")
        if node.id in self._nodes:
            if not replace:
                raise DuplicateNavNode(
                    f"node {node.id!r} already registered")
            for d in self._domains.values():
                d.nodes.pop(node.id, None)
            logger.debug("replacing node %s", node.id)
        if node.child_domain is not None \
                and node.child_domain not in self._domains:
            raise UnknownNavDomain(
                f"child_domain {node.child_domain!r} not registered")
        if node.grid is not None \
                and (node.grid.rowspan > 1 or node.grid.colspan > 1):
            logger.warning(
                "node %s has span>1; grid scan does not support span in v1",
                node.id,
            )
        self._domains[domain_id].nodes[node.id] = node
        self._nodes[node.id] = node
        return node

    def register_from_slot(self, slot: "ShortcutSlot") -> Node | None:
        """``slot.nav is None`` 返回 ``None``；否则用 :class:`NavPlacement`
        构造 :class:`Node` 并注册。

        校验：

        - ``nav.domain`` 已注册 → :class:`UnknownNavDomain`
        - ``nav.parent`` 若给出必须已注册（域或节点）→
          :class:`UnknownNavDomain`
        """
        placement: NavPlacement | None = getattr(slot, "nav", None)
        if placement is None:
            return None
        if placement.parent is not None \
                and placement.parent not in self._domains \
                and placement.parent not in self._nodes:
            raise UnknownNavDomain(
                f"parent {placement.parent!r} not registered")
        node = Node(
            id=_slot_node_id(slot),
            widget=placement.widget,
            grid=placement.grid,
            child_domain=placement.child_domain,
            action=placement.action,
            focusable=placement.focusable,
            focus_fn=placement.focus_fn,
            visible_fn=placement.visible_fn,
            pass_through_keys=placement.pass_through_keys,
        )
        return self.register_node(node, domain_id=placement.domain)

    def unregister_node(self, node_id: str) -> None:
        """从 ``_nodes`` 和所属域 ``nodes`` 移除。

        不级联清理 ``child_domain``、``PageState.current``——悬空引用由调用方
        的页面重建流程收拾。
        """
        self._nodes.pop(node_id, None)
        for d in self._domains.values():
            d.nodes.pop(node_id, None)

    def unregister_domain(self, domain_id: str) -> None:
        """从 ``_domains`` 移除。

        不级联清理 ``_nodes``、``PageState``、``parent`` / ``child_domain``
        引用。

        TODO(v2): 是否级联清理。
        """
        self._domains.pop(domain_id, None)

    # -- 查询 ----------------------------------------------------------
    def get_domain(self, domain_id: str) -> Domain | None:
        return self._domains.get(domain_id)

    def get_node(self, node_id: str) -> Node | None:
        return self._nodes.get(node_id)

    def domains(self) -> tuple[Domain, ...]:
        return tuple(self._domains.values())

    def nodes_in(self, domain_id: str) -> tuple[Node, ...]:
        d = self._domains.get(domain_id)
        return tuple(d.nodes.values()) if d else ()

    # -- 页面状态 ------------------------------------------------------
    def ensure_page(self, page_id: str, *, root_domain: str) -> PageState:
        """首次：创建 page_focus 域（``id=f"{page_id}::page_focus"``,
        ``is_page_focus=True``, ``has_root=True``），栈 =
        ``[page_focus(None), root_domain(None)]``。

        已存在返回同一对象（即使 ``root_domain`` 不同，也返回旧对象，仅 debug
        日志）。校验：``root_domain`` 已注册；``has_root=True``（否则
        :class:`NavError`）。
        """
        if page_id in self._pages:
            stack = self._pages[page_id].stack
            # 栈可能已被 esc / shift_esc 弹到只剩 page_focus 一帧（调用方误用），
            # 这种情况下缺少可比对的根帧，不去读 stack[1]。
            current_root = stack[1].domain_id if len(stack) > 1 else None
            if current_root != root_domain:
                logger.debug(
                    "ensure_page(%s) already exists with root %s; "
                    "ignoring new root_domain %s",
                    page_id, current_root, root_domain,
                )
            return self._pages[page_id]
        if root_domain not in self._domains:
            raise UnknownNavDomain(
                f"root_domain {root_domain!r} not registered")
        if not self._domains[root_domain].has_root:
            raise NavError(f"root_domain {root_domain!r} must has_root=True")
        focus_id = f"{page_id}::page_focus"
        self._domains[focus_id] = Domain(
            id=focus_id, has_root=True, is_page_focus=True,
        )
        state = PageState(page_id=page_id, stack=[
            DomainFrame(domain_id=focus_id, current=None),
            DomainFrame(domain_id=root_domain, current=None),
        ])
        self._pages[page_id] = state
        return state

    def pages(self) -> tuple[PageState, ...]:
        """所有已建立页面的状态，按 ``ensure_page`` 的创建顺序。

        只读快照：元素仍是内部的 :class:`PageState`（``stack`` 外部只读）。
        """
        return tuple(self._pages.values())

    def restore_page(self, page_id: str) -> PageState | None:
        return self._pages.get(page_id)

    def current_frame(self, page_id: str) -> DomainFrame | None:
        state = self._pages.get(page_id)
        return state.stack[-1] if state else None

    def snapshot(self, page_id: str) -> Snapshot:
        """页面栈的可比快照：``((domain_id, current), ...)``（自底向顶）。

        只读视图，用于两件事：判断动作有没有进展（:attr:`last_action_ok`），
        以及执行失败时回滚（:meth:`restore`）。
        """
        state = self._pages.get(page_id)
        if state is None:
            return ()
        return tuple((frame.domain_id, frame.current) for frame in state.stack)

    def restore(self, page_id: str, snapshot: Snapshot) -> bool:
        """把页面栈恢复成 :meth:`snapshot` 拿到的那一刻；返回是否恢复成功。

        分发器在「焦点执行失败」时用它回滚逻辑光标——决策已经动了状态（挪了
        ``current``、推了帧、弹了帧），但控件没接住焦点，两者必须一起退回去。

        恢复是**重建**而不是只截断：动作可能把帧弹掉了（Esc / 边界退出），
        快照里那几帧的数据还在，用 ``(domain_id, current)`` 原样补回来。所以
        唯一会拒绝的情况是快照里的某个域**已经被反注册**——那时补回去会造出
        指向不存在域的栈帧，宁可让光标留在新位置并返回 ``False``。
        """
        state = self._pages.get(page_id)
        if state is None:
            return False
        for domain_id, _ in snapshot:
            if domain_id not in self._domains:
                logger.debug(
                    "restore(%s) failed: domain %s no longer registered",
                    page_id, domain_id)
                return False
        while len(state.stack) > len(snapshot):
            state.stack.pop()
        while len(state.stack) < len(snapshot):
            domain_id, current = snapshot[len(state.stack)]
            state.stack.append(
                DomainFrame(domain_id=domain_id, current=current))
        for frame, (domain_id, current) in zip(state.stack, snapshot):
            frame.domain_id = domain_id
            frame.current = current
        return True

    # -- 挂起 / 恢复（模态） --------------------------------------------
    def suspend(self, page_id: str) -> None:
        """挂起页面的导航动作（可重入：与 :meth:`resume` 成对使用）。

        挂起期间 :meth:`move` / :meth:`enter` / :meth:`exit_back` /
        :meth:`esc` / :meth:`shift_esc` / :meth:`jump_to_page_root` /
        :meth:`tab_next` / :meth:`tab_prev` 直接返回 ``None``，**不改动任何
        状态**。用于模态对话框 / 窗口失活：那时按键属于模态控件，nav 不该
        在旁边偷偷挪光标。

        注册 / 查询 / :meth:`set_current` / :meth:`focus_node` 不受影响（它们
        不是「按键动作」，而是调用方的显式编排）。
        """
        self._suspended.add(page_id)
        logger.debug("nav page %s suspended", page_id)

    def resume(self, page_id: str) -> None:
        """解除 :meth:`suspend`（多余的 ``resume`` 是安全的空操作）。"""
        if page_id in self._suspended:
            self._suspended.discard(page_id)
            logger.debug("nav page %s resumed", page_id)

    def is_suspended(self, page_id: str) -> bool:
        """该页面当前是否被 :meth:`suspend` 挂起。"""
        return page_id in self._suspended

    # -- 动作 ----------------------------------------------------------
    def should_pass_through(
            self, page_id: str, key: Direction | str,
    ) -> bool:
        """当前节点是否让渡该按键给控件自治。

        ``key`` 接受 :class:`Direction` 枚举（自动取 ``.value``）或字符串
        （``NAV_UP`` / ``NAV_DOWN`` / ``NAV_LEFT`` / ``NAV_RIGHT`` /
        ``NAV_ENTER`` / ``NAV_ESC`` / ``NAV_TAB``）。

        调用方**必须先调用**此方法：命中则不调 :meth:`move` / :meth:`enter` /
        :meth:`esc` 等 nav 动作，让事件冒泡给控件（Qt 侧 ``event.ignore()``）。
        nav 的动作方法本身不检查让渡，这是调用方的显式契约（见
        ``albuswall.ui.nav`` 包文档「调用方契约」第 6 条）。

        判定顺序：

        1. 页面 / ``current`` 为 ``None`` → ``False``
        2. ``node.pass_through_keys is not None`` → 直接查集合（空集 = 不让渡）
        3. ``node.pass_through_keys is None`` 且 ``widget.keymap_passthrough``
           为真（Qt 动态属性或同名 Python 属性，见 :func:`_widget_flag`）→
           让渡全集 :data:`DEFAULT_PASS_THROUGH_KEYS`
        4. 否则 → ``False``
        """
        frame = self.current_frame(page_id)
        if frame is None or frame.current is None:
            return False
        node = self._nodes.get(frame.current)
        if node is None:
            return False
        key_str = key.value if isinstance(key, Direction) else key
        if node.pass_through_keys is not None:
            return key_str in node.pass_through_keys
        if self._widget_flag(node, "keymap_passthrough"):
            return key_str in DEFAULT_PASS_THROUGH_KEYS
        return False

    @_acting
    def move(self, page_id: str, direction: Direction) -> Node | None:
        """沿 ``direction`` 移动。

        1. ``direction_aliases`` 命中：
           ``"enter"`` → :meth:`enter`；``"next"`` / ``"prev"`` → 按
           ``nodes_in`` 注册顺序，跳过 ``focusable=False``。
           ``current=None`` 时 next 从第一个 focusable、prev 从最后一个开始。
        2. 否则网格扫描。``current=None`` 从该方向网格边缘找第一个
           focusable。
        3. ``current`` 是节点：``cur.grid is None`` 时先判
           ``exit_directions``（命中则 :meth:`exit_back`），否则返回
           ``None``；有 grid 时沿方向逐格扫描，到边界命中
           ``exit_directions`` → :meth:`exit_back`。
        4. 不跨域（除规则 3 的 ``exit_directions``）、不垂直回退。

        TODO(v2): rowspan / colspan 出口、跨域跳跃。
        """
        frame = self.current_frame(page_id)
        if frame is None:
            return None
        domain = self._domains[frame.domain_id]

        alias = domain.direction_aliases.get(direction)
        if alias == "enter":
            return self.enter(page_id, how="enter")
        if alias in ("next", "prev"):
            return self._sequential(domain, frame, alias)

        return self._grid_scan(page_id, domain, frame, direction)

    # -- 域内 Tab 语义 --------------------------------------------------
    @_acting
    def tab_next(self, page_id: str) -> Node | None:
        """域内 Tab：在当前域里按顺序前进一个落点；返回落点或 ``None``。

        顺序来源：``Domain.tab_order`` 给出时严格按它，否则按
        :class:`~albuswall.ui.nav.model.GridPlacement` 的 ``(row, col)`` 排序
        （无 grid 的节点排在后面，彼此按注册顺序）。``focusable=False`` 与当前
        不可见的节点一律跳过。

        - ``current is None`` → 落域入口，等价于 ``enter(how="tab")``；
        - ``current`` 不在可落点列表里（停在隐藏节点上）→ 落到该顺序的第一个
          可见节点（不 ``None``，否则一次隐藏就把 Tab 卡死）；
        - 已经是最后一个可见节点 → 返回 ``None``。

        **v1 不跨域**：末尾返回 ``None`` 之后要不要跨到别的区域由分发器决定
        （当前实现是不跨，把按键冒泡出去 / 由调用方另行编排）。进 / 出一个域
        仍然走 :meth:`enter` / :meth:`exit_back`。
        """
        return self._tab_step(page_id, forward=True)

    @_acting
    def tab_prev(self, page_id: str) -> Node | None:
        """域内 Shift+Tab：:meth:`tab_next` 的反方向（同样 v1 不跨域）。

        已经在第一个可见落点上时返回 ``None``，由分发器决定是冒泡还是
        :meth:`exit_back`。
        """
        return self._tab_step(page_id, forward=False)

    def _tab_step(self, page_id: str, *, forward: bool) -> Node | None:
        frame = self.current_frame(page_id)
        if frame is None:
            return None
        domain = self._domains[frame.domain_id]
        if frame.current is None:
            # 虚拟根 / 域首：落域入口（与 enter(how="tab") 同一套三级 fallback）
            return self.enter(page_id, how="tab")

        order = [node.id for node in self._tab_order(domain)]
        if not order:
            return None
        if frame.current not in order:
            target = order[0] if forward else order[-1]
            frame.current = target
            return self._nodes[target]

        idx = order.index(frame.current)
        nxt = idx + 1 if forward else idx - 1
        if not 0 <= nxt < len(order):
            return None
        frame.current = order[nxt]
        return self._nodes[order[nxt]]

    def _tab_order(self, domain: Domain) -> list[Node]:
        """域内 Tab 顺序：``tab_order`` 优先，否则按网格坐标排序。"""
        registered = [n for n in domain.nodes.values()
                      if n.focusable and self._is_visible(n)]
        if domain.tab_order is not None:
            by_id = {n.id: n for n in registered}
            return [by_id[node_id] for node_id in domain.tab_order
                    if node_id in by_id]
        position = {node_id: i for i, node_id in enumerate(domain.nodes)}
        return sorted(
            registered,
            key=lambda n: (
                (0, n.grid.row, n.grid.col, position[n.id])
                if n.grid is not None
                else (1, 0, 0, position[n.id])
            ),
        )

    # -- 内部判定 ------------------------------------------------------
    @staticmethod
    def _widget_flag(node: Node, name: str) -> bool:
        """读 widget 上的布尔标志。

        优先级：

        1. Qt 动态属性（``widget.property(name)``）——PySide6 的 ``setProperty``
           不生成同名 Python 属性，必须走 ``property()`` 读；
        2. Python 静态属性（``getattr``）——兼容非 QObject widget 或类属性声明。

        只接受 ``is True``。字符串 ``"true"`` / ``1`` 等不算（防
        ``setProperty("x", "yes")`` 误判）。非 QObject widget 没有 ``property``
        方法时跳过动态属性检查，退到 ``getattr``。任何异常吞掉返回 ``False``
        （``visible_fn`` 之外的第二道防御）。
        """
        widget = node.widget
        prop = getattr(widget, "property", None)
        if prop is not None:
            try:
                if prop(name) is True:
                    return True
            except Exception:  # noqa: BLE001 - 非 Qt 的 property() 语义不同
                logger.debug(
                    "widget.property(%r) raised for node %s; falling back",
                    name, node.id,
                )
        try:
            return getattr(widget, name) is True
        except Exception:  # noqa: BLE001 - 属性访问本身抛异常也只是「没有」
            return False

    def is_visible(self, node: Node) -> bool:
        """该节点当前是否可落点（``visible_fn`` 的公开读法）。

        落点判定内部用的就是它；调用方（如键盘分发器）需要「这个节点现在能不能
        落上去」时也走这里，避免两边各写一份 ``visible_fn`` 语义。
        """
        return self._is_visible(node)

    def _is_visible(self, node: Node) -> bool:
        """``visible_fn`` 为 ``None`` → ``True``；返回 ``False`` 或抛异常 → ``False``。"""
        if node.visible_fn is None:
            return True
        try:
            return bool(node.visible_fn())
        except Exception:
            logger.warning(
                "visible_fn raised for node %s; treating as hidden",
                node.id, exc_info=True,
            )
            return False

    def _sequential(
            self, domain: Domain, frame: DomainFrame, direction: str,
    ) -> Node | None:
        """按注册顺序做 next / prev（``direction_aliases`` 的线性语义）。

        跳过 ``focusable=False`` 与当前不可见的节点。``current`` 不在可见
        focusable 列表里（停在隐藏节点 / 寄存了悬空 id）时**不返回 ``None``**：
        退回注册顺序上「下一个 / 上一个大于当前位置的可见 focusable」，都没有
        就取列表首 / 尾。否则一个按钮临时 disabled 就能把光标锁死在原地。
        """
        focusable = [n for n in domain.nodes.values()
                     if n.focusable and self._is_visible(n)]
        if not focusable:
            return None
        ids = [n.id for n in focusable]
        if frame.current is None:
            target = ids[0] if direction == "next" else ids[-1]
            frame.current = target
            return self._nodes[target]
        if frame.current in ids:
            idx = ids.index(frame.current)
            nxt = idx + 1 if direction == "next" else idx - 1
            if not 0 <= nxt < len(ids):
                return None
            frame.current = ids[nxt]
            return self._nodes[ids[nxt]]

        # 回退：按**注册顺序**（``domain.nodes`` 的插入顺序）定位当前位置。
        order = {node_id: i for i, node_id in enumerate(domain.nodes)}
        pos = order.get(frame.current, -1 if direction == "next"
                        else len(order))
        if direction == "next":
            after = [nid for nid in ids if order.get(nid, -1) > pos]
            target = after[0] if after else ids[0]
        else:
            before = [nid for nid in ids if order.get(nid, -1) < pos]
            target = before[-1] if before else ids[-1]
        frame.current = target
        return self._nodes[target]

    def _grid_scan(
            self, page_id: str, domain: Domain,
            frame: DomainFrame, direction: Direction,
    ) -> Node | None:
        candidates = [n for n in domain.nodes.values()
                      if n.focusable and n.grid is not None
                      and self._is_visible(n)]

        if frame.current is not None:
            cur = self._nodes.get(frame.current)
            if cur is None:
                return None
            if cur.grid is None:
                # 无 grid 节点：无法网格扫描，但可声明方向退出
                if direction in domain.exit_directions:
                    return self.exit_back(page_id)
                return None
            candidates = [n for n in candidates if n.id != cur.id]
            target = self._scan_from(cur.grid, candidates, direction)
        else:
            target = self._scan_from_edge(candidates, direction)

        if target is not None:
            frame.current = target.id
            return target
        if direction in domain.exit_directions:
            return self.exit_back(page_id)
        return None

    @staticmethod
    def _scan_from(cur_grid, candidates, direction):
        best = None
        for n in candidates:
            g = n.grid
            if direction is Direction.DOWN \
                    and g.col == cur_grid.col and g.row > cur_grid.row:
                if best is None or g.row < best.grid.row:
                    best = n
            elif direction is Direction.UP \
                    and g.col == cur_grid.col and g.row < cur_grid.row:
                if best is None or g.row > best.grid.row:
                    best = n
            elif direction is Direction.RIGHT \
                    and g.row == cur_grid.row and g.col > cur_grid.col:
                if best is None or g.col < best.grid.col:
                    best = n
            elif direction is Direction.LEFT \
                    and g.row == cur_grid.row and g.col < cur_grid.col:
                if best is None or g.col > best.grid.col:
                    best = n
        return best

    @staticmethod
    def _scan_from_edge(candidates, direction):
        if not candidates:
            return None
        if direction is Direction.DOWN:
            m = min(n.grid.row for n in candidates)
        elif direction is Direction.UP:
            m = max(n.grid.row for n in candidates)
        elif direction is Direction.RIGHT:
            m = min(n.grid.col for n in candidates)
        else:
            m = max(n.grid.col for n in candidates)
        edge = [n for n in candidates
                if (n.grid.row if direction in (Direction.UP, Direction.DOWN)
                    else n.grid.col) == m]
        return edge[0] if edge else None

    @_acting
    def enter(
            self, page_id: str, *, how: Literal["tab", "enter"],
    ) -> Node | None:
        """进入子域或触发动作。

        A. ``current`` 是节点且有 ``child_domain``：``how="enter"`` 且子域
           ``has_root=True`` → 推入，``current=None``；其余 → 推入，落子域
           entry / fallback / 容器踏板。父帧 ``current`` 不更新。
        B. ``current`` 是节点且无 ``child_domain`` 且 ``how="enter"``：有
           ``action`` 则调用并返回 ``node``，否则返回 ``None``。
        C. ``current=None``：``how="enter"`` 且 ``has_root`` 且
           ``root_action`` → 调用，返回 ``None``；否则（``how="tab"`` 或
           ``has_root=False`` 或无 ``root_action``）→ 落点逻辑：
           :meth:`_resolve_entry_target` 三级 fallback（entry → 第一个
           focusable → 第一个节点）；落点 ``focusable=True`` → 落点并返回
           node；落点 ``focusable=False`` 且有 ``child_domain`` → 容器踏板；
           落点 ``focusable=False`` 无 ``child_domain`` → 返回 ``None``。
        D. 无可用落点返回 ``None``。

        ``current`` 指向的节点当前不可见（``visible_fn`` 为假）时，A / B 两条
        路径都**不执行**：隐藏的按钮不该被回车点下去，隐藏的容器也不该被 Tab
        推进去。此时返回 ``None``（无操作）。

        TODO(v2): 域栈快照深拷贝、节点销毁 fallback。
        """
        frame = self.current_frame(page_id)
        if frame is None:
            return None
        domain = self._domains[frame.domain_id]

        # A / B
        if frame.current is not None:
            node = self._nodes.get(frame.current)
            if node is None:
                return None
            if not self._is_visible(node):
                # 光标停在一个已经不可见的节点上（HUD 收起、页面被关掉）：
                # 它的 action / 子域都够不到，不能「按 Enter 点一个看不见的
                # 按钮」。不落点、不推栈，直接当无操作。
                logger.debug(
                    "enter(%s) on hidden node %s; no-op",
                    how, frame.current)
                return None
            if node.child_domain is not None:
                child = self._domains[node.child_domain]
                if how == "enter" and child.has_root:
                    return self._push_child(page_id, child, target=None)
                target = self._resolve_entry_target(child)
                return self._push_child(page_id, child, target=target)
            if how == "enter" and node.action is not None:
                node.action()
                return node
            return None

        # C
        if how == "enter" and domain.has_root \
                and domain.root_action is not None:
            domain.root_action()
            return None

        target = self._resolve_entry_target(domain)
        if target is None:
            return None
        node = domain.nodes[target]
        if node.focusable:
            frame.current = target
            return node
        if node.child_domain is not None:
            child = self._domains[node.child_domain]
            return self._push_child(
                page_id, child, target=self._resolve_entry_target(child))
        logger.warning(
            "domain %s: entry %s is focusable=False without child_domain",
            domain.id, target,
        )
        return None

    def _resolve_entry_target(self, domain: Domain) -> str | None:
        """三级 fallback：entry → 第一个可见 focusable → 第一个可见节点。"""
        if domain.entry is not None and domain.entry in domain.nodes \
                and self._is_visible(domain.nodes[domain.entry]):
            return domain.entry
        for n in domain.nodes.values():
            if n.focusable and self._is_visible(n):
                return n.id
        for n in domain.nodes.values():
            if self._is_visible(n):
                return n.id
        return None

    def _push_child(
            self, page_id: str, child: Domain, *,
            target: str | None, _depth: int = 0,
    ) -> Node | None:
        """推入子域。``target=None`` 表示落虚拟根（要求 ``child.has_root``）。

        ``target`` 指定的节点若是容器（``focusable=False`` +
        ``child_domain``），递归踏板下钻，限深 :data:`_MAX_PEDAL_DEPTH`。
        ``target`` 指定的节点若是 ``focusable=False`` 且无
        ``child_domain``，视为不可落点，返回 ``None``。

        任何非落点分支都在**推帧之前**返回，因此踏板失败不会留下半截子栈帧。
        """
        if _depth >= _MAX_PEDAL_DEPTH:
            logger.warning(
                "container pedal depth exceeded at domain %s", child.id,
            )
            return None
        if target is None:
            if not child.has_root:
                logger.warning(
                    "pushing has_root=False domain %s with target=None; "
                    "falling back to entry resolution", child.id,
                )
                target = self._resolve_entry_target(child)
                if target is None:
                    return None
                return self._push_child(
                    page_id, child, target=target, _depth=_depth)
            self._pages[page_id].stack.append(
                DomainFrame(domain_id=child.id, current=None))
            return None
        node = child.nodes.get(target)
        if node is None:
            logger.warning(
                "target node %s not in domain %s", target, child.id,
            )
            return None
        if not self._is_visible(node):
            logger.debug("target node %s is hidden; not landing", target)
            return None
        if not node.focusable:
            if node.child_domain is not None:
                grand = self._domains[node.child_domain]
                grand_target = self._resolve_entry_target(grand)
                return self._push_child(
                    page_id, grand, target=grand_target, _depth=_depth + 1)
            logger.warning(
                "node %s is focusable=False without child_domain; "
                "cannot land", target,
            )
            return None
        self._pages[page_id].stack.append(
            DomainFrame(domain_id=child.id, current=target))
        return node

    @_acting
    def exit_back(self, page_id: str) -> Node | None:
        """Shift+Tab。栈深 ≤ 1 或当前域 ``allow_exit=False`` → ``None``。

        否则弹栈，返回父帧 ``current`` 对应 :class:`Node`（``None`` = 父域
        虚拟根）。
        """
        state = self._pages.get(page_id)
        if state is None or len(state.stack) <= 1:
            return None
        if not self._domains[state.stack[-1].domain_id].allow_exit:
            return None
        state.stack.pop()
        parent = state.stack[-1]
        return self._nodes.get(parent.current) if parent.current else None

    @_acting
    def esc(self, page_id: str) -> Node | None:
        """Esc。

        - 当前域 ``esc_clears_current=True`` 且 ``current is not None``：
          ``current=None``，返回 ``None``。
        - 否则无条件弹一层（忽略 ``allow_exit``）。
        - 栈深 ≤ 1 返回 ``None``（关页由调用方判定）。
        """
        state = self._pages.get(page_id)
        if state is None or len(state.stack) <= 1:
            return None
        cur = state.stack[-1]
        if self._domains[cur.domain_id].esc_clears_current \
                and cur.current is not None:
            cur.current = None
            return None
        state.stack.pop()
        parent = state.stack[-1]
        return self._nodes.get(parent.current) if parent.current else None

    @_acting
    def shift_esc(self, page_id: str) -> Node | None:
        """Shift+Esc。反复弹栈直到 ``is_real_page`` / ``is_page_focus`` /
        栈深 1。

        停在该帧，返回 ``current`` 对应 :class:`Node`（``None`` = 该域虚拟
        根）。不执行关页。
        """
        state = self._pages.get(page_id)
        if state is None:
            return None
        while len(state.stack) > 1:
            cur = state.stack[-1]
            d = self._domains[cur.domain_id]
            if d.is_real_page or d.is_page_focus:
                break
            state.stack.pop()
        frame = state.stack[-1]
        return self._nodes.get(frame.current) if frame.current else None

    @_acting
    def jump_to_page_root(self, page_id: str) -> None:
        """连续弹栈到 ``is_real_page`` / ``is_page_focus`` / 栈深 1，
        ``current`` 强制置 ``None``。

        v1 恒返回 ``None``；调用方通过 :meth:`current_frame` 查询。

        TODO(v2): 键位未定；v1 只提供方法不绑定快捷键。
        """
        state = self._pages.get(page_id)
        if state is None:
            return
        while len(state.stack) > 1:
            cur = state.stack[-1]
            d = self._domains[cur.domain_id]
            if d.is_real_page or d.is_page_focus:
                break
            state.stack.pop()
        state.stack[-1].current = None

    def switch_page(self, page_id: str) -> tuple[str, str]:
        """在页面焦点态下切相邻页。v1 返回 ``(page_id, page_id)``，不实际切换。

        TODO(v2): 页面列表来源未定。
        """
        return (page_id, page_id)

    def set_current(self, page_id: str, node_id: str | None) -> None:
        """外部强制设置光标。``node_id=None`` 表示虚拟根。

        页面未创建 → :class:`UnknownNavPage`；``node_id`` 未注册或不在当前帧
        的域里 → :class:`UnknownNavNode`。
        """
        state = self._pages.get(page_id)
        if state is None:
            raise UnknownNavPage(f"page {page_id!r} not created")
        if node_id is None:
            state.stack[-1].current = None
            return
        if node_id not in self._nodes:
            raise UnknownNavNode(f"node {node_id!r} not registered")
        if node_id not in self._domains[state.stack[-1].domain_id].nodes:
            raise UnknownNavNode(
                f"node {node_id!r} not in domain "
                f"{state.stack[-1].domain_id!r}")
        if not self._is_visible(self._nodes[node_id]):
            logger.warning(
                "set_current on hidden node %s; external override allowed",
                node_id,
            )
        state.stack[-1].current = node_id

    # -- 生命周期 / 焦点 ----------------------------------------------
    def clear(self) -> None:
        """清空域 / 节点 / 页面状态，并解除所有挂起。

        执行后端**不**重置：它是运行期注入的（重新 wiring 之后仍然是同一个
        Qt 后端）。需要回到默认后端时显式 ``set_backend(None)``。
        """
        self._domains.clear()
        self._nodes.clear()
        self._pages.clear()
        self._suspended.clear()
        self._last_action_ok = False

    def focus_node(self, node: Node) -> bool:
        """执行焦点并**验证**结果；返回焦点是否真的落到了这个节点上。

        执行路径有两条，验证策略不同：

        1. ``node.focus_fn is not None`` —— 调用方自定义的执行（复合控件指到
           内部输入框、需要 ``scrollTo`` 后聚焦……）。**视为成功**，不验证：
           真实落点可能是 ``widget`` 的后代，甚至根本不是它的后代，nav 猜不
           出来（见包文档契约 4）。
        2. 否则走 ``backend.apply_focus(node)``，然后读
           ``backend.current_focus_chain()`` 确认 ``node.widget`` 在链上。
           链为空 = 这台环境观测不到焦点（无 WM / offscreen），**按成功处理**
           ——观测不到不等于失败，否则无头环境里整条导航都会退化成回滚。

        验证失败时返回 ``False`` 并 ``warning``（这是真正的执行事故）。调用方
        （分发器）负责把逻辑光标回滚到动作前，见 :meth:`snapshot` /
        :meth:`restore`。

        nav 层不知道 ``widget`` 是什么类型：聚焦失败由后端 / 控件抛出的异常
        直接向上冒泡，本方法不吞异常。
        """
        self._backend.ensure_focusable(node)
        if node.focus_fn is not None:
            node.focus_fn()
            return True
        self._backend.apply_focus(node)
        chain = self._backend.current_focus_chain()
        if not chain:
            logger.debug(
                "focus_node %s: focus chain unobservable; assuming success",
                node.id)
            return True
        if any(item is node.widget for item in chain):
            return True
        logger.warning(
            "focus_node %s failed; focus chain=%r", node.id, chain[:1])
        return False


# -- 模块级单例（唯一） -----------------------------------------------
_default = NavRegistry()


def get_default_registry() -> NavRegistry:
    return _default


def set_default_backend(backend: FocusBackend | None) -> None:
    """给进程级默认注册表注入焦点执行后端（``None`` → 恢复默认后端）。

    界面 boot 之后由分发器调用：那时才有 Qt 侧的实现（
    :class:`~albuswall.ui.nav.qt.QtFocusBackend`）可注入。返回 ``None`` 的语义
    是「退回到 :class:`~albuswall.ui.nav.backend.NullFocusBackend`」，不是
    「什么都不做」——无头 / 测试环境里必须能把注入撤回来。
    """
    _default.set_backend(backend)


def registry_nav_registry(container: Any) -> None:
    """把进程级导航注册表登记进依赖容器（与 ``registry_*`` 系列同风格）。

    只登记「容器里有 nav_registry」，**不**建页面、不加载声明：注册时机属于
    界面启动流程，由调用方显式 ``ensure_page()`` / ``register_from_slot()``
    决定。
    """
    register = getattr(container, "register", None)
    if register is None:
        logger.warning(
            "container has no .register(); nav registry not bound")
        return
    register("nav_registry", get_default_registry, returns=NavRegistry)


__all__ = [
    "NavRegistry",
    "get_default_registry",
    "registry_nav_registry",
    "set_default_backend",
]
