#
"""普通叶节点不装 ``focus_fn``：``focus_node`` 走 ``apply_focus`` + 验证路径。

``focus_fn`` 是「nav 猜不出真实落点」时的逃生门（契约 4）：装上它，执行结果
就不验证了。普通叶节点的落点控件就是节点控件本身，没有任何猜不出的东西，
所以不装 —— 这样「焦点没落上去」能被 ``focus_node`` 发现、由分发器回滚。

``_gateway_node`` 是相反的取舍：区域入口的 ``widget`` 是容器本体（Qt 侧被
固定成 ``NoFocus``），落点只能由 ``focus_fn`` 交给内部控件，所以那里保留。
"""

from __future__ import annotations

from PySide6.QtCore import Qt


def test_plain_leaf_nodes_have_no_focus_fn(nav_rig):
    rig = nav_rig
    for node_id in ("content.body", "album.cover", "album.trash",
                    "source.cards", "detail.image", "detail.strip",
                    "panel.meta"):
        node = rig.nav.get_node(node_id)
        assert node is not None, node_id
        assert node.focus_fn is None, f"{node_id} 不该装 focus_fn"


def test_composite_leaf_nodes_keep_their_focus_fn(nav_rig):
    """落点控件 ≠ 节点控件的复合叶节点仍然装 ``focus_fn``。"""
    rig = nav_rig
    for node_id in ("source.form", "detail.tagbar"):
        node = rig.nav.get_node(node_id)
        assert node is not None, node_id
        assert node.focus_fn is not None, node_id


def test_album_cover_focuses_the_view_and_verifies(nav_rig):
    """``focus_node(album.cover)``：走 apply_focus，焦点落在 album_view，返回 True。"""
    rig = nav_rig
    rig.open_page("album")
    node = rig.nav.get_node("album.cover")

    assert rig.nav.focus_node(node) is True
    rig.app.processEvents()

    assert rig.app.focusWidget() is rig.window.album.album_view


def test_gateway_nodes_land_inside_their_region(nav_rig):
    """区域入口的 ``widget`` 是容器本体，焦点由 ``focus_fn`` 交给内部控件。"""
    rig = nav_rig
    rig.open_page("album")

    gateway = rig.nav.get_node("window.album")
    assert gateway.widget is rig.window.album       # 容器本体
    assert gateway.focus_fn is not None             # 落点交给内部控件
    assert gateway.focusable is True                # nav 侧仍可选

    assert rig.nav.focus_node(gateway) is True
    rig.app.processEvents()
    assert rig.app.focusWidget() is rig.window.album.album_view


def test_titlebar_gateway_lands_on_its_first_button(nav_rig):
    rig = nav_rig
    gateway = rig.nav.get_node("window.titlebar")
    assert gateway.focus_fn is not None
    assert gateway.widget is rig.window.title_bar

    assert rig.nav.focus_node(gateway) is True
    rig.app.processEvents()
    assert rig.app.focusWidget() is rig.window.title_bar.album_button
    assert rig.window.title_bar.focusPolicy() == Qt.FocusPolicy.NoFocus
