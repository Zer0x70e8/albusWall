#
"""``NavRegistry`` 的动作语义回归（move / enter / exit_back / esc / 踏板）。

守六条语义规则：

1. 虚拟根 Tab 走三级 fallback，容器可踏板下钻；
2. 踏板多路径（A 路径 = 从节点进子域；C 路径 = 从虚拟根进域）；
3. 无 grid 节点的域可声明 ``exit_directions`` 弹栈；
4. ``enter`` 的分支 C（虚拟根 / root_action）；
5. next / prev 序列的起点（``current=None`` 从两端进）；
6. ``entry`` 优先于 ``focusable`` 判定。
"""

from __future__ import annotations

import logging

import pytest

from albuswall.ui.nav.errors import (
    NavError,
    UnknownNavNode,
    UnknownNavPage,
)
from albuswall.ui.nav.model import (
    DEFAULT_PASS_THROUGH_KEYS,
    NAV_DOWN,
    NAV_LEFT,
    NAV_RIGHT,
    NAV_UP,
    Direction,
    Domain,
    DomainFrame,
    GridPlacement,
    Node,
)
from albuswall.ui.nav.registry import NavRegistry


class FakeWidget:
    def __init__(self):
        self.focused = False

    def setFocus(self):
        self.focused = True


@pytest.fixture
def reg():
    """场景：page → list_view → form。page 不设 entry，依赖 fallback。"""
    r = NavRegistry()
    r.register_domain(Domain(id="page", has_root=True, is_real_page=True))
    r.register_domain(Domain(
        id="list_view", has_root=False, allow_exit=False,
        entry="lv.item_a",
        direction_aliases={Direction.RIGHT: "enter"},
    ))
    r.register_domain(Domain(id="form", has_root=True, allow_exit=False))
    r.register_node(Node(id="list_view_container", widget=object(),
                         child_domain="list_view", focusable=False),
                    domain_id="page")
    r.register_node(Node(id="lv.item_a", widget=object(),
                         child_domain="form"),
                    domain_id="list_view")
    r.register_node(Node(id="lv.item_b", widget=object()),
                    domain_id="list_view")
    r.register_node(Node(id="form.name", widget=object()), domain_id="form")
    return r


# --- 页面状态 ---

def test_ensure_page_stack_shape(reg):
    state = reg.ensure_page("p1", root_domain="page")
    assert len(state.stack) == 2
    assert state.stack[0].domain_id.endswith("::page_focus")
    assert reg.get_domain(state.stack[0].domain_id).is_page_focus is True
    assert state.stack[1].domain_id == "page"
    assert state.stack[0].current is None
    assert state.stack[1].current is None


def test_ensure_page_idempotent(reg):
    s1 = reg.ensure_page("p1", root_domain="page")
    s2 = reg.ensure_page("p1", root_domain="page")
    assert s1 is s2


def test_ensure_page_rootless_root_raises(reg):
    reg.register_domain(Domain(id="rootless", has_root=False, entry="x"))
    with pytest.raises(NavError):
        reg.ensure_page("p1", root_domain="rootless")


def test_restore_page_not_created(reg):
    assert reg.restore_page("nope") is None


# --- Tab / Enter ---

def test_tab_from_virtual_root_uses_fallback_pedal(reg):
    """page 无 entry，Tab 从虚拟根进入：
    fallback → list_view_container（容器）→ 踏板推入 list_view → 落 lv.item_a。
    父帧 page 的 current 保持 None。"""
    reg.ensure_page("p1", root_domain="page")
    assert reg.current_frame("p1").current is None
    node = reg.enter("p1", how="tab")
    assert node is not None
    assert node.id == "lv.item_a"
    state = reg.restore_page("p1")
    assert state.stack[-1].domain_id == "list_view"
    assert state.stack[-1].current == "lv.item_a"
    assert state.stack[-2].current is None  # 父帧未更新


def test_tab_from_virtual_root_honors_explicit_entry(reg):
    """显式设置 page.entry = list_view_container 后 Tab 依旧通过容器踏板。"""
    reg.get_domain("page").entry = "list_view_container"
    reg.ensure_page("p1", root_domain="page")
    node = reg.enter("p1", how="tab")
    assert node.id == "lv.item_a"
    assert reg.current_frame("p1").domain_id == "list_view"


def test_tab_from_node_child_domain_rootless(reg):
    """A 路径：从节点进入无根子域（how=tab），落点 entry。"""
    reg.ensure_page("p1", root_domain="page")
    reg.set_current("p1", "list_view_container")  # 手动定位
    node = reg.enter("p1", how="tab")
    assert node.id == "lv.item_a"
    assert reg.current_frame("p1").domain_id == "list_view"
    assert reg.current_frame("p1").current == "lv.item_a"


def test_enter_child_domain_with_root_lands_virtual_root(reg):
    """A 路径：lv.item_a.child_domain=form, form.has_root=True → 落虚拟根。"""
    reg.ensure_page("p1", root_domain="page")
    reg.set_current("p1", "list_view_container")
    reg.enter("p1", how="tab")  # → list_view(lv.item_a)
    assert reg.current_frame("p1").current == "lv.item_a"

    result = reg.enter("p1", how="enter")
    assert result is None
    state = reg.restore_page("p1")
    assert state.stack[-1].domain_id == "form"
    assert state.stack[-1].current is None
    # 父帧 list_view 的 current 未变
    assert state.stack[-2].current == "lv.item_a"


def test_enter_node_action_called():
    reg = NavRegistry()
    called = []
    reg.register_domain(Domain(id="d", has_root=True, is_real_page=True))
    reg.register_node(
        Node(id="n", widget=object(), action=lambda: called.append(1)),
        domain_id="d",
    )
    reg.ensure_page("p1", root_domain="d")
    reg.set_current("p1", "n")
    result = reg.enter("p1", how="enter")
    assert result.id == "n"
    assert called == [1]


def test_enter_node_no_action_returns_none():
    reg = NavRegistry()
    reg.register_domain(Domain(id="d", has_root=True, is_real_page=True))
    reg.register_node(Node(id="n", widget=object()), domain_id="d")
    reg.ensure_page("p1", root_domain="d")
    reg.set_current("p1", "n")
    assert reg.enter("p1", how="enter") is None


def test_enter_root_action_called():
    reg = NavRegistry()
    called = []
    reg.register_domain(Domain(
        id="d", has_root=True, is_real_page=True,
        root_action=lambda: called.append(1),
    ))
    reg.ensure_page("p1", root_domain="d")
    assert reg.enter("p1", how="enter") is None
    assert called == [1]


# --- A2：隐藏节点上的 enter 不执行 ---

def _hidden_action_rig(*, child_domain: str | None = None):
    """一个 current 停在「不可见节点」上的注册表。"""
    reg = NavRegistry()
    calls: list[str] = []
    reg.register_domain(Domain(id="d", has_root=True, is_real_page=True))
    reg.register_domain(Domain(id="child", has_root=True, entry="kid"))
    reg.register_node(Node(id="kid", widget=object()), domain_id="child")
    reg.register_node(
        Node(id="ghost", widget=object(), action=lambda: calls.append("click"),
             child_domain=child_domain,
             visible_fn=lambda: False),
        domain_id="d",
    )
    reg.ensure_page("p1", root_domain="d")
    reg.set_current("p1", "ghost")          # 外部强制设置（仅 warning）
    return reg, calls


def test_enter_hidden_node_does_not_fire_action():
    """A2：HUD 收起后 ``detail.close`` 之类的节点按回车不该被点下去。"""
    reg, calls = _hidden_action_rig()
    assert reg.enter("p1", how="enter") is None
    assert calls == []
    assert reg.current_frame("p1").current == "ghost"


def test_enter_hidden_container_does_not_push():
    """A2 的容器分支：隐藏的入口不该被 Tab 推进去。"""
    reg, _ = _hidden_action_rig(child_domain="child")
    stack_before = len(reg.restore_page("p1").stack)
    assert reg.enter("p1", how="tab") is None
    assert len(reg.restore_page("p1").stack) == stack_before


# --- A3：current 不在 focusable 列表时的 next / prev 回退 ---

def _sequential_rig(visible: dict[str, bool]):
    reg = NavRegistry()
    reg.register_domain(Domain(id="page", has_root=True, is_real_page=True))
    reg.register_domain(Domain(
        id="nav", has_root=False, entry="a",
        direction_aliases={Direction.RIGHT: "next", Direction.LEFT: "prev"},
    ))
    reg.register_node(Node(id="page.c", widget=object(),
                           child_domain="nav"), domain_id="page")
    for node_id, shown in visible.items():
        reg.register_node(
            Node(id=node_id, widget=object(),
                 visible_fn=(None if shown else (lambda: False))),
            domain_id="nav",
        )
    reg.ensure_page("p1", root_domain="page")
    return reg


def test_sequential_falls_back_when_current_hidden():
    """A3：current 停在隐藏按钮上时，RIGHT 落到相邻的可见按钮。"""
    reg = _sequential_rig({"a": True, "b": False, "c": True})
    reg.restore_page("p1").stack.append(
        DomainFrame(domain_id="nav", current="b"))

    node = reg.move("p1", Direction.RIGHT)
    assert node is not None and node.id == "c"
    assert reg.current_frame("p1").current == "c"

    # 再回退到 b 上，LEFT 落到 a
    reg.current_frame("p1").current = "b"
    node = reg.move("p1", Direction.LEFT)
    assert node is not None and node.id == "a"


def test_sequential_fallback_wraps_to_first_and_last():
    """A3：前后都没有可见落点时取列表首 / 尾（不返回 None）。"""
    reg = _sequential_rig({"a": True, "b": False})
    reg.restore_page("p1").stack.append(
        DomainFrame(domain_id="nav", current="b"))

    node = reg.move("p1", Direction.RIGHT)      # b 之后没有可见节点 → 首 a
    assert node is not None and node.id == "a"

    reg.current_frame("p1").current = "b"
    node = reg.move("p1", Direction.LEFT)       # b 之前只有 a
    assert node is not None and node.id == "a"


# --- exit_back / esc / shift_esc ---

def test_exit_back_allow_exit_false(reg):
    reg.ensure_page("p1", root_domain="page")
    reg.set_current("p1", "list_view_container")
    reg.enter("p1", how="tab")          # → list_view(lv.item_a)
    reg.enter("p1", how="enter")        # → form(None)
    reg.set_current("p1", "form.name")
    depth_before = len(reg.restore_page("p1").stack)
    assert reg.exit_back("p1") is None
    assert len(reg.restore_page("p1").stack) == depth_before


def test_esc_ignores_allow_exit_false(reg):
    reg.ensure_page("p1", root_domain="page")
    reg.set_current("p1", "list_view_container")
    reg.enter("p1", how="tab")          # → list_view(lv.item_a)
    reg.enter("p1", how="enter")        # → form(None)
    reg.set_current("p1", "form.name")
    # form.allow_exit=False，但 esc 忽略
    result = reg.esc("p1")
    assert result is not None
    assert result.id == "lv.item_a"
    assert reg.current_frame("p1").domain_id == "list_view"


def test_esc_clears_current_first():
    reg = NavRegistry()
    reg.register_domain(Domain(
        id="d", has_root=True, is_real_page=True, esc_clears_current=True,
    ))
    reg.register_node(Node(id="n", widget=object()), domain_id="d")
    reg.ensure_page("p1", root_domain="d")
    reg.set_current("p1", "n")
    depth_before = len(reg.restore_page("p1").stack)

    result = reg.esc("p1")
    assert result is None
    assert reg.current_frame("p1").current is None
    assert len(reg.restore_page("p1").stack) == depth_before


def test_shift_esc_stops_at_page_focus():
    """栈里没有任何 ``is_real_page`` 域时，shift_esc 一路弹到 page_focus。"""
    reg = NavRegistry()
    reg.register_domain(Domain(id="root", has_root=True))
    reg.register_domain(Domain(id="sub", has_root=True))
    reg.register_node(Node(id="root.n", widget=object(),
                           child_domain="sub"), domain_id="root")
    reg.register_node(Node(id="sub.n", widget=object()), domain_id="sub")
    reg.ensure_page("p1", root_domain="root")
    reg.set_current("p1", "root.n")
    reg.enter("p1", how="enter")   # → sub(None)
    reg.set_current("p1", "sub.n")

    result = reg.shift_esc("p1")
    assert result is None  # page_focus 虚拟根
    state = reg.restore_page("p1")
    assert len(state.stack) == 1
    assert state.stack[0].domain_id.endswith("::page_focus")


def test_shift_esc_stops_at_real_page():
    reg = NavRegistry()
    reg.register_domain(Domain(id="page", has_root=True, is_real_page=True))
    reg.register_domain(Domain(id="sub", has_root=True))
    reg.register_node(Node(id="page.n", widget=object(),
                           child_domain="sub"), domain_id="page")
    reg.register_node(Node(id="sub.n", widget=object()), domain_id="sub")
    reg.ensure_page("p1", root_domain="page")
    reg.set_current("p1", "page.n")
    reg.enter("p1", how="enter")  # → sub(None)
    reg.set_current("p1", "sub.n")
    # shift_esc 弹到 page（is_real_page=True）停，current=page.n
    result = reg.shift_esc("p1")
    assert result is not None
    assert result.id == "page.n"
    assert reg.current_frame("p1").domain_id == "page"


def test_jump_to_page_root_clears_current(reg):
    reg.ensure_page("p1", root_domain="page")
    reg.set_current("p1", "list_view_container")
    reg.enter("p1", how="tab")
    reg.enter("p1", how="enter")
    reg.set_current("p1", "form.name")

    assert reg.jump_to_page_root("p1") is None
    state = reg.restore_page("p1")
    assert len(state.stack) == 2
    assert state.stack[-1].domain_id == "page"
    assert state.stack[-1].current is None


# --- move ---

def _setup_navbar(reg, *, host="page"):
    """装一个 navbar 域，并把进入它的容器节点挂到 ``host`` 域里。"""
    reg.register_domain(Domain(
        id="navbar", has_root=False, entry="nav.home",
        exit_directions={Direction.UP, Direction.DOWN},
        direction_aliases={Direction.LEFT: "prev", Direction.RIGHT: "next"},
    ))
    reg.register_node(Node(id="outer.n", widget=object(),
                           child_domain="navbar"), domain_id=host)
    reg.register_node(Node(id="nav.home", widget=object()), domain_id="navbar")
    reg.register_node(Node(id="nav.search", widget=object()), domain_id="navbar")
    reg.register_node(Node(id="nav.settings", widget=object()), domain_id="navbar")


def test_move_left_in_navbar_goes_prev(reg):
    reg.register_domain(Domain(id="outer", has_root=True, is_real_page=True))
    _setup_navbar(reg, host="outer")
    reg.ensure_page("p1", root_domain="outer")
    reg.set_current("p1", "outer.n")
    reg.enter("p1", how="tab")  # → navbar(nav.home)

    reg.move("p1", Direction.RIGHT)  # → nav.search
    assert reg.current_frame("p1").current == "nav.search"
    reg.move("p1", Direction.LEFT)   # → nav.home
    assert reg.current_frame("p1").current == "nav.home"


def test_move_right_alias_enter_on_list_view(reg):
    """list_view 有 RIGHT → enter 别名，lv.item_a 有 child_domain=form,
    has_root=True → 推入 form 虚拟根，返回 None，栈深 +1。"""
    reg.ensure_page("p1", root_domain="page")
    reg.set_current("p1", "list_view_container")
    reg.enter("p1", how="tab")  # → list_view(lv.item_a)
    depth = len(reg.restore_page("p1").stack)

    result = reg.move("p1", Direction.RIGHT)
    assert result is None
    state = reg.restore_page("p1")
    assert len(state.stack) == depth + 1
    assert state.stack[-1].domain_id == "form"
    assert state.stack[-1].current is None


def test_move_up_navbar_boundary_exits(reg):
    """navbar 节点 grid=None，UP 命中 exit_directions → exit_back，
    返回父帧 current 对应 Node。"""
    reg.register_domain(Domain(id="outer", has_root=True, is_real_page=True))
    _setup_navbar(reg, host="outer")
    reg.ensure_page("p1", root_domain="outer")
    reg.set_current("p1", "outer.n")
    reg.enter("p1", how="tab")  # → navbar(nav.home)
    assert reg.current_frame("p1").domain_id == "navbar"

    result = reg.move("p1", Direction.UP)
    assert result is not None
    assert result.id == "outer.n"
    state = reg.restore_page("p1")
    assert state.stack[-1].domain_id == "outer"
    assert state.stack[-1].current == "outer.n"


def test_move_up_on_grid_domain_boundary_returns_none():
    """普通网格域，UP 到边界且不在 exit_directions → None，current 不动。"""
    reg = NavRegistry()
    reg.register_domain(Domain(id="grid", has_root=True, is_real_page=True))
    reg.register_node(
        Node(id="g00", widget=object(), grid=GridPlacement(0, 0)),
        domain_id="grid",
    )
    reg.register_node(
        Node(id="g10", widget=object(), grid=GridPlacement(1, 0)),
        domain_id="grid",
    )
    reg.ensure_page("p1", root_domain="grid")
    reg.set_current("p1", "g00")

    result = reg.move("p1", Direction.UP)
    assert result is None
    assert reg.current_frame("p1").current == "g00"


def test_move_down_on_grid_advances():
    reg = NavRegistry()
    reg.register_domain(Domain(id="grid", has_root=True, is_real_page=True))
    reg.register_node(
        Node(id="g00", widget=object(), grid=GridPlacement(0, 0)),
        domain_id="grid",
    )
    reg.register_node(
        Node(id="g10", widget=object(), grid=GridPlacement(1, 0)),
        domain_id="grid",
    )
    reg.ensure_page("p1", root_domain="grid")
    reg.set_current("p1", "g00")
    result = reg.move("p1", Direction.DOWN)
    assert result.id == "g10"


# --- 容器踏板 ---

def test_pedal_A_path_entry_is_container():
    """A 路径：从节点进子域（how=tab 或 has_root=False），entry 是容器 → 踏板。"""
    reg = NavRegistry()
    reg.register_domain(Domain(id="root", has_root=True, is_real_page=True))
    reg.register_domain(Domain(id="mid", has_root=True, entry="mid.c"))
    reg.register_domain(Domain(id="leaf", has_root=False, entry="leaf.n"))
    reg.register_node(Node(id="root.n", widget=object(), child_domain="mid"),
                      domain_id="root")
    reg.register_node(Node(id="mid.c", widget=object(),
                           child_domain="leaf", focusable=False),
                      domain_id="mid")
    reg.register_node(Node(id="leaf.n", widget=object()), domain_id="leaf")
    reg.ensure_page("p1", root_domain="root")
    reg.set_current("p1", "root.n")

    # 触发 A 路径：how="tab"（mid.has_root=True 时只有 tab 走 entry 解析）
    reg.enter("p1", how="tab")
    state = reg.restore_page("p1")
    assert state.stack[-1].domain_id == "leaf"
    assert state.stack[-1].current == "leaf.n"
    assert state.stack[-2].domain_id == "root"
    assert state.stack[-2].current == "root.n"


def test_pedal_C_path_entry_is_container():
    """C 路径：虚拟根进入，entry 是容器 → 踏板下钻。"""
    reg = NavRegistry()
    reg.register_domain(Domain(id="root", has_root=True, is_real_page=True,
                               entry="root.c"))
    reg.register_domain(Domain(id="leaf", has_root=False, entry="leaf.n"))
    reg.register_node(Node(id="root.c", widget=object(),
                           child_domain="leaf", focusable=False),
                      domain_id="root")
    reg.register_node(Node(id="leaf.n", widget=object()), domain_id="leaf")
    reg.ensure_page("p1", root_domain="root")

    reg.enter("p1", how="tab")
    state = reg.restore_page("p1")
    assert state.stack[-1].domain_id == "leaf"
    assert state.stack[-1].current == "leaf.n"


def test_pedal_cycle_hits_max_depth():
    """A→B→A→B 循环，达到 ``_MAX_PEDAL_DEPTH`` 后返回 None，栈不增长。"""
    reg = NavRegistry()
    reg.register_domain(Domain(id="root", has_root=True, is_real_page=True,
                               entry="root.n"))
    reg.register_domain(Domain(id="A", has_root=False, entry="a.c"))
    reg.register_domain(Domain(id="B", has_root=False, entry="b.c"))
    reg.register_node(Node(id="root.n", widget=object()), domain_id="root")
    reg.register_node(Node(id="a.c", widget=object(),
                           child_domain="B", focusable=False), domain_id="A")
    reg.register_node(Node(id="b.c", widget=object(),
                           child_domain="A", focusable=False), domain_id="B")
    reg.ensure_page("p1", root_domain="root")
    reg.set_current("p1", "root.n")

    # 构造：直接推入 A 域，从它的虚拟根开始踏板循环
    state = reg.restore_page("p1")
    state.stack.append(DomainFrame(domain_id="A", current=None))
    depth_before = len(state.stack)
    result = reg.enter("p1", how="tab")   # 从 A 虚拟根 → 踏板循环
    assert result is None
    # 栈不增长（踏板中途失败不推帧）
    assert len(state.stack) == depth_before


def test_push_child_focusable_false_no_child_returns_none():
    """修复验证：目标节点 focusable=False 且无 child_domain → 不可落点，
    返回 None。不得把它塞进 current（旧版 fall-through bug）。"""
    reg = NavRegistry()
    reg.register_domain(Domain(id="root", has_root=True, is_real_page=True))
    reg.register_domain(Domain(id="sub", has_root=False, entry="s.bad"))
    reg.register_node(Node(id="root.n", widget=object(),
                           child_domain="sub"), domain_id="root")
    reg.register_node(Node(id="s.bad", widget=object(), focusable=False),
                      domain_id="sub")
    reg.ensure_page("p1", root_domain="root")
    reg.set_current("p1", "root.n")

    depth_before = len(reg.restore_page("p1").stack)
    result = reg.enter("p1", how="tab")
    assert result is None
    state = reg.restore_page("p1")
    # 栈未变，且 sub 帧未被推入
    assert len(state.stack) == depth_before
    assert state.stack[-1].domain_id == "root"
    assert state.stack[-1].current == "root.n"


# --- set_current / switch_page ---

def test_set_current_page_not_created(reg):
    with pytest.raises(UnknownNavPage):
        reg.set_current("nope", None)


def test_set_current_unknown_node(reg):
    reg.ensure_page("p1", root_domain="page")
    with pytest.raises(UnknownNavNode):
        reg.set_current("p1", "nope")


def test_set_current_wrong_domain(reg):
    reg.ensure_page("p1", root_domain="page")
    with pytest.raises(UnknownNavNode):
        reg.set_current("p1", "form.name")  # form.name 属于 form 域


def test_set_current_none(reg):
    reg.ensure_page("p1", root_domain="page")
    reg.set_current("p1", "list_view_container")
    reg.set_current("p1", None)
    assert reg.current_frame("p1").current is None


def test_switch_page_v1_noop(reg):
    reg.ensure_page("p1", root_domain="page")
    assert reg.switch_page("p1") == ("p1", "p1")


# --- focus_node ---

def test_focus_node_uses_focus_fn():
    reg = NavRegistry()
    flag = []
    node = Node(id="n", widget=object(), focus_fn=lambda: flag.append(1))
    reg.focus_node(node)
    assert flag == [1]


def test_focus_node_uses_setFocus():
    reg = NavRegistry()
    w = FakeWidget()
    node = Node(id="n", widget=w)
    reg.focus_node(node)
    assert w.focused is True


# --- visible_fn：隐藏节点不参与落点 ---

class _Toggle:
    """可控可见性：``visible_fn`` 的可变来源。"""

    def __init__(self, visible=True):
        self.visible = visible

    def __call__(self):
        return self.visible


def test_entry_hidden_falls_back_to_next_visible():
    """entry 指向隐藏节点 → 降级到下一个可见 focusable。"""
    reg = NavRegistry()
    reg.register_domain(Domain(id="d", has_root=True, entry="hidden"))
    reg.register_node(Node(id="hidden", widget=object(),
                           visible_fn=lambda: False), domain_id="d")
    reg.register_node(Node(id="shown", widget=object()), domain_id="d")
    reg.ensure_page("p1", root_domain="d")

    node = reg.enter("p1", how="tab")
    assert node is not None
    assert node.id == "shown"
    assert reg.current_frame("p1").current == "shown"


def test_entry_hidden_with_no_visible_node_returns_none():
    reg = NavRegistry()
    reg.register_domain(Domain(id="d", has_root=True, entry="hidden"))
    reg.register_node(Node(id="hidden", widget=object(),
                           visible_fn=lambda: False), domain_id="d")
    reg.ensure_page("p1", root_domain="d")
    assert reg.enter("p1", how="tab") is None


def test_grid_scan_skips_hidden_neighbor():
    reg = NavRegistry()
    reg.register_domain(Domain(id="grid", has_root=True, is_real_page=True))
    reg.register_node(
        Node(id="g00", widget=object(), grid=GridPlacement(0, 0)),
        domain_id="grid",
    )
    reg.register_node(
        Node(id="g01", widget=object(), grid=GridPlacement(0, 1),
             visible_fn=lambda: False),
        domain_id="grid",
    )
    reg.register_node(
        Node(id="g02", widget=object(), grid=GridPlacement(0, 2)),
        domain_id="grid",
    )
    reg.ensure_page("p1", root_domain="grid")
    reg.set_current("p1", "g00")

    node = reg.move("p1", Direction.RIGHT)
    assert node is not None
    assert node.id == "g02"


def test_sequential_skips_hidden():
    """无根域没有虚拟根，得从容器节点推入才能测 next/prev。"""
    reg = NavRegistry()
    reg.register_domain(Domain(id="page", has_root=True, is_real_page=True))
    reg.register_domain(Domain(
        id="nav", has_root=False, entry="a",
        direction_aliases={Direction.RIGHT: "next"},
    ))
    reg.register_node(Node(id="page.c", widget=object(),
                           child_domain="nav"), domain_id="page")
    reg.register_node(Node(id="a", widget=object()), domain_id="nav")
    reg.register_node(Node(id="b", widget=object(),
                           visible_fn=lambda: False), domain_id="nav")
    reg.register_node(Node(id="c", widget=object()), domain_id="nav")
    reg.ensure_page("p1", root_domain="page")
    reg.set_current("p1", "page.c")
    reg.enter("p1", how="tab")           # → nav(a)
    assert reg.current_frame("p1").current == "a"

    node = reg.move("p1", Direction.RIGHT)
    assert node is not None
    assert node.id == "c"


def test_visible_fn_exception_treated_as_hidden(caplog):
    def boom():
        raise RuntimeError("widget gone")

    reg = NavRegistry()
    reg.register_domain(Domain(id="d", has_root=True, entry="bad"))
    reg.register_node(Node(id="bad", widget=object(), visible_fn=boom),
                      domain_id="d")
    reg.register_node(Node(id="ok", widget=object()), domain_id="d")
    reg.ensure_page("p1", root_domain="d")

    caplog.set_level(logging.WARNING)
    node = reg.enter("p1", how="tab")
    assert node.id == "ok"
    assert any("visible_fn raised" in r.message for r in caplog.records)


def test_set_current_on_hidden_node_warns_but_lands(caplog):
    reg = NavRegistry()
    reg.register_domain(Domain(id="d", has_root=True))
    reg.register_node(Node(id="hidden", widget=object(),
                           visible_fn=lambda: False), domain_id="d")
    reg.ensure_page("p1", root_domain="d")

    caplog.set_level(logging.WARNING)
    reg.set_current("p1", "hidden")
    assert reg.current_frame("p1").current == "hidden"
    assert any("hidden node" in r.message for r in caplog.records)


def test_visible_fn_toggle_takes_effect_immediately():
    """可见性是动态的：同一个 registry 内翻转 false→true 即恢复落点。"""
    flag = _Toggle(False)
    reg = NavRegistry()
    reg.register_domain(Domain(id="d", has_root=True, entry="n"))
    reg.register_node(Node(id="n", widget=object(), visible_fn=flag),
                      domain_id="d")
    reg.ensure_page("p1", root_domain="d")
    assert reg.enter("p1", how="tab") is None

    flag.visible = True
    node = reg.enter("p1", how="tab")
    assert node is not None
    assert node.id == "n"


# --- should_pass_through：节点级让渡判定 ---

def test_pass_through_only_declared_keys():
    reg = NavRegistry()
    reg.register_domain(Domain(id="d", has_root=True))
    reg.register_node(Node(id="n", widget=object(),
                           pass_through_keys={NAV_UP}), domain_id="d")
    reg.ensure_page("p1", root_domain="d")
    reg.set_current("p1", "n")

    assert reg.should_pass_through("p1", NAV_UP) is True
    assert reg.should_pass_through("p1", NAV_DOWN) is False


def test_pass_through_empty_set_overrides_widget_flag():
    """``pass_through_keys=set()`` 是显式空集，压过 widget 的 keymap_passthrough。"""
    widget = FakeWidget()
    widget.keymap_passthrough = True
    reg = NavRegistry()
    reg.register_domain(Domain(id="d", has_root=True))
    reg.register_node(Node(id="n", widget=widget, pass_through_keys=set()),
                      domain_id="d")
    reg.ensure_page("p1", root_domain="d")
    reg.set_current("p1", "n")

    for key in DEFAULT_PASS_THROUGH_KEYS:
        assert reg.should_pass_through("p1", key) is False


def test_pass_through_falls_back_to_widget_flag():
    widget = FakeWidget()
    widget.keymap_passthrough = True
    reg = NavRegistry()
    reg.register_domain(Domain(id="d", has_root=True))
    reg.register_node(Node(id="n", widget=widget), domain_id="d")
    reg.ensure_page("p1", root_domain="d")
    reg.set_current("p1", "n")

    for key in DEFAULT_PASS_THROUGH_KEYS:
        assert reg.should_pass_through("p1", key) is True


def test_pass_through_accepts_direction_and_string_equally():
    reg = NavRegistry()
    reg.register_domain(Domain(id="d", has_root=True))
    reg.register_node(Node(id="n", widget=object(),
                           pass_through_keys={"up"}), domain_id="d")
    reg.ensure_page("p1", root_domain="d")
    reg.set_current("p1", "n")

    assert reg.should_pass_through("p1", Direction.UP) is True
    assert reg.should_pass_through("p1", NAV_UP) is True
    assert (reg.should_pass_through("p1", "up")
            == reg.should_pass_through("p1", Direction.UP))


# --- 叶域端到端 ---

def test_leaf_domain_via_pass_through():
    """无根域 + 容器节点 pass_through_keys 全集 → 进入后方向键让渡。"""
    reg = NavRegistry()
    reg.register_domain(Domain(id="page", has_root=True, is_real_page=True))
    reg.register_domain(Domain(id="content", has_root=False,
                               entry="content.body"))
    reg.register_node(Node(id="page.c", widget=object(),
                           child_domain="content", focusable=False),
                      domain_id="page")
    reg.register_node(Node(id="content.body", widget=object(),
                           pass_through_keys=set(DEFAULT_PASS_THROUGH_KEYS)),
                      domain_id="content")
    reg.ensure_page("p1", root_domain="page")
    reg.set_current("p1", "page.c")

    reg.enter("p1", how="tab")
    assert reg.current_frame("p1").current == "content.body"
    for key in ("up", "down", "left", "right", "enter", "esc", "tab"):
        assert reg.should_pass_through("p1", key) is True


def test_direction_values_match_nav_constants():
    assert Direction.UP.value == NAV_UP
    assert Direction.DOWN.value == NAV_DOWN
    assert Direction.LEFT.value == NAV_LEFT
    assert Direction.RIGHT.value == NAV_RIGHT


class _QtLikeWidget:
    """模拟真实控件的 Qt 动态属性语义：``setProperty`` 只立 ``property()``。

    PySide6 的动态属性**不会**变成同名 Python 属性，所以只看 ``getattr`` 的实现
    会永远读到 ``False``——这条测试锁住「两种打标方式都认」。
    """

    def __init__(self, **flags):
        self._props = dict(flags)

    def property(self, name):
        return self._props.get(name)


def test_pass_through_reads_qt_dynamic_property():
    widget = _QtLikeWidget(keymap_passthrough=True)
    assert getattr(widget, "keymap_passthrough", "MISSING") == "MISSING"
    reg = NavRegistry()
    reg.register_domain(Domain(id="d", has_root=True))
    reg.register_node(Node(id="n", widget=widget), domain_id="d")
    reg.ensure_page("p1", root_domain="d")
    reg.set_current("p1", "n")

    for key in DEFAULT_PASS_THROUGH_KEYS:
        assert reg.should_pass_through("p1", key) is True


# --- _widget_flag 的三层守卫 ---

def _nav_with_widget(widget) -> NavRegistry:
    reg = NavRegistry()
    reg.register_domain(Domain(id="d", has_root=True))
    reg.register_node(Node(id="n", widget=widget), domain_id="d")
    reg.ensure_page("p1", root_domain="d")
    reg.set_current("p1", "n")
    return reg


def test_widget_without_property_method_does_not_crash():
    """非 QObject widget（没有 ``property``）→ 退到 getattr，不崩、不让渡。"""

    class Plain:
        __slots__ = ()

    reg = _nav_with_widget(Plain())
    for key in DEFAULT_PASS_THROUGH_KEYS:
        assert reg.should_pass_through("p1", key) is False


def test_widget_property_returning_truthy_string_is_not_true():
    """严格要求 ``is True``：``setProperty("x", "yes")`` 不算声明。"""
    widget = _QtLikeWidget(keymap_passthrough="yes")
    reg = _nav_with_widget(widget)
    for key in DEFAULT_PASS_THROUGH_KEYS:
        assert reg.should_pass_through("p1", key) is False


def test_widget_property_raising_is_swallowed():
    class ExplodingProperty:
        def property(self, name):
            raise RuntimeError("no such property")

        def __getattr__(self, name):  # getattr 也炸，第二道守卫
            raise RuntimeError("no such attribute")

    reg = _nav_with_widget(ExplodingProperty())
    for key in DEFAULT_PASS_THROUGH_KEYS:
        assert reg.should_pass_through("p1", key) is False
