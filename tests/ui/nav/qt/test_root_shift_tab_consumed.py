#
"""根帧 Shift+Tab 必须被 nav 消费，不许落回 Qt 的焦点链。

诊断报告的场景：光标在页面根帧（虚拟根）上按 Shift+Tab，旧实现 ``return
False`` 冒泡，``QWidget::focusNextPrevChild`` 接手，把焦点甩到主窗口自己的
焦点链上（第一个 / 最后一个可聚焦控件）。硬接管之后这条路径必须消失：
事件被消费，``nav.current`` 与 ``QApplication.focusWidget()`` 都不动。
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt

from albuswall.ui.nav.wiring import NAV_PAGE_ID

_SHIFT = Qt.KeyboardModifier.ShiftModifier


def _record_focus_changes(rig) -> tuple[list[Any], Any]:
    """记录 ``focusChanged`` 的 new-widget（返回列表 + 用于断连的槽）。"""
    seen: list[Any] = []

    def slot(_old: Any, new: Any) -> None:
        seen.append(new)

    rig.app.focusChanged.connect(slot)
    return seen, slot


def test_shift_tab_at_the_root_frame_is_consumed(nav_rig):
    rig = nav_rig
    button = rig.window.title_bar.album_button
    button.setFocus(Qt.FocusReason.OtherFocusReason)
    rig.app.processEvents()
    assert rig.app.focusWidget() is button

    # 焦点回灌会把光标推进 titlebar 域；显式退回根帧虚拟根
    rig.nav.jump_to_page_root(NAV_PAGE_ID)
    assert rig.current is None
    before_focus = rig.focused_widget()

    seen, slot = _record_focus_changes(rig)
    try:
        assert rig.press(Qt.Key.Key_Backtab, _SHIFT) is True
    finally:
        rig.app.focusChanged.disconnect(slot)

    # 光标不动、焦点不动
    assert rig.current is None
    assert rig.frame.domain_id == "window"
    assert rig.focused_widget() is before_focus

    # 没有把焦点甩到一个 nav 不认识的控件上
    for widget in seen:
        if widget is None:
            continue
        assert rig.presenter.node_id_for_widget(widget) is not None, (
            f"Shift+Tab 把焦点交给了未注册控件：{widget!r}")


def test_shift_tab_at_the_root_does_not_walk_qt_focus_chain(nav_rig):
    """根帧 Shift+Tab 不触发 Qt 的焦点链（对比：不拦截时焦点会跑到别处）。"""
    rig = nav_rig
    rig.nav.jump_to_page_root(NAV_PAGE_ID)
    before = rig.focused_widget()

    assert rig.press(Qt.Key.Key_Backtab, _SHIFT) is True
    assert rig.current is None
    assert rig.focused_widget() is before


def test_shift_tab_inside_an_overlay_page_pops_to_the_parent_frame(nav_rig):
    """叠层页内 Shift+Tab：``exit_back`` 正常弹到父帧，不落到 Qt 原生链。"""
    rig = nav_rig
    rig.open_page("album")
    rig.land("album.close")
    assert rig.frame.domain_id == "album"

    seen, slot = _record_focus_changes(rig)
    try:
        assert rig.press(Qt.Key.Key_Backtab, _SHIFT) is True
    finally:
        rig.app.focusChanged.disconnect(slot)

    assert rig.frame.domain_id == "window"
    # 焦点仍在 nav 认识的控件上（页内 → 区域入口的落点控件）
    focused = rig.focused_widget()
    if focused is not None:
        assert rig.presenter.node_id_for_widget(focused) is not None
    for widget in seen:
        if widget is None:
            continue
        assert rig.presenter.node_id_for_widget(widget) is not None


def test_shift_tab_closes_the_overlay_page_at_the_root(nav_rig):
    """根帧 Shift+Tab 的关页钩子：有页就关，焦点落点由 nav 决定。"""
    rig = nav_rig
    rig.open_page("album")
    rig.nav.jump_to_page_root(NAV_PAGE_ID)
    assert rig.current is None
    assert rig.window.overlay.is_blank() is False

    assert rig.press(Qt.Key.Key_Backtab, _SHIFT) is True
    rig.app.processEvents()
    assert rig.window.overlay.is_blank() is True
    # 关页之后焦点交给确定的父区域入口（内容层），光标随之离开虚拟根。
    # 策略锁定：默认落点 content.body，见 docs/nav.md「overlay 关页落点」。
    assert rig.frame.domain_id == "content"
    assert rig.current == "content.body"
    assert rig.focused_widget() is rig.window.content

    # 已经是空白层：Shift+Tab 仍然被消费（弹回根帧，光标停在内容层入口）
    assert rig.press(Qt.Key.Key_Backtab, _SHIFT) is True
    assert rig.frame.domain_id == "window"
    assert rig.current == "window.content"
