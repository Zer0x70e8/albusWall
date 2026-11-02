#
"""容器节点永远 ``NoFocus``；落点控件只升不降。

硬接管之后 Qt 的焦点链不再参与跨区域决策，因此区域容器（标题栏、内容层、
overlay 的三个页、HUD 容器、信息面板）**绝不该**成为 Qt 的焦点目标；焦点只
落在它们内部的落点控件上（``Node`` 的 ``focus_on`` / 子域 entry）。

唯一例外是「容器与叶共用同一个控件」（内容层：``window.content`` 与
``content.body`` 都是 ``Content``）：那时容器规则让位于叶，否则叶域的键盘
光标无处可落。判定走 ``NavPresenter.is_leaf_widget``（注册表在 presenter 手里）。
"""

from __future__ import annotations

from PySide6.QtCore import Qt

from albuswall.ui.nav.wiring import NAV_PAGE_ID


def _container_nodes(nav):
    """所有「区域入口 / 容器踏板」节点（``child_domain`` 非空）。"""
    return [
        node
        for domain in nav.domains()
        for node in domain.nodes.values()
        if node.child_domain is not None
    ]


def test_every_container_node_is_unfocusable(nav_rig):
    rig = nav_rig
    containers = _container_nodes(rig.nav)
    assert containers, "wiring 之后应有区域入口节点"

    exempt = {node.id for node in containers
              if rig.presenter.is_leaf_widget(node.widget)}

    for node in containers:
        rig.nav.focus_node(node)          # 让 ensure_focusable 真的跑一遍
        rig.app.processEvents()
        if node.id in exempt:
            continue
        assert node.widget.focusPolicy() == Qt.FocusPolicy.NoFocus, (
            f"容器节点 {node.id} 拿到了 Qt 焦点（应为 NoFocus）")


def test_the_only_exempt_container_shares_its_widget_with_a_leaf(nav_rig):
    """例外只有内容层：容器（``window.content``）与叶（``content.body``）同控件。"""
    rig = nav_rig
    exempt = {node.id for node in _container_nodes(rig.nav)
              if rig.presenter.is_leaf_widget(node.widget)}
    assert exempt == {"window.content"}

    # 叶节点仍然可聚焦（否则内容层的键盘光标无处可落）
    leaf = rig.nav.get_node("content.body")
    rig.nav.focus_node(leaf)
    rig.app.processEvents()
    assert rig.window.content.focusPolicy() != Qt.FocusPolicy.NoFocus


def test_focus_node_on_the_titlebar_gateway_leaves_the_container_unfocusable(
        nav_rig,
):
    """``focus_node(window.titlebar)``：TitleBar 仍是 ``NoFocus``，焦点进按钮。"""
    rig = nav_rig
    gateway = rig.nav.get_node("window.titlebar")

    assert rig.nav.focus_node(gateway) is True
    rig.app.processEvents()

    assert rig.window.title_bar.focusPolicy() == Qt.FocusPolicy.NoFocus
    assert rig.app.focusWidget() is rig.window.title_bar.album_button


def test_gateways_stay_selectable_by_nav(nav_rig):
    """容器在 Qt 侧不可聚焦，但在 nav 侧仍是可落点（``Node.focusable`` 生效）。"""
    rig = nav_rig
    gateway = rig.nav.get_node("window.titlebar")
    assert gateway.focusable is True

    rig.land("window.titlebar")
    assert rig.press(Qt.Key.Key_Down) is True
    assert rig.current == "window.content"
    assert rig.frame.domain_id == "window"


def test_leaf_focus_lands_on_the_widget_itself(nav_rig):
    """回归：普通叶节点 ``focus_node`` 之后焦点确实在它的控件上。"""
    rig = nav_rig
    rig.open_page("album")
    view = rig.window.album.album_view

    assert rig.nav.focus_node(rig.nav.get_node("album.cover")) is True
    rig.app.processEvents()

    assert rig.app.focusWidget() is view
    # 焦点回灌把光标对齐到同一个叶节点
    assert rig.current == "album.cover"
    assert rig.frame.domain_id == "album"
    assert rig.nav.current_frame(NAV_PAGE_ID) is rig.frame
