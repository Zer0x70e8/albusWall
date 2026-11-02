#
"""焦点跑到 nav 的地盘外：只告警，不拉回。

旧的 ``_restore_nav_focus`` 会主动把焦点拽回 ``nav.current``。它掩盖了两件事：
focusChanged 的异常路径，以及「某个控件根本不在注册表里」这个结构问题。
硬接管之后 nav 不跟 Qt 抢焦点 —— 离开 nav 控件树的焦点原样留着，只留一条
``warning`` 当证据。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QPushButton


def _stray_button(rig) -> QPushButton:
    stray = QPushButton(rig.window)
    stray.setObjectName("StrayUnregisteredButton")
    stray.setGeometry(0, 0, 10, 10)
    stray.show()
    rig.app.processEvents()
    return stray


def test_stray_focus_only_warns(nav_rig, caplog):
    rig = nav_rig
    rig.land("titlebar.album")
    assert rig.current == "titlebar.album"

    stray = _stray_button(rig)
    try:
        with caplog.at_level(
                logging.WARNING, logger="albuswall.ui.presenter"):
            stray.setFocus(Qt.FocusReason.OtherFocusReason)
            rig.app.processEvents()

        # 焦点没有被拉回：nav 不跟 Qt 抢
        assert rig.app.focusWidget() is stray
        # nav 状态也没被动过
        assert rig.current == "titlebar.album"
        assert rig.frame.domain_id == "titlebar"

        warnings = [r.getMessage() for r in caplog.records
                    if r.levelno >= logging.WARNING]
        assert any("unregistered widget" in message for message in warnings), (
            warnings)
    finally:
        stray.deleteLater()
        rig.app.processEvents()


def test_focus_on_a_nav_widget_still_syncs_the_cursor(nav_rig, caplog):
    """回归：正常回灌（焦点落在已注册控件上）仍然对齐 ``nav.current``。"""
    rig = nav_rig
    rig.land("titlebar.album")

    with caplog.at_level(logging.WARNING, logger="albuswall.ui.presenter"):
        rig.window.title_bar.extra_button.setFocus(
            Qt.FocusReason.OtherFocusReason)
        rig.app.processEvents()

    assert rig.current == "titlebar.extra"
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_hidden_page_focus_does_not_move_the_cursor(nav_rig):
    """隐藏页里的控件（页面没开）：光标不动，也不报错。"""
    rig = nav_rig
    rig.land("titlebar.album")

    rig.window.detail.image_viewer.setFocus(Qt.FocusReason.OtherFocusReason)
    rig.app.processEvents()

    assert rig.current == "titlebar.album"
    assert rig.frame.domain_id == "titlebar"
