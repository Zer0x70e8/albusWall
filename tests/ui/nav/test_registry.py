#
"""``NavRegistry`` 的注册语义回归。

覆盖 5 条注册期校验：replace 清旧域、``ensure_page`` 的 ``has_root`` 前提、
无根域缺 entry 的 warning、``NavPlacement.parent`` 校验、span>1 的 warning。
"""

from __future__ import annotations

import logging

import pytest

from albuswall.core.container import Container
from albuswall.ui.nav import (
    NavRegistry,
    get_default_registry,
    register_domain,
    registry_nav_registry,
)
from albuswall.ui.nav.errors import (
    DuplicateNavDomain,
    DuplicateNavNode,
    UnknownNavDomain,
    UnknownNavNode,
)
from albuswall.ui.nav.model import (
    DEFAULT_PASS_THROUGH_KEYS,
    NAV_UP,
    Domain,
    GridPlacement,
    NavPlacement,
    Node,
)


class FakeSlot:
    """最小槽替身：只有导航层需要的两个字段。"""

    def __init__(self, slot_id, nav=None):
        self.slot_id = slot_id
        self.nav = nav


@pytest.fixture
def reg():
    return NavRegistry()


def test_register_domain_ok(reg):
    d = Domain(id="d")
    out = reg.register_domain(d)
    assert out is d
    assert reg.get_domain("d") is d


def test_register_domain_duplicate(reg):
    reg.register_domain(Domain(id="d"))
    with pytest.raises(DuplicateNavDomain):
        reg.register_domain(Domain(id="d"))


def test_register_domain_replace(reg):
    d1 = Domain(id="d")
    d2 = Domain(id="d")
    reg.register_domain(d1)
    reg.register_domain(d2, replace=True)
    assert reg.get_domain("d") is d2


def test_register_domain_parent_unknown(reg):
    with pytest.raises(UnknownNavDomain):
        reg.register_domain(Domain(id="d", parent="nope"))


def test_register_domain_parent_can_be_node(reg):
    reg.register_domain(Domain(id="d"))
    reg.register_node(Node(id="n", widget=object()), domain_id="d")
    reg.register_domain(Domain(id="d2", parent="n"))


def test_register_domain_rootless_no_entry_warns(reg, caplog):
    caplog.set_level(logging.WARNING)
    reg.register_domain(Domain(id="d", has_root=False, entry=None))
    assert any("has_root=False but entry is None" in r.message
               for r in caplog.records)


def test_register_node_ok(reg):
    reg.register_domain(Domain(id="d"))
    n = Node(id="n", widget=object())
    out = reg.register_node(n, domain_id="d")
    assert out is n
    assert reg.get_node("n") is n
    assert n in reg.nodes_in("d")


def test_register_node_domain_missing(reg):
    with pytest.raises(UnknownNavDomain):
        reg.register_node(Node(id="n", widget=object()), domain_id="nope")


def test_register_node_duplicate_id(reg):
    reg.register_domain(Domain(id="d"))
    reg.register_node(Node(id="n", widget=object()), domain_id="d")
    with pytest.raises(DuplicateNavNode):
        reg.register_node(Node(id="n", widget=object()), domain_id="d")


def test_register_node_replace_moves_between_domains(reg):
    reg.register_domain(Domain(id="a"))
    reg.register_domain(Domain(id="b"))
    reg.register_node(Node(id="n", widget=object()), domain_id="a")
    reg.register_node(Node(id="n", widget=object()), domain_id="b", replace=True)
    assert "n" not in reg._domains["a"].nodes
    assert "n" in reg._domains["b"].nodes


def test_register_node_child_domain_unknown(reg):
    reg.register_domain(Domain(id="d"))
    with pytest.raises(UnknownNavDomain):
        reg.register_node(
            Node(id="n", widget=object(), child_domain="nope"),
            domain_id="d",
        )


def test_register_node_span_warns(reg, caplog):
    caplog.set_level(logging.WARNING)
    reg.register_domain(Domain(id="d"))
    reg.register_node(
        Node(id="n", widget=object(), grid=GridPlacement(0, 0, rowspan=2)),
        domain_id="d",
    )
    assert any("span>1" in r.message for r in caplog.records)


def test_nodes_in_unknown_domain_returns_empty(reg):
    assert reg.nodes_in("nope") == ()


def test_domains_returns_registration_order(reg):
    reg.register_domain(Domain(id="a"))
    reg.register_domain(Domain(id="b"))
    ids = [d.id for d in reg.domains()]
    assert ids == ["a", "b"]


def test_unregister_node(reg):
    reg.register_domain(Domain(id="d"))
    reg.register_node(Node(id="n", widget=object()), domain_id="d")
    reg.unregister_node("n")
    assert reg.get_node("n") is None
    assert reg.nodes_in("d") == ()


def test_unregister_domain(reg):
    reg.register_domain(Domain(id="d"))
    reg.unregister_domain("d")
    assert reg.get_domain("d") is None


def test_clear(reg):
    reg.register_domain(Domain(id="d"))
    reg.register_node(Node(id="n", widget=object()), domain_id="d")
    reg.clear()
    assert reg.domains() == ()
    assert reg.get_node("n") is None


# --- register_from_slot ---

def test_register_from_slot_none(reg):
    slot = FakeSlot(slot_id="s", nav=None)
    assert reg.register_from_slot(slot) is None
    assert reg.domains() == ()


def test_register_from_slot_ok(reg):
    reg.register_domain(Domain(id="d", has_root=False, entry="n"))
    slot = FakeSlot(
        slot_id="s1",
        nav=NavPlacement(domain="d", widget=object()),
    )
    node = reg.register_from_slot(slot)
    assert node is not None
    assert node.id == "s1"
    assert node.id in reg._domains["d"].nodes


def test_register_from_slot_parent_unknown(reg):
    reg.register_domain(Domain(id="d", has_root=False, entry="x"))
    slot = FakeSlot(
        slot_id="s1",
        nav=NavPlacement(domain="d", widget=object(), parent="nope"),
    )
    with pytest.raises(UnknownNavDomain):
        reg.register_from_slot(slot)


def test_register_from_slot_accepts_slot_id_attribute(reg):
    """``keymap.ShortcutSlot`` 把槽 id 暴露为 ``id``；两种字段名都要认。"""

    class KeymapLikeSlot:
        def __init__(self, slot_id, nav):
            self.id = slot_id
            self.nav = nav

    reg.register_domain(Domain(id="d", has_root=False, entry="s2"))
    node = reg.register_from_slot(
        KeymapLikeSlot("s2", NavPlacement(domain="d", widget=object())))
    assert node is not None
    assert node.id == "s2"


def test_register_from_slot_without_nav_attribute(reg):
    """没有 nav 字段的槽（当前 keymap 形态）按「无导航声明」处理。"""

    class PlainSlot:
        def __init__(self, slot_id):
            self.id = slot_id

    assert reg.register_from_slot(PlainSlot("s3")) is None


# --- 模块级单例 / 容器登记 ---

def test_module_level_helpers_use_default_registry():
    nav = get_default_registry()
    nav.clear()
    try:
        domain = register_domain(Domain(id="mod.d"))
        assert nav.get_domain("mod.d") is domain
        node = Node(id="mod.n", widget=object())
        nav.register_node(node, domain_id="mod.d")
        assert nav.get_node("mod.n") is node
    finally:
        nav.clear()


def test_registry_nav_registry_binds_singleton():
    container = Container()
    registry_nav_registry(container)
    assert container.require("nav_registry") is get_default_registry()


def test_registry_nav_registry_warns_without_register(caplog):
    caplog.set_level(logging.WARNING)
    registry_nav_registry(object())
    assert any("nav registry not bound" in r.message
               for r in caplog.records)


# --- v1 补件：register_from_slot 透传 / should_pass_through 基础 ---

def test_register_from_slot_passes_visible_and_pass_through(reg):
    visible = lambda: True  # noqa: E731 - 只验证透传
    reg.register_domain(Domain(id="d", has_root=False, entry="s4"))
    node = reg.register_from_slot(FakeSlot(
        slot_id="s4",
        nav=NavPlacement(
            domain="d", widget=object(),
            visible_fn=visible, pass_through_keys={"esc"},
        ),
    ))
    assert node is not None
    assert node.visible_fn is visible
    assert node.pass_through_keys == {"esc"}


def test_should_pass_through_page_not_created(reg):
    assert reg.should_pass_through("nope", NAV_UP) is False


def test_should_pass_through_virtual_root(reg):
    """``current=None`` 时没有「当前节点」，一律不让渡。"""
    reg.register_domain(Domain(id="d", has_root=True))
    reg.register_node(Node(id="n", widget=object(),
                           pass_through_keys={NAV_UP}), domain_id="d")
    reg.ensure_page("p1", root_domain="d")
    assert reg.should_pass_through("p1", NAV_UP) is False

    reg.set_current("p1", "n")
    assert reg.should_pass_through("p1", NAV_UP) is True


def test_should_pass_through_undelcared_node(reg):
    """未声明且 widget 无 ``keymap_passthrough``：一个键都不让渡。"""
    reg.register_domain(Domain(id="d", has_root=True))
    reg.register_node(Node(id="n", widget=object()), domain_id="d")
    reg.ensure_page("p1", root_domain="d")
    reg.set_current("p1", "n")
    for key in DEFAULT_PASS_THROUGH_KEYS:
        assert reg.should_pass_through("p1", key) is False


def test_slot_id_wins_over_id(reg):
    """``slot_id`` 比 ``id`` 更正式：两者都在时用前者。"""

    class BothSlot:
        slot_id = "from_slot_id"
        id = "from_id"  # noqa: A003 - 模拟两套字段名同时存在
        nav = NavPlacement(domain="d", widget=object())

    reg.register_domain(Domain(id="d", has_root=False, entry="from_slot_id"))
    node = reg.register_from_slot(BothSlot())
    assert node is not None
    assert node.id == "from_slot_id"


def test_slot_without_any_id_raises(reg):
    class NoIdSlot:
        nav = NavPlacement(domain="d", widget=object())

    reg.register_domain(Domain(id="d", has_root=False, entry="x"))
    with pytest.raises(UnknownNavNode):
        reg.register_from_slot(NoIdSlot())
