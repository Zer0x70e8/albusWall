#
"""``NavPresenter`` 作为分发器：受控执行、回滚、回灌、域内 Tab。

与 ``tests/test_ui_nav_presenter.py`` 的分工：

* 那边测**分派顺序与决策**（快捷键 / 让渡 / 动作 / 冒泡），焦点用记录器代替；
* 这边测 **Phase C 的执行语义**：注入的 ``QtFocusBackend``、``focus_node``
  失败回滚、焦点被抢走后拉回、Tab 接管（Qt 焦点链失效）、域内 Tab 遍历、
  挂起时 nav 动作返回 ``None``。

全部在 ``QT_QPA_PLATFORM=offscreen`` 下跑：真实窗口、真实焦点，但没有合成器。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from albuswall.core.container import Container  # noqa: E402
from albuswall.ui.nav.backend import NullFocusBackend  # noqa: E402
from albuswall.ui.nav.model import Direction  # noqa: E402
from albuswall.ui.nav.qt import QtFocusBackend  # noqa: E402
from albuswall.ui.nav.registry import NavRegistry  # noqa: E402
from albuswall.ui.nav.wiring import (  # noqa: E402
    NAV_PAGE_ID,
    register_window_nav,
)

pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QPushButton  # noqa: E402

from albuswall.ui.keymap import ShortcutRegistry  # noqa: E402
from albuswall.ui.presenter.nav_presenter import NavPresenter  # noqa: E402
from albuswall.ui.window import Window  # noqa: E402


# ==================== fixtures ====================

@pytest.fixture(scope="module")
def qapp() -> Iterator[QApplication]:
    app = QApplication.instance() or QApplication([])
    yield app


class Rig:
    """窗口 + 注册表 + 已启动的分发器（真实焦点）。"""

    def __init__(self, app: QApplication, window: Window,
                 nav: NavRegistry, presenter: NavPresenter) -> None:
        self.app = app
        self.window = window
        self.nav = nav
        self.presenter = presenter

    # ---- 键盘 ----
    def handle(self, key: Qt.Key,
               modifiers: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier,
               ) -> bool:
        """只问分派结果，不发事件。"""
        return self.presenter.handle_key_event(
            QKeyEvent(QEvent.Type.KeyPress, key, modifiers))

    def press(self, key: Qt.Key,
              modifiers: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier,
              ) -> bool:
        """走真实事件链：``sendEvent`` → 应用级 eventFilter → nav。"""
        target = self.app.focusWidget() or self.window
        event = QKeyEvent(QEvent.Type.KeyPress, key, modifiers)
        QApplication.sendEvent(target, event)
        self.app.processEvents()
        return event.isAccepted()

    # ---- 状态 ----
    @property
    def frame(self):
        return self.nav.current_frame(NAV_PAGE_ID)

    @property
    def current(self) -> str | None:
        return None if self.frame is None else self.frame.current

    def focused_widget(self) -> Any:
        self.app.processEvents()
        return self.app.focusWidget()

    def reset(self) -> None:
        self.window.overlay.show_blank()
        self.app.processEvents()
        register_window_nav(self.window, registry=self.nav)
        # 重新 wiring 之后把真实后端注入回去（register 不动后端，但保持显式）
        self.presenter.refresh()
        self.nav.set_backend(QtFocusBackend(
            focus_getter=self.presenter.target_widget,
            node_id_resolver=self.presenter.node_id_for_widget,
        ))
        self.nav.resume(NAV_PAGE_ID)

    def land(self, node_id: str, widget: Any = None) -> None:
        """把光标放到某个节点上，并把真实焦点交给它的控件。"""
        frame = self.nav.current_frame(NAV_PAGE_ID)
        domain_id = next(
            d.id for d in self.nav.domains() if node_id in d.nodes)
        if frame is not None and frame.domain_id != domain_id:
            self.presenter.sync_current(NAV_PAGE_ID, node_id)
        self.nav.set_current(NAV_PAGE_ID, node_id)
        target = widget if widget is not None else self.nav.get_node(node_id).widget
        target.setFocus(Qt.FocusReason.OtherFocusReason)
        self.app.processEvents()


@pytest.fixture(scope="module")
def _rig(qapp: QApplication) -> Iterator[Rig]:
    window = Window()
    window.resize(900, 700)
    window.show()
    qapp.processEvents()

    nav = NavRegistry()
    register_window_nav(window, registry=nav)

    container = Container()
    container.set("nav_registry", nav)
    container.set("shortcuts", ShortcutRegistry())
    presenter = NavPresenter(container, window=window)
    presenter.start()

    rig = Rig(qapp, window, nav, presenter)
    try:
        yield rig
    finally:
        presenter.stop()
        window.close()


@pytest.fixture
def rig(_rig: Rig) -> Rig:
    _rig.reset()
    return _rig


# ==================== 后端注入 ====================

def test_presenter_injects_the_qt_backend(rig):
    assert isinstance(rig.nav.backend, QtFocusBackend)


def test_stop_restores_the_default_backend(rig):
    rig.presenter.stop()
    try:
        assert isinstance(rig.nav.backend, NullFocusBackend)
    finally:
        rig.presenter.start()


def test_focus_node_verification_is_live(rig):
    """方向键落点由真实焦点验证：光标与 Qt 焦点一致。"""
    rig.land("titlebar.album")

    assert rig.press(Qt.Key.Key_Right) is True
    assert rig.current == "titlebar.search"
    assert rig.focused_widget() is rig.window.title_bar.search_button


# ==================== 失败回滚 ====================

def test_failed_focus_rolls_back_and_bubbles(rig):
    """``focus_node`` 报失败：光标回滚到动作前，按键不消费（冒泡）。"""
    rig.land("titlebar.album")
    other = rig.window.title_bar.close_button

    # 后端永远只看得见另一个控件 → 验证必然失败
    rig.nav.set_backend(QtFocusBackend(focus_getter=lambda: other))
    try:
        assert rig.handle(Qt.Key.Key_Right) is False
        assert rig.current == "titlebar.album"     # 回滚成功
    finally:
        rig.reset()


def test_rolled_back_move_does_not_strand_the_cursor(rig):
    """失败回滚之后，下一次按键仍然从原光标继续（不是原地卡死）。"""
    rig.land("titlebar.album")
    rig.nav.set_backend(QtFocusBackend(
        focus_getter=lambda: rig.window.title_bar.close_button))
    try:
        assert rig.handle(Qt.Key.Key_Right) is False
    finally:
        rig.reset()

    rig.land("titlebar.album")
    assert rig.press(Qt.Key.Key_Right) is True
    assert rig.current == "titlebar.search"


# ==================== Tab 接管 ====================

def test_tab_is_consumed_and_nav_moves_the_cursor(rig):
    """区域入口上 Tab：nav 进域，Qt 焦点链不参与。"""
    rig.land("window.titlebar")

    assert rig.press(Qt.Key.Key_Tab) is True
    assert rig.frame.domain_id == "titlebar"
    assert rig.current == "titlebar.album"
    assert rig.focused_widget() is rig.window.title_bar.album_button


def test_tab_walks_inside_the_domain(rig):
    """域内 Tab：album → search → extra（Qt 自己的焦点链已被 nav 取代）。"""
    rig.land("titlebar.album")

    assert rig.press(Qt.Key.Key_Tab) is True
    assert rig.current == "titlebar.search"
    assert rig.focused_widget() is rig.window.title_bar.search_button

    assert rig.press(Qt.Key.Key_Tab) is True
    assert rig.current == "titlebar.extra"


def test_tab_at_domain_end_is_consumed_but_stays(rig):
    """域内最后一个节点上 Tab：消费掉但不动（v1 不跨域）。"""
    rig.land("titlebar.extra")

    # 标题栏是两行网格：Tab 顺序按 (row, col)，第 0 行走到头接第 1 行
    for expected in ("titlebar.minimize", "titlebar.maximize",
                     "titlebar.close"):
        assert rig.press(Qt.Key.Key_Tab) is True
        assert rig.current == expected

    assert rig.press(Qt.Key.Key_Tab) is True          # 末尾：消费但不动
    assert rig.current == "titlebar.close"
    assert rig.frame.domain_id == "titlebar"


def test_shift_tab_walks_back_then_exits(rig):
    rig.land("titlebar.search")

    assert rig.press(Qt.Key.Key_Backtab,
                     Qt.KeyboardModifier.ShiftModifier) is True
    assert rig.current == "titlebar.album"

    # 已经是第一个 → 弹栈出域
    assert rig.press(Qt.Key.Key_Backtab,
                     Qt.KeyboardModifier.ShiftModifier) is True
    assert rig.frame.domain_id == "window"


def test_tab_does_not_move_focus_inside_a_leaf_domain(rig):
    """叶域里 Tab 仍归 nav（不让渡）：光标不动，Qt 焦点也不动。"""
    rig.presenter.sync_current(NAV_PAGE_ID, "content.body")
    rig.land("content.body")
    before = rig.focused_widget()

    assert rig.press(Qt.Key.Key_Tab) is True
    assert rig.current == "content.body"
    assert rig.focused_widget() is before


# ==================== 焦点跑到地盘外 → 只告警，不拉回 ====================

def test_focus_escaped_to_a_foreign_widget_is_not_pulled_back(rig, caplog):
    """``setFocus`` 到没有 nav 节点的控件：焦点留着，nav 状态不动，只 warning。"""
    import logging

    rig.land("titlebar.album")

    stray = QPushButton(rig.window)
    stray.setGeometry(0, 0, 10, 10)
    stray.show()
    try:
        with caplog.at_level(logging.WARNING,
                             logger="albuswall.ui.presenter"):
            stray.setFocus(Qt.FocusReason.OtherFocusReason)
            rig.app.processEvents()

        assert rig.current == "titlebar.album"
        # 硬接管：nav 不跟 Qt 抢焦点（旧实现会把它拽回 album_button）
        assert rig.focused_widget() is stray
        assert any("unregistered widget" in record.getMessage()
                   for record in caplog.records)
    finally:
        stray.deleteLater()
        rig.app.processEvents()


def test_focus_on_a_nav_widget_still_syncs_the_cursor(rig):
    """正常回灌不受「拉回」影响：焦点落在注册控件上就对齐光标。"""
    rig.land("titlebar.album")

    rig.window.title_bar.extra_button.setFocus(Qt.FocusReason.OtherFocusReason)
    rig.app.processEvents()

    assert rig.current == "titlebar.extra"


# ==================== 挂起 / 模态 ====================

def test_suspend_blocks_both_dispatch_and_nav_actions(rig):
    """挂起时：分派 False，且绕过分发器直接调 nav 也返回 ``None``。"""
    rig.land("titlebar.album")
    rig.presenter.suspend()
    try:
        assert rig.nav.is_suspended(NAV_PAGE_ID) is True
        assert rig.handle(Qt.Key.Key_Right) is False
        assert rig.nav.move(NAV_PAGE_ID, Direction.RIGHT) is None
        assert rig.current == "titlebar.album"
    finally:
        rig.presenter.resume()
    assert rig.nav.is_suspended(NAV_PAGE_ID) is False
    assert rig.press(Qt.Key.Key_Right) is True
