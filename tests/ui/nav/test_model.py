#
"""``albuswall.ui.nav.model`` 的数据结构回归。

守三条边界：

* ``GridPlacement`` 在**构造期**校验（负值 / span < 1 → ``InvalidNavGrid``）；
* ``Node`` 只要求 ``id`` + 任意 ``widget``，不 import Qt、不做类型检查；
* ``Domain`` 的可变默认值（``nodes`` / ``direction_aliases`` /
  ``exit_directions``）必须各自独立，不能跨实例共享。
"""

from __future__ import annotations

import pytest

from albuswall.ui.nav.errors import InvalidNavGrid
from albuswall.ui.nav.model import (
    DEFAULT_PASS_THROUGH_KEYS,
    NAV_DOWN,
    NAV_ENTER,
    NAV_ESC,
    NAV_LEFT,
    NAV_RIGHT,
    NAV_TAB,
    NAV_UP,
    Domain,
    GridPlacement,
    NavPlacement,
    Node,
)

#: 供 hash 测试复用的同一 widget 引用（相等性依赖 id + widget）。
W = object()


def test_grid_negative_row_raises():
    with pytest.raises(InvalidNavGrid):
        GridPlacement(-1, 0)


def test_grid_negative_col_raises():
    with pytest.raises(InvalidNavGrid):
        GridPlacement(0, -1)


def test_grid_zero_rowspan_raises():
    with pytest.raises(InvalidNavGrid):
        GridPlacement(0, 0, rowspan=0)


def test_grid_zero_colspan_raises():
    with pytest.raises(InvalidNavGrid):
        GridPlacement(0, 0, colspan=0)


def test_grid_default_span():
    g = GridPlacement(0, 0)
    assert g.rowspan == 1
    assert g.colspan == 1


def test_grid_valid_span():
    g = GridPlacement(1, 2, rowspan=2, colspan=3)
    assert (g.row, g.col, g.rowspan, g.colspan) == (1, 2, 2, 3)


def test_node_constructs_with_arbitrary_widget():
    n = Node(id="n", widget=object())
    assert n.id == "n"
    assert n.focusable is True
    assert n.grid is None
    assert n.child_domain is None
    assert n.action is None
    assert n.focus_fn is None


def test_node_with_focus_fn():
    def fn():
        return None

    n = Node(id="n", widget=object(), focus_fn=fn)
    assert n.focus_fn is fn


def test_domain_nodes_isolated():
    d1 = Domain(id="d1")
    d2 = Domain(id="d2")
    d1.nodes["x"] = Node(id="x", widget=object())
    assert "x" in d1.nodes
    assert "x" not in d2.nodes


def test_domain_defaults():
    d = Domain(id="d")
    assert d.has_root is True
    assert d.allow_exit is True
    assert d.esc_clears_current is False
    assert d.exit_directions == set()
    assert d.direction_aliases == {}


# --- v1 补件：visible_fn / pass_through_keys ---

def test_node_visible_fn_and_pass_through_fields():
    fn = lambda: True  # noqa: E731 - 只验证字段搬运

    n = Node(id="n", widget=object(), visible_fn=fn,
             pass_through_keys={"up", "down"})
    assert n.visible_fn is fn
    assert n.pass_through_keys == {"up", "down"}


def test_node_visible_and_pass_through_default_none():
    n = Node(id="n", widget=object())
    assert n.visible_fn is None
    assert n.pass_through_keys is None


def test_nav_placement_passes_new_fields():
    fn = lambda: False  # noqa: E731
    p = NavPlacement(
        widget=object(), domain="d", visible_fn=fn,
        pass_through_keys={"esc"},
    )
    assert p.visible_fn is fn
    assert p.pass_through_keys == {"esc"}
    assert NavPlacement(widget=object(), domain="d").visible_fn is None


def test_default_pass_through_keys_covers_all_nav_keys():
    assert DEFAULT_PASS_THROUGH_KEYS == {
        NAV_UP, NAV_DOWN, NAV_LEFT, NAV_RIGHT, NAV_ENTER, NAV_ESC, NAV_TAB,
    }


def test_node_hash_ignores_unhashable_fields():
    """``set`` 不可 hash：靠 ``field(hash=False, compare=False)`` 排除。"""
    a = Node(id="n", widget=W, pass_through_keys={"up"})
    b = Node(id="n", widget=W, pass_through_keys={"down"})
    assert hash(a) == hash(b)
    assert a == b  # 行为配置不参与相等性


def test_domain_tab_order_defaults_to_none():
    """D1：``tab_order`` 缺省 ``None`` = 按网格坐标排序，且不共享可变默认值。"""
    first = Domain(id="a")
    second = Domain(id="b")
    assert first.tab_order is None
    assert second.tab_order is None

    first.tab_order = ["a.x"]
    assert second.tab_order is None       # 每个域一份，不是类级共享


def test_domain_tab_order_accepts_explicit_list():
    domain = Domain(id="d", tab_order=["x", "y"])
    assert domain.tab_order == ["x", "y"]
