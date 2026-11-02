#
"""``albuswall.ui.nav.qt``：Qt 侧焦点执行后端的集成测试。

``QT_QPA_PLATFORM=offscreen``：真实 ``QWidget`` + 真实焦点，但没有合成器。
offscreen 平台会照常维护「窗口内焦点控件」，所以这些用例能验证
``focus_node`` 的**验证路径**（读焦点链）而不只是记录调用。
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from albuswall.ui.nav.model import Domain, Node  # noqa: E402
from albuswall.ui.nav.registry import NavRegistry  # noqa: E402

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from albuswall.ui.nav.backend import FocusBackend  # noqa: E402
from albuswall.ui.nav.qt import QtFocusBackend  # noqa: E402


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def window(qapp) -> QWidget:
    """一个带两个按钮的窗口（真实焦点，show 过才生效）。"""
    win = QWidget()
    layout = QVBoxLayout(win)
    first = QPushButton("first")
    second = QPushButton("second")
    layout.addWidget(first)
    layout.addWidget(second)
    win.show()
    qapp.processEvents()
    try:
        yield win
    finally:
        win.close()
        win.deleteLater()
        qapp.processEvents()


def test_qt_backend_satisfies_the_protocol():
    assert isinstance(QtFocusBackend(), FocusBackend)


def test_apply_focus_moves_real_focus(window, qapp):
    backend = QtFocusBackend(focus_getter=qapp.focusWidget)
    button = window.findChildren(QPushButton)[0]

    backend.apply_focus(Node(id="n", widget=button))
    qapp.processEvents()

    assert qapp.focusWidget() is button
    chain = backend.current_focus_chain()
    assert chain[0] is button
    assert window in chain            # 祖先链里有窗口


def test_current_focus_chain_is_empty_when_unobservable():
    """观测不到焦点（无 WM / 窗口未激活）→ 空元组，调用方据此跳过验证。"""
    backend = QtFocusBackend(focus_getter=lambda: None)
    assert backend.current_focus_chain() == ()


def test_current_focus_chain_tolerates_destroyed_widget():
    def boom():
        raise RuntimeError("wrapped C/C++ object deleted")

    assert QtFocusBackend(focus_getter=boom).current_focus_chain() == ()


def test_ensure_focusable_upgrades_nofocus_only(window):
    plain = QWidget(window)
    plain.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    button = window.findChildren(QPushButton)[0]
    backend = QtFocusBackend()

    backend.ensure_focusable(Node(id="plain", widget=plain))
    backend.ensure_focusable(Node(id="button", widget=button))

    assert plain.focusPolicy() == Qt.FocusPolicy.StrongFocus
    # 已经是焦点策略的控件不被改动（尤其不反向设 NoFocus，那会破坏 IME）
    assert button.focusPolicy() != Qt.FocusPolicy.NoFocus


def test_widget_to_node_id_uses_resolver(window):
    button = window.findChildren(QPushButton)[1]
    calls: list[object] = []

    def resolver(widget):
        calls.append(widget)
        return "second-node" if widget is button else None

    backend = QtFocusBackend(node_id_resolver=resolver)
    assert backend.widget_to_node_id(button) == "second-node"
    assert calls == [button]
    assert backend.widget_to_node_id(None) is None


def test_widget_to_node_id_without_resolver_is_none(window):
    assert QtFocusBackend().widget_to_node_id(
        window.findChildren(QPushButton)[0]) is None


def test_widget_to_node_id_swallows_destroyed_widget():
    def boom(_widget):
        raise RuntimeError("deleted")

    assert QtFocusBackend(node_id_resolver=boom).widget_to_node_id(object()) is None


def test_apply_focus_on_duck_typed_widget():
    """非 QWidget 的替身（只接受无参 setFocus）也要能用。"""
    class Duck:
        def __init__(self) -> None:
            self.calls = 0

        def setFocus(self):  # noqa: N802 - 对接 Qt 命名
            self.calls += 1

    duck = Duck()
    QtFocusBackend().apply_focus(Node(id="duck", widget=duck))
    assert duck.calls == 1


def test_apply_focus_without_set_focus_is_noop():
    QtFocusBackend().apply_focus(Node(id="plain", widget=object()))


def test_registry_focus_node_verifies_through_qt(window, qapp):
    """端到端：注册表 + 真实后端 → ``focus_node`` 验证通过并真的动了焦点。"""
    button = window.findChildren(QPushButton)[0]
    nav = NavRegistry(backend=QtFocusBackend(focus_getter=qapp.focusWidget))
    nav.register_domain(Domain(id="d"))
    node = nav.register_node(Node(id="n", widget=button), domain_id="d")
    nav.ensure_page("p", root_domain="d")

    assert nav.focus_node(node) is True
    qapp.processEvents()
    assert qapp.focusWidget() is button


def test_registry_focus_node_reports_failure_through_qt(window, qapp):
    """目标控件不在焦点链上（焦点被固定到别的控件）→ ``False``。"""
    other = window.findChildren(QPushButton)[1]
    other.setFocus()
    qapp.processEvents()

    nav = NavRegistry(
        backend=QtFocusBackend(focus_getter=lambda: other))
    nav.register_domain(Domain(id="d"))
    node = nav.register_node(
        Node(id="n", widget=window.findChildren(QPushButton)[0]),
        domain_id="d")
    nav.ensure_page("p", root_domain="d")

    assert nav.focus_node(node) is False
