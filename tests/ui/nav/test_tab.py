#
"""域内 Tab 语义（``Domain.tab_order`` / ``tab_next`` / ``tab_prev``）。

三条规则：

* **顺序**：``tab_order`` 优先，否则按 ``GridPlacement(row, col)`` 排序，
  无 grid 的节点排在后面（彼此按注册顺序）；
* **过滤**：``focusable=False`` 与不可见节点一律跳过；
* **边界**：第一个 / 最后一个可见落点之外返回 ``None``（v1 **不跨域**）。

另有两条接线验收（主窗口真实结构）：``album`` 域的
``close → edit → add → cover → trash``，``detail_hud`` 域的
``multiple → favorite → information → edit → trash``。
"""

from __future__ import annotations

import pytest

from albuswall.ui.nav.model import Direction, Domain, GridPlacement, Node
from albuswall.ui.nav.registry import NavRegistry
from albuswall.ui.nav.wiring import NAV_PAGE_ID, register_window_nav

from .test_wiring import _fake_window


# ==================== 合成域 ====================

def _grid_rig(
        *, tab_order: list[str] | None = None, hidden: str | None = None,
) -> NavRegistry:
    """一个 row/col 与注册顺序**不一致**的域：b(0,1) 先注册，a(0,0) 后注册。"""
    r = NavRegistry()
    r.register_domain(Domain(id="page", has_root=True, is_real_page=True))
    r.register_domain(
        Domain(id="t", has_root=False, entry="a", tab_order=tab_order))
    r.register_node(Node(id="page.g", widget=object(), child_domain="t",
                         focusable=False), domain_id="page")
    for node_id, grid in (("b", GridPlacement(0, 1)),
                          ("a", GridPlacement(0, 0)),
                          ("c", GridPlacement(1, 0))):
        r.register_node(
            Node(id=node_id, widget=object(), grid=grid,
                 visible_fn=(lambda: False) if node_id == hidden else None),
            domain_id="t")
    r.ensure_page("p", root_domain="page")
    r.set_current("p", "page.g")
    r.enter("p", how="tab")            # → t(a)，entry 优先
    return r


def test_tab_order_defaults_to_grid_coordinates():
    r = _grid_rig()
    assert r.current_frame("p").current == "a"      # entry

    assert r.tab_next("p").id == "b"                # (0,0) → (0,1)
    assert r.tab_next("p").id == "c"                # (0,1) → (1,0)
    assert r.tab_next("p") is None                  # 末尾：v1 不跨域、不回绕

    assert r.tab_prev("p").id == "b"
    assert r.tab_prev("p").id == "a"
    assert r.tab_prev("p") is None


def test_tab_order_override_is_strict():
    r = _grid_rig(tab_order=["c", "a"])             # 只列 c / a，b 被排除
    assert r.current_frame("p").current == "a"

    assert r.tab_next("p") is None                  # a 是列表末尾
    assert r.tab_prev("p").id == "c"
    assert r.tab_prev("p") is None


def test_tab_skips_hidden_nodes():
    r = _grid_rig(hidden="b")
    assert r.tab_next("p").id == "c"                # 跳过隐藏的 b
    assert r.tab_next("p") is None
    assert r.tab_prev("p").id == "a"


def test_tab_skips_unfocusable_nodes():
    r = NavRegistry()
    r.register_domain(Domain(id="page", has_root=True, is_real_page=True))
    r.register_domain(Domain(id="t", has_root=False, entry="a"))
    r.register_node(Node(id="page.g", widget=object(), child_domain="t",
                         focusable=False), domain_id="page")
    r.register_node(Node(id="a", widget=object(), grid=GridPlacement(0, 0)),
                    domain_id="t")
    r.register_node(Node(id="container", widget=object(),
                         grid=GridPlacement(0, 1), focusable=False,
                         child_domain=None), domain_id="t")
    r.register_node(Node(id="last", widget=object(), grid=GridPlacement(1, 0)),
                    domain_id="t")
    r.ensure_page("p", root_domain="page")
    r.set_current("p", "page.g")
    r.enter("p", how="tab")

    assert r.tab_next("p").id == "last"
    assert r.tab_next("p") is None


def test_tab_from_virtual_root_lands_entry():
    """``current=None`` 时 Tab 落域入口（等价 ``enter(how="tab")``）。"""
    r = NavRegistry()
    r.register_domain(Domain(id="page", has_root=True, is_real_page=True))
    r.register_domain(Domain(id="t", has_root=False, entry="a"))
    r.register_node(Node(id="page.g", widget=object(), child_domain="t",
                         focusable=False), domain_id="page")
    r.register_node(Node(id="a", widget=object(), grid=GridPlacement(0, 0)),
                    domain_id="t")
    r.register_node(Node(id="b", widget=object(), grid=GridPlacement(0, 1)),
                    domain_id="t")
    r.ensure_page("p", root_domain="page")
    r.enter("p", how="tab")                 # page → 踏板 → t(a)
    r.current_frame("p").current = None     # 手动退回虚拟根

    assert r.tab_next("p").id == "a"
    r.current_frame("p").current = None
    assert r.tab_prev("p").id == "a"


def test_tab_falls_back_when_current_hidden():
    """current 停在隐藏节点上：Tab 落到顺序里的第一个 / 最后一个，不卡死。"""
    r = _grid_rig(hidden="a")
    assert r.current_frame("p").current == "b"      # entry 隐藏 → 落 b

    r.set_current("p", "a")                         # 人为停在隐藏节点上
    assert r.tab_next("p").id == "b"                # 顺序第一个可见
    r.current_frame("p").current = "a"
    assert r.tab_prev("p").id == "c"                # 顺序最后一个可见


def test_tab_respects_suspend():
    r = _grid_rig()
    r.suspend("p")
    try:
        assert r.tab_next("p") is None
        assert r.tab_prev("p") is None
    finally:
        r.resume("p")
    assert r.tab_next("p") is not None


def test_tab_never_crosses_domains():
    """v1 不跨域：域内走到头就是 ``None``，不弹栈也不跳兄弟域。"""
    r = _grid_rig()
    depth = len(r.restore_page("p").stack)
    r.tab_next("p")
    r.tab_next("p")
    assert r.tab_next("p") is None
    assert len(r.restore_page("p").stack) == depth
    assert r.current_frame("p").domain_id == "t"


# ==================== 主窗口接线验收 ====================

@pytest.fixture
def nav():
    """按真实窗口结构注册好的注册表（假窗口，不需要 Qt）。"""
    registry = NavRegistry()
    register_window_nav(_fake_window(), registry=registry)
    return registry


def _walk(nav: NavRegistry, count: int) -> list[str]:
    landed: list[str] = []
    for _ in range(count):
        node = nav.tab_next(NAV_PAGE_ID)
        assert node is not None, landed
        landed.append(node.id)
    return landed


def test_album_tab_walks_close_to_trash(nav):
    nav.set_current(NAV_PAGE_ID, "window.album")
    nav.enter(NAV_PAGE_ID, how="tab")
    assert nav.current_frame(NAV_PAGE_ID).current == "album.close"

    assert _walk(nav, 4) == ["album.edit", "album.add", "album.cover",
                             "album.trash"]
    assert nav.tab_next(NAV_PAGE_ID) is None


def test_detail_hud_tab_walks_multiple_to_trash(nav):
    window = _fake_window()
    window.overlay.current = window.detail
    nav = NavRegistry()
    register_window_nav(window, registry=nav)

    nav.set_current(NAV_PAGE_ID, "window.detail")
    nav.enter(NAV_PAGE_ID, how="tab")               # → detail.image
    for _ in range(3):                              # image → tagbar → strip → hud
        assert nav.move(NAV_PAGE_ID, Direction.DOWN) is not None
    assert nav.current_frame(NAV_PAGE_ID).current == "detail.hud"

    nav.enter(NAV_PAGE_ID, how="tab")               # 推进 detail_hud
    assert nav.current_frame(NAV_PAGE_ID).domain_id == "detail_hud"
    assert nav.current_frame(NAV_PAGE_ID).current == "hud.multiple"

    assert _walk(nav, 4) == ["hud.favorite", "hud.information", "hud.edit",
                             "hud.trash"]
    assert nav.tab_next(NAV_PAGE_ID) is None


def test_enter_tab_still_lands_the_entry(nav):
    """D3：``enter(how="tab")`` 仍是「进域入口」，不是域内遍历。"""
    nav.set_current(NAV_PAGE_ID, "window.album")
    node = nav.enter(NAV_PAGE_ID, how="tab")
    assert node is not None and node.id == "album.close"

    # 已经在域内的叶/按钮节点上再来一次 enter(tab)：NAV 不动（域内遍历是
    # tab_next 的事），返回 None 让分发器决定要不要冒泡
    assert nav.enter(NAV_PAGE_ID, how="tab") is None
    assert nav.current_frame(NAV_PAGE_ID).current == "album.close"
