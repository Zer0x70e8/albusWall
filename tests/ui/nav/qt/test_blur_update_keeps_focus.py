#
"""``BlurLabel._update_blur``：自隐藏不该让焦点跑出这一页。

复现诊断报告的场景：相册页是 ``BlurLabel``（``Album(Retranslatable, BlurLabel)``），
刷新背景模糊时要先 ``setVisible(False)`` 再 ``grab()``，再 ``setVisible(True)``。
Qt 会在子树被隐藏时把焦点挪走 —— 页内控件失去焦点之后，如果没人还回去，
键盘光标就跳到页外（表现成「叠层刷新后焦点跳回主窗口」）。
"""

from __future__ import annotations

from PySide6.QtCore import Qt

from albuswall.ui.nav.wiring import NAV_PAGE_ID


def test_album_blur_refresh_keeps_the_focus_in_the_page(nav_rig):
    rig = nav_rig
    album = rig.window.album
    view = album.album_view

    rig.open_page("album")
    view.setFocus(Qt.FocusReason.OtherFocusReason)
    rig.app.processEvents()
    assert rig.app.focusWidget() is view

    # 焦点落在页内 → 反向同步把光标带进 album 域
    assert rig.frame.domain_id == "album"
    assert rig.current == "album.cover"

    album._update_blur()          # noqa: SLF001 - 直接跑那段 hide/grab/show
    rig.app.processEvents()

    focused = rig.app.focusWidget()
    assert focused is not None, "刷新后焦点跑丢了"
    assert focused is view or view.isAncestorOf(focused), (
        f"焦点离开了 album_view 子树：{focused!r}")

    # nav 的认知也没被这次刷新带偏
    assert rig.frame.domain_id == "album"
    assert rig.current == "album.cover"


def test_blur_refresh_leaves_foreign_focus_alone(nav_rig):
    """焦点不在本控件子树内时，刷新不去抢别人的焦点。"""
    rig = nav_rig
    album = rig.window.album
    rig.open_page("album")

    title_button = rig.window.title_bar.album_button
    title_button.setFocus(Qt.FocusReason.OtherFocusReason)
    rig.app.processEvents()
    assert rig.app.focusWidget() is title_button

    album._update_blur()          # noqa: SLF001
    rig.app.processEvents()

    assert rig.app.focusWidget() is title_button
