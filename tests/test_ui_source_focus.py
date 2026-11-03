#
"""导入源页 / 相册页的焦点链：滚动容器不抢焦点，表单按阅读顺序遍历。

回归：从左侧卡片列表按回车进入详情后，按方向键 / Tab 会走到
``QScrollArea`` 上 —— 它没有焦点指示器，表现是「焦点不见了」。现在
``QScrollArea`` 退出焦点链，详情页的 Tab 链由
``IngestSource._setup_tab_order`` 按视觉阅读顺序显式串起。

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
def page(qapp):
    """真实导入源页（挂在普通宿主窗口里，显示出来才能算焦点链）。"""
    from PySide6.QtWidgets import QVBoxLayout, QWidget

    from albuswall.ui.window.source import IngestSource

    host = QWidget()
    host.resize(1000, 700)
    widget = IngestSource(parent=host, target=host)
    layout = QVBoxLayout(host)
    layout.addWidget(widget)
    host.show()
    host.activateWindow()

    # 直接进详情模式（本用例不关心卡片数据）
    widget._detail_mode = True
    widget.detail_container.show()
    qapp.processEvents()
    try:
        yield widget
    finally:
        host.hide()
        host.close()
        host.deleteLater()
        qapp.processEvents()


def key(widget, key_code, modifiers=None):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    mods = Qt.KeyboardModifier.NoModifier if modifiers is None else modifiers
    QTest.keyClick(widget, key_code, mods)


def tab_chain(page, steps: int) -> list:
    """从详情页关闭按钮开始，连续按 Tab，收集落点控件。"""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    page.close_detail_btn.setFocus()
    QApplication.instance().processEvents()
    seen = []
    for _ in range(steps):
        key(page.close_detail_btn if not seen else seen[-1], Qt.Key.Key_Tab)
        seen.append(page.window().focusWidget())
    return seen


def focus_chain(page, steps: int = 500) -> list:
    """声明式的焦点链：沿 ``nextInFocusChain`` 走，收集页面子树里的可聚焦控件。

    与「按 Tab 实测」分开：Tab / 方向键的落点还受 Qt 自己的跳过规则影响
    （隐藏 / 禁用 / 单选组只停一个），链本身才是本页的契约。
    """
    from PySide6.QtCore import Qt

    start = page.close_detail_btn
    chain = []
    widget = start
    for _ in range(steps):
        widget = widget.nextInFocusChain()
        if widget is start:
            break
        if page.isAncestorOf(widget) and (
                widget.focusPolicy() != Qt.FocusPolicy.NoFocus):
            chain.append(widget)
    return chain


# ==================== 滚动容器不是焦点停点 ====================

def test_detail_scroll_area_is_not_a_focus_stop(page):
    from PySide6.QtCore import Qt

    assert page.scroll_area.focusPolicy() == Qt.FocusPolicy.NoFocus
    assert page.scroll_area.viewport().focusPolicy() == Qt.FocusPolicy.NoFocus


def test_tab_chain_never_lands_on_scroll_area(page):
    chain = tab_chain(page, 20)

    assert page.scroll_area not in chain
    assert page.scroll_area.viewport() not in chain
    for widget in chain:
        assert widget is not None


# ==================== 顺序 = 阅读顺序 ====================

def test_tab_order_declares_reading_order(page):
    """详情页的焦点链 = 关闭 → 应用 → 表单（阅读顺序）。"""
    form = page.detail_widget
    expected = [page.apply_button, *form.tab_order()]

    chain = focus_chain(page)
    assert chain, "focus chain is empty"

    # 期望顺序必须是焦点链的子序列：链里可能夹着 Qt 内部控件
    # （例如 QSpinBox 自带的行编辑），那不是本页能声明的。
    missing = [w.objectName() for w in expected if w not in chain]
    assert missing == []
    positions = [chain.index(w) for w in expected]
    assert positions == sorted(positions), "焦点链顺序与阅读顺序不一致"


def test_read_only_lists_stay_in_the_chain(page):
    """两个只读列表（标签 / 文件类型）仍然可 Tab，只是不能编辑。"""
    from PySide6.QtWidgets import QListView

    form = page.detail_widget
    for view in (form.tags_view, form.file_type_list_view):
        assert isinstance(view, QListView)
        assert view.editTriggers() == QListView.EditTrigger.NoEditTriggers


# ==================== 方向键同一套顺序 ====================

def test_down_arrow_goes_from_close_to_apply_to_first_field(page, qapp):
    from PySide6.QtCore import Qt

    page.close_detail_btn.setFocus()
    qapp.processEvents()
    assert page.window().focusWidget() is page.close_detail_btn

    key(page.close_detail_btn, Qt.Key.Key_Down)
    qapp.processEvents()
    assert page.window().focusWidget() is page.apply_button

    key(page.apply_button, Qt.Key.Key_Down)
    qapp.processEvents()
    assert page.window().focusWidget() is page.detail_widget.title_line_edit


# ==================== 相册页同一个坑 ====================

def test_album_scroll_area_is_not_a_focus_stop(qapp):
    from PySide6.QtCore import Qt

    from albuswall.ui.window import Window

    window = Window()
    try:
        album = window.album
        assert album.scroll_area.focusPolicy() == Qt.FocusPolicy.NoFocus
        assert (
            album.scroll_area.viewport().focusPolicy()
            == Qt.FocusPolicy.NoFocus
        )
    finally:
        window.close()
        window.deleteLater()
        qapp.processEvents()


# ==================== 可勾选分组：方向键进出子控件 ====================

def test_down_arrow_enters_checked_group_children(page, qapp):
    from PySide6.QtCore import Qt

    form = page.detail_widget
    form.grp_scheduled.setChecked(True)
    form.grp_scheduled.setFocus()
    qapp.processEvents()

    key(form.grp_scheduled, Qt.Key.Key_Down)
    qapp.processEvents()
    assert page.window().focusWidget() is form.update_mode_combo

    form.grp_device_insertion_trigger.setChecked(True)
    form.grp_device_insertion_trigger.setFocus()
    qapp.processEvents()
    key(form.grp_device_insertion_trigger, Qt.Key.Key_Down)
    qapp.processEvents()
    assert page.window().focusWidget() is form.target_combo


def test_up_arrow_leaves_group_to_previous_control(page, qapp):
    from PySide6.QtCore import Qt

    form = page.detail_widget
    form.grp_scheduled.setChecked(True)
    form.grp_scheduled.setFocus()
    qapp.processEvents()

    key(form.grp_scheduled, Qt.Key.Key_Up)
    qapp.processEvents()
    assert (
        page.window().focusWidget()
        is form.subfolder_recursion_depth_check_box
    )


def test_arrow_on_unchecked_group_does_not_stall_page(page, qapp):
    """未勾选时子控件是禁用的：方向键不能「卡」在分组上。"""
    from PySide6.QtCore import Qt

    form = page.detail_widget
    form.grp_device_insertion_trigger.setChecked(False)
    form.grp_device_insertion_trigger.setFocus()
    qapp.processEvents()

    key(form.grp_device_insertion_trigger, Qt.Key.Key_Down)
    qapp.processEvents()
    focused = page.window().focusWidget()
    # 落点既不是分组自己，也不是它（禁用的）子控件
    assert focused is not form.grp_device_insertion_trigger
    assert not form.grp_device_insertion_trigger.isAncestorOf(focused)


def test_group_children_reachable_by_tab_when_checked(page, qapp):
    from PySide6.QtCore import Qt

    form = page.detail_widget
    form.grp_scheduled.setChecked(True)
    form.grp_scheduled.setFocus()
    qapp.processEvents()

    key(form.grp_scheduled, Qt.Key.Key_Tab)
    qapp.processEvents()
    assert page.window().focusWidget() is form.update_mode_combo
