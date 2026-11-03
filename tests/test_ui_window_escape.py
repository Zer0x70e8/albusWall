#
"""窗口的 Esc 层级：先取消焦点，再收起叠加层；Shift+Esc 一步收起。

* ``Esc``（第一次）→ 焦点被取消（窗口内不再有焦点控件），叠加层不动；
* ``Esc``（焦点已取消时）→ 收起叠加层，回到空白占位页；
* ``Shift+Esc`` → 不看焦点，直接收起叠加层；
* 带 ``Ctrl`` / ``Alt`` / ``Meta`` 的 Esc 不参与这套层级。

Qt 需要 offscreen 平台才能在无显示环境跑；必须在 import PySide6 之前设好。
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")


# ==================== fixture ====================

@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app
    app.setStyleSheet("")


@pytest.fixture()
def window(qapp):
    """真实主窗口：显示出来，焦点落在内容网格上。"""
    from albuswall.ui.window import Window

    w = Window()
    w.resize(640, 480)
    w.show()
    # offscreen 下窗口不一定会被激活；本文件的断言只看窗口内部的焦点
    # （``QWidget.focusWidget()``），不依赖应用级的 active window。
    w.activateWindow()
    w.content.setFocus()
    qapp.processEvents()
    try:
        yield w
    finally:
        w.hide()
        w.close()
        w.deleteLater()
        qapp.processEvents()


def press_esc(widget, modifiers=None):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    mods = Qt.KeyboardModifier.NoModifier if modifiers is None else modifiers
    QTest.keyClick(widget, Qt.Key.Key_Escape, mods)


# ==================== Esc 两步 ====================

def test_esc_clears_focus_first(window, qapp):
    assert window.focusWidget() is window.content

    press_esc(window.content)

    assert window.focusWidget() is None
    assert window.overlay.is_blank() is True


def test_esc_clears_focus_inside_overlay_without_closing(window, qapp):
    window.show_album()
    album_view = window.album.album_view
    album_view.setFocus()
    qapp.processEvents()
    assert window.focusWidget() is album_view

    press_esc(album_view)

    assert window.focusWidget() is None
    assert window.overlay.currentWidget() is window.album


def test_second_esc_closes_overlay(window, qapp):
    window.show_album()
    window.album.album_view.setFocus()
    qapp.processEvents()

    press_esc(window.album.album_view)      # 第一步：取消焦点
    assert window.overlay.currentWidget() is window.album

    press_esc(window)                       # 第二步：焦点已取消 → 收起叠加层
    assert window.overlay.is_blank() is True


def test_esc_without_focus_closes_detail_overlay(window, qapp):
    window.overlay.setCurrentWidget(window.detail)
    window.detail.image_viewer.setFocus()
    qapp.processEvents()
    assert window.focusWidget() is window.detail.image_viewer

    press_esc(window.detail.image_viewer)   # 取消焦点
    assert window.overlay.currentWidget() is window.detail

    press_esc(window)                       # 收起叠加层
    assert window.overlay.is_blank() is True


# ==================== Shift+Esc 一步收起 ====================

def test_shift_esc_closes_overlay_without_touching_focus_first(window, qapp):
    from PySide6.QtCore import Qt

    window.show_album()
    album_view = window.album.album_view
    album_view.setFocus()
    qapp.processEvents()

    press_esc(album_view, Qt.KeyboardModifier.ShiftModifier)

    assert window.overlay.is_blank() is True


def test_shift_esc_on_blank_overlay_is_a_no_op(window, qapp):
    from PySide6.QtCore import Qt

    window.close_overlay()
    window.content.setFocus()
    qapp.processEvents()

    press_esc(window.content, Qt.KeyboardModifier.ShiftModifier)

    assert window.overlay.is_blank() is True
    assert window.focusWidget() is window.content


# ==================== 修饰键排除 ====================

def test_ctrl_esc_does_not_touch_focus_or_overlay(window, qapp):
    from PySide6.QtCore import Qt

    window.show_album()
    album_view = window.album.album_view
    album_view.setFocus()
    qapp.processEvents()

    press_esc(album_view, Qt.KeyboardModifier.ControlModifier)

    assert window.focusWidget() is album_view
    assert window.overlay.currentWidget() is window.album
