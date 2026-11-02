#
"""``tests/ui/nav/qt`` 的共享装置：真实窗口 + 真实焦点 + 已启动的分发器。

与 ``tests/test_ui_nav_presenter.py`` 的 ``Rig`` 差别：那边的焦点是**记录器**
（无 WM 环境里 ``focusWidget()`` 常为 ``None``，用例自己指定「焦点在哪」）；
这里的 ``nav_rig`` 用**真实焦点**（``QT_QPA_PLATFORM=offscreen`` 下 offscreen
平台照常维护窗口内焦点），因此能验证「隐藏再显示之后焦点还在不在」「Qt 的
焦点链有没有被绕开」这类只有真焦点才看得出来行为。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from albuswall.core.container import Container  # noqa: E402
from albuswall.ui.nav.registry import NavRegistry  # noqa: E402
from albuswall.ui.nav.wiring import (  # noqa: E402
    NAV_PAGE_ID,
    register_window_nav,
)

pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from albuswall.ui.keymap import ShortcutRegistry  # noqa: E402
from albuswall.ui.presenter.nav_presenter import NavPresenter  # noqa: E402
from albuswall.ui.window import Window  # noqa: E402


@pytest.fixture(scope="session")
def qapp() -> Iterator[QApplication]:
    app = QApplication.instance() or QApplication([])
    yield app


class NavRig:
    """窗口 + 注册表 + 已启动的分发器（真实焦点）。"""

    def __init__(
            self,
            app: QApplication,
            window: Window,
            nav: NavRegistry,
            presenter: NavPresenter,
    ) -> None:
        self.app = app
        self.window = window
        self.nav = nav
        self.presenter = presenter

    # ---- 键盘 ----
    def handle(
            self,
            key: Qt.Key,
            modifiers: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier,
    ) -> bool:
        """只问分派结果，不发事件。"""
        return self.presenter.handle_key_event(
            QKeyEvent(QEvent.Type.KeyPress, key, modifiers))

    def press(
            self,
            key: Qt.Key,
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

    def open_page(self, page: str) -> None:
        """打开 overlay 的某一页（``album`` / ``source`` / ``detail``）。"""
        self.window.overlay.setCurrentWidget(getattr(self.window, page))
        self.app.processEvents()

    def close_page(self) -> None:
        self.window.close_overlay()
        self.app.processEvents()

    def land(self, node_id: str, widget: Any = None) -> None:
        """把光标放到某个节点上，并把真实焦点交给它的控件。"""
        frame = self.nav.current_frame(NAV_PAGE_ID)
        domain_id = next(
            d.id for d in self.nav.domains() if node_id in d.nodes)
        if frame is not None and frame.domain_id != domain_id:
            self.presenter.sync_current(NAV_PAGE_ID, node_id)
        self.nav.set_current(NAV_PAGE_ID, node_id)
        node = self.nav.get_node(node_id)
        target = widget if widget is not None else node.widget
        target.setFocus(Qt.FocusReason.OtherFocusReason)
        self.app.processEvents()


@pytest.fixture
def nav_rig(qapp: QApplication) -> Iterator[NavRig]:
    """每个用例一套干净的窗口 / 注册表 / 分发器（用例之间不留状态）。"""
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

    rig = NavRig(qapp, window, nav, presenter)
    try:
        yield rig
    finally:
        presenter.stop()
        window.close()
        window.deleteLater()
        qapp.processEvents()


__all__ = ["NavRig", "nav_rig", "qapp"]
