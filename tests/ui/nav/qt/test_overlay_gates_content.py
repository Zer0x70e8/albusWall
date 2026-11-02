#
"""叠层可见 ⇒ 被它盖住的内容层不可落点。

内容层铺满整窗、压在 overlay 之下。叠层页开着时它被完全遮住，方向键 / 焦点
回流却还能把光标送到那里 —— 用户看到的是「选中了看不见的东西」。``wiring``
给 ``window.content``（区域入口）和 ``content.body``（页内叶节点）加了
``_overlay_blank`` 闸门；标题栏**不**加，它盖在叠层之上仍然可达。
"""

from __future__ import annotations

from PySide6.QtCore import Qt


def test_content_is_not_landable_while_an_overlay_page_is_open(nav_rig):
    rig = nav_rig
    rig.open_page("album")

    assert rig.nav.is_visible(rig.nav.get_node("content.body")) is False
    assert rig.nav.is_visible(rig.nav.get_node("window.content")) is False


def test_content_becomes_landable_again_when_the_page_closes(nav_rig):
    rig = nav_rig
    rig.open_page("album")
    assert rig.nav.is_visible(rig.nav.get_node("window.content")) is False

    rig.close_page()

    assert rig.nav.is_visible(rig.nav.get_node("content.body")) is True
    assert rig.nav.is_visible(rig.nav.get_node("window.content")) is True


def test_titlebar_stays_landable_while_an_overlay_page_is_open(nav_rig):
    rig = nav_rig
    rig.open_page("detail")

    assert rig.nav.is_visible(rig.nav.get_node("window.titlebar")) is True


def test_window_domain_arrows_skip_the_covered_content_layer(nav_rig):
    """叠层开着时，``window`` 域的方向键不会落到内容层。"""
    rig = nav_rig
    rig.open_page("album")
    rig.land("window.titlebar")

    assert rig.press(Qt.Key.Key_Down) is True
    assert rig.current != "window.content"
    # 直接落到当前那一页的区域入口（相册页开着）
    assert rig.current == "window.album"


def test_covered_content_leaf_cannot_be_entered(nav_rig):
    """进入内容层域也拿不到落点（``content.body`` 不可见）。"""
    rig = nav_rig
    rig.open_page("album")

    rig.nav.set_current("main", "window.content")
    assert rig.nav.enter("main", how="tab") is None
    assert rig.frame.domain_id == "window"
