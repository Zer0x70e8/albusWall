#
"""关掉 overlay 页之后，焦点必须由 nav 决定落点（硬接管闭环）。

以前这条链是漏的：``QStackedLayout`` 换页时若旧页持有焦点，会把焦点**显式转给
新页**（程序化 ``setFocus`` 无视 ``NoFocus`` 策略），于是一收起相册页，焦点就
落进 ``PassthroughStack`` 的空白占位页 ``_PassthroughPage`` —— 一个 nav 不认识
的控件，分发器只能事后 warning。

闭环由两半组成，缺一不可：

1. **视图侧**（``PassthroughStack._release_focus``）：切页前先把焦点从旧页交出
   去，Qt 那一步「转给新页」就不会发生，占位页永远拿不到焦点；
2. **分发器侧**（``NavPresenter._on_overlay_page_changed``）：关页后经 nav 弹回
   根帧、把焦点交给确定的区域入口（内容层 → 标题栏兜底），**不直接 setFocus**。
"""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import Qt

from albuswall.ui.nav.qt import QtFocusBackend
from albuswall.ui.nav.wiring import NAV_PAGE_ID


def _inner_pages(rig) -> list[Any]:
    """overlay 栈里的全部占位 / 页控件（用来断言焦点不在其中）。"""
    overlay = rig.window.overlay
    pages = [overlay.blank_page]
    for index in range(overlay.count()):
        widget = overlay.widget(index)
        if widget is not None:
            pages.append(widget)
    return pages


def test_closing_the_album_page_hands_focus_back_to_nav(nav_rig):
    rig = nav_rig
    rig.open_page("album")
    rig.window.album.album_view.setFocus(Qt.FocusReason.OtherFocusReason)
    rig.app.processEvents()
    assert rig.frame.domain_id == "album"

    rig.window.close_overlay()
    rig.app.processEvents()

    focused = rig.app.focusWidget()
    assert focused is not None, "关页之后焦点凭空消失了"
    assert focused not in _inner_pages(rig), (
        f"焦点落在 overlay 的占位页 / 页控件上：{focused!r}")

    # 焦点落在 nav 注册过的控件上
    backend = rig.nav.backend
    assert isinstance(backend, QtFocusBackend)
    assert backend.widget_to_node_id(focused) is not None, (
        f"焦点落在未注册控件上：{focused!r}")

    # 光标离开 album 域，落到父域里的确定入口
    assert rig.frame.domain_id != "album"
    assert rig.current is not None
    assert rig.nav.is_visible(rig.nav.get_node(rig.current)) is True


def test_closing_the_album_page_does_not_trip_the_stray_focus_probe(
        nav_rig, caplog,
):
    """硬接管的探针（``_report_stray_focus``）不该被这条正常路径触发。"""
    rig = nav_rig
    rig.open_page("album")
    rig.window.album.album_view.setFocus(Qt.FocusReason.OtherFocusReason)
    rig.app.processEvents()

    with caplog.at_level(logging.WARNING, logger="albuswall.ui.presenter"):
        rig.window.close_overlay()
        rig.app.processEvents()

    strays = [record.getMessage() for record in caplog.records
              if "unregistered widget" in record.getMessage()]
    assert not strays, strays


def test_the_blank_page_never_receives_focus(nav_rig):
    """占位页本身也拿不到焦点（视图侧防线）。"""
    rig = nav_rig
    rig.open_page("detail")
    rig.window.detail.image_viewer.setFocus(Qt.FocusReason.OtherFocusReason)
    rig.app.processEvents()

    seen: list[Any] = []
    slot = lambda _old, new: seen.append(new)  # noqa: E731
    rig.app.focusChanged.connect(slot)
    try:
        rig.window.close_overlay()
        rig.app.processEvents()
    finally:
        rig.app.focusChanged.disconnect(slot)

    blank = rig.window.overlay.blank_page
    assert blank not in seen, "空白占位页曾经拿到过焦点"
    assert rig.app.focusWidget() is not blank


def test_closing_a_nested_page_lands_on_the_revealed_content(nav_rig):
    """从相册页的嵌套域退出：光标回根帧，焦点落在内容层入口。"""
    rig = nav_rig
    rig.open_page("album")
    rig.land("album.close")
    assert rig.frame.domain_id == "album"

    rig.window.close_overlay()
    rig.app.processEvents()

    # 策略锁定（不是语义锁定）：当前默认落点是内容层入口 → content.body，
    # 见 docs/nav.md「overlay 关页落点（默认策略）」。策略变更时同步改断言。
    assert rig.frame.domain_id == "content"
    assert rig.current == "content.body"
    assert rig.app.focusWidget() is rig.window.content
    assert rig.nav.current_frame(NAV_PAGE_ID).domain_id == "content"
