#
"""结构性断言：``focus_node`` 成功时，焦点必须落在 nav 认知内的控件上。

硬接管的核心不变量——nav 说「光标在这一格」，Qt 的焦点就必须在**属于这一格
（或某个已注册节点）**的控件上，不能落在一个 nav 不认识的控件上。这条断言
对 wiring 之后所有可见节点跑一遍，任何一处「focus_fn 指到了注册表外的控件」
都会红。
"""

from __future__ import annotations

from albuswall.ui.nav.qt import QtFocusBackend


def assert_focus_within_nav(nav, backend, node):
    """``focus_node`` 返回 True 时，焦点必须落在 nav 认知内。"""
    chain = backend.current_focus_chain()
    if not chain:
        return  # 无头环境不可观测
    focus_widget = chain[0]
    # 要么是 node.widget，要么能映射回某个已注册节点
    if focus_widget is node.widget:
        return
    node_id = backend.widget_to_node_id(focus_widget)
    assert node_id is not None, f"focus landed on unregistered widget: {focus_widget!r}"


def test_every_visible_node_lands_its_focus_within_nav(nav_rig):
    """对 wiring 后每个可见节点跑 ``focus_node``，逐条校验落点在 nav 认知内。"""
    rig = nav_rig
    backend = rig.nav.backend
    assert isinstance(backend, QtFocusBackend)

    checked = 0
    for domain in rig.nav.domains():
        for node in domain.nodes.values():
            if not rig.nav.is_visible(node):
                continue
            outcome = rig.nav.focus_node(node)
            rig.app.processEvents()
            if outcome is True:
                assert_focus_within_nav(rig.nav, backend, node)
                checked += 1

    assert checked > 0, "没有任何可见节点被检查（wiring 或可见性判定出问题）"


def test_the_assertion_catches_a_focus_fn_pointing_outside_nav(nav_rig):
    """反证：``focus_fn`` 把焦点交给注册表外的控件时，断言必须红。"""
    rig = nav_rig
    backend = rig.nav.backend
    from PySide6.QtWidgets import QPushButton

    stray = QPushButton(rig.window)
    stray.setGeometry(0, 0, 10, 10)
    stray.show()
    try:
        node = rig.nav.get_node("album.cover")
        object.__setattr__(node, "focus_fn", stray.setFocus)

        assert rig.nav.focus_node(node) is True       # focus_fn 路径不验证
        rig.app.processEvents()

        try:
            assert_focus_within_nav(rig.nav, backend, node)
        except AssertionError as error:
            assert "unregistered widget" in str(error)
        else:                                          # pragma: no cover
            raise AssertionError("结构性断言没能发现注册表外的落点")
    finally:
        stray.deleteLater()
        rig.app.processEvents()
