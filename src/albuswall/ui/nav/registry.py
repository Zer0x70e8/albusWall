#
"""``albuswall.ui.nav`` 的注册表：注册（域 / 节点）+ 决策（返回落点）。

本模块是纯决策层：不 import Qt、不装事件过滤器、不接 ``QShortcut``、不主动
``setFocus``。所有「查到哪个节点」的答案都以返回值 / ``PageState`` 的形式交给
调用方，焦点执行通过 :meth:`NavRegistry.focus_node` 由调用方触发。

动作返回 ``None`` 的多种含义，调用方需结合 ``stack`` / ``current`` 区分：

- ``move``：未移动（边界 + 不在 ``exit_directions``），或无 grid 节点且方向
  未声明退出；
- ``enter``：落到虚拟根（``current=None``），或触发 ``root_action`` /
  ``node.action`` 后返回；
- ``exit_back``：栈深 ≤ 1，或 ``allow_exit=False``；
- ``esc``：``esc_clears_current`` 命中（取消选中），或栈深 ≤ 1；
- ``shift_esc`` / ``jump_to_page_root``：停在该域虚拟根或栈深 1。

v2 待办（本次不实现，代码里不留 stub）：

- 叶域类型 ``Domain.is_leaf``：把「叶域」约定升级为一等类型（见
  ``__init__.py`` 定位节）；
- 动态网格提供者：列数运行期变化时用回调代替静态 ``GridPlacement``；
- 页面栈管理层：从 ``WindowPresenter`` 手写 ``setFocus`` 兜底中抽象；
- 页面↔域声明绑定：``PAGE_ROOTS = {page_id: root_domain}`` 显式声明表；
- ``unregister_*`` 级联：换图 / 换模型时清理悬空 ``current``；
- 模态 suspend / resume：可选 API，v1 由调用方契约 1 承担；
- 网格 span 出口、网格碰撞检测、跨域跳跃。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Literal

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
    """导航注册表。单一实例用 :func:`get_default_registry`。"""

    def __init__(self) -> None:
        self._domains: dict[str, Domain] = {}
        self._nodes: dict[str, Node] = {}
        self._pages: dict[str, PageState] = {}
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
        self._domains.clear()
        self._nodes.clear()
        self._pages.clear()

    def focus_node(self, node: Node) -> None:
        """执行焦点：``focus_fn`` 优先，否则 ``widget.setFocus()``。

        v1 不做类型检查（nav 层不知道 widget 是什么）。
        """
        if node.focus_fn is not None:
            node.focus_fn()
            return
        node.widget.setFocus()


# -- 模块级单例（唯一） -----------------------------------------------
_default = NavRegistry()


def get_default_registry() -> NavRegistry:
    return _default


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


__all__ = ["NavRegistry", "get_default_registry", "registry_nav_registry"]
