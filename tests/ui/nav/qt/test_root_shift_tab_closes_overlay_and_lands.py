#
"""端到端：根帧 Shift+Tab → 关页 → 焦点落点（完整链路）。

覆盖的是**整条链**，不是某一环：

```
真实 KeyPress(Backtab) → 应用级 eventFilter → NavPresenter.handle_key_event
  → _handle_tab（域内反向 / 出域 / 根帧分支）→ _close_current_overlay()
  → Window.close_overlay() → PassthroughStack.show_blank()（_release_focus）
  → overlay.currentChanged → _on_overlay_page_changed()
  → exit_back 回根帧 → 落确定的区域入口 → focus_node
```

配套的 `test_passthrough_page_no_focus.py` 只从 `_on_overlay_page_changed`
被触发之后开始；这里从**按键**开始，并且刻意全程走真实的
`QApplication.sendEvent`（`rig.press`），**不直接调 `_handle_tab`** —— 内部方法
只为覆盖链路而存在，测的是「用户按了 Shift+Tab 之后世界是什么样」。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt

from albuswall.ui.nav.qt import QtFocusBackend

_SHIFT = Qt.KeyboardModifier.ShiftModifier


def test_shift_tab_from_inside_the_page_closes_the_overlay_and_lands_nav(
        nav_rig, caplog,
):
    rig = nav_rig
    window = rig.window

    # 1) 打开相册页（走窗口的公开钩子，真实交互路径）
    window.toggle_album()
    rig.app.processEvents()
    assert window.overlay.is_blank() is False

    # 2) 鼠标点进页内：真实焦点 + focusChanged 反向同步 → 光标进 album 域
    window.album.album_view.setFocus(Qt.FocusReason.OtherFocusReason)
    rig.app.processEvents()
    assert rig.frame.domain_id == "album"
    assert rig.current == "album.cover"

    # 3) 一路 Shift+Tab：先在 album 域内反向遍历（tab_prev）到域首，再出域回到
    #    根帧，最后一下命中根帧分支 → 关页钩子。每一跳都必须被 nav 消费
    #    （不冒泡给 Qt 的焦点链）。
    domains_seen: list[str] = []
    with caplog.at_level(logging.WARNING, logger="albuswall.ui.presenter"):
        presses = 0
        while not window.overlay.is_blank() and presses < 8:
            assert rig.press(Qt.Key.Key_Backtab, _SHIFT) is True
            presses += 1
            domains_seen.append(rig.frame.domain_id)
    assert presses < 8, "Shift+Tab 没能关页（弹到根帧之后没有触发关页钩子）"
    # 链路形状：域内反向遍历（album）→ 出域回到根帧（window）→ 关页后由 nav
    # 重新落点（content）。三跳的顺序就是「弹到根帧 → 关页 → 落点」。
    assert "album" in domains_seen and "window" in domains_seen, domains_seen
    assert domains_seen.index("album") < domains_seen.index("window")

    # 4) 断言链
    assert window.overlay.is_blank() is True, "Shift+Tab 到根帧后没有关页"

    focused = rig.app.focusWidget()
    assert focused is not None, "关页之后焦点凭空消失了"
    backend = rig.nav.backend
    assert isinstance(backend, QtFocusBackend)
    assert backend.widget_to_node_id(focused) is not None, (
        f"焦点落在未注册控件上：{focused!r}")

    # 策略锁定（不是语义锁定）：当前默认落点是「内容层入口 → content.body」，
    # 见 docs/nav.md「overlay 关页落点（默认策略）」。落点策略变了就同步改这里。
    assert rig.current == "content.body"
    assert rig.frame.domain_id == "content"

    strays = [record.getMessage() for record in caplog.records
              if "unregistered widget" in record.getMessage()]
    assert not strays, strays


def test_the_whole_chain_never_lets_qt_steal_the_focus(nav_rig):
    """链路中每一跳之后，焦点都在 nav 注册的控件上（探针全程静默）。"""
    rig = nav_rig
    window = rig.window
    window.toggle_album()
    rig.app.processEvents()
    window.album.album_view.setFocus(Qt.FocusReason.OtherFocusReason)
    rig.app.processEvents()

    backend = rig.nav.backend
    assert isinstance(backend, QtFocusBackend)

    observed: list[tuple[str, object]] = []

    def slot(_old, new) -> None:
        observed.append(("changed", new))

    rig.app.focusChanged.connect(slot)
    try:
        while not window.overlay.is_blank():
            assert rig.press(Qt.Key.Key_Backtab, _SHIFT) is True
    finally:
        rig.app.focusChanged.disconnect(slot)

    # 出现过焦点为 None（视图侧 clearFocus 交出去的那一瞬）是允许的：落点随后
    # 由 nav 补上。但**不允许**出现「停在 overlay 内部占位 / 页控件上」。
    overlay_pages = {window.overlay.blank_page} | {
        window.overlay.widget(index)
        for index in range(window.overlay.count())
    }
    for _kind, widget in observed:
        if widget is None:
            continue
        assert widget not in overlay_pages, (
            f"焦点停在 overlay 内部控件上：{widget!r}")

    final = rig.app.focusWidget()
    assert final is not None
    assert backend.widget_to_node_id(final) is not None
