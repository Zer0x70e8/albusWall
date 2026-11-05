#
"""键盘激活键（空格 / 回车）统一 + ``Menu`` 键打开菜单的规范。

对应实现：

* :mod:`albuswall.ui.utils.activation_keys` —— 应用级事件过滤器；
* :mod:`albuswall.ui.widgets.tile_view` —— 内容网格的唯一例外
  （空格 = 预览、回车 = 详情，``space_previews`` 标记让过滤器放行）；
* :meth:`albuswall.ui.window.detail.Detail.eventFilter` —— 预览模式的退出键。
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


@pytest.fixture
def filtered_app(qapp):
    """装好激活键过滤器的应用（幂等：同一进程里只装一个）。"""
    from albuswall.ui.utils import install_activation_key_filter

    install_activation_key_filter(qapp)
    return qapp


def _dispose(widget):
    """收起并销毁顶层测试控件。

    测试里 show() 过的**顶层**控件在 offscreen 平台会一直参与
    ``QApplication.widgetAt`` 的命中测试；不 hide 就 deleteLater 会留下
    「看不见但还在」的窗口，污染其它用例。这里显式 hide + close。
    """
    from PySide6.QtWidgets import QApplication

    widget.hide()
    widget.close()
    widget.deleteLater()
    app = QApplication.instance()
    if app is not None:
        app.processEvents()


def key_click(widget, key, modifiers=None):
    """把一次按键同时投给应用过滤器与目标控件。"""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    mods = Qt.KeyboardModifier.NoModifier if modifiers is None else modifiers
    QTest.keyClick(widget, key, mods)


# ==================== 按钮：回车 = 空格 ====================

def test_plain_button_enter_and_space_both_click(filtered_app):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QPushButton

    button = QPushButton("确定")
    clicks: list[int] = []
    button.clicked.connect(lambda: clicks.append(1))
    button.show()
    try:
        key_click(button, Qt.Key.Key_Return)
        key_click(button, Qt.Key.Key_Enter)
        key_click(button, Qt.Key.Key_Space)
        assert len(clicks) == 3
    finally:
        _dispose(button)


def test_checkable_button_enter_toggles_like_space(filtered_app):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QPushButton

    button = QPushButton("收藏")
    button.setCheckable(True)
    button.show()
    try:
        key_click(button, Qt.Key.Key_Return)
        assert button.isChecked() is True
        key_click(button, Qt.Key.Key_Space)
        assert button.isChecked() is False
    finally:
        _dispose(button)


def test_enter_is_not_hijacked_when_ctrl_is_held(filtered_app):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QPushButton

    button = QPushButton("确定")
    clicks: list[int] = []
    button.clicked.connect(lambda: clicks.append(1))
    button.show()
    try:
        key_click(button, Qt.Key.Key_Return,
                  Qt.KeyboardModifier.ControlModifier)
        assert clicks == []
    finally:
        _dispose(button)


def test_enter_keeps_dialog_default_button_semantics(filtered_app):
    """对话框里的回车仍是「默认按钮」：焦点在取消按钮上也不改成点取消。"""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QDialog, QHBoxLayout, QPushButton

    dialog = QDialog()
    layout = QHBoxLayout(dialog)
    ok = QPushButton("确定", dialog)
    cancel = QPushButton("取消", dialog)
    ok.setDefault(True)
    ok.setAutoDefault(True)
    cancel.setAutoDefault(False)
    layout.addWidget(ok)
    layout.addWidget(cancel)

    calls: list[str] = []
    ok.clicked.connect(lambda: calls.append("ok"))
    cancel.clicked.connect(lambda: calls.append("cancel"))
    dialog.show()
    try:
        cancel.setFocus()
        key_click(cancel, Qt.Key.Key_Return)
        assert calls != ["cancel"]
    finally:
        dialog.close()
        _dispose(dialog)


# ==================== 列表 / 网格：空格 = activated ====================

def test_item_view_space_activates_current_item(filtered_app):
    from PySide6.QtCore import QStringListModel, Qt
    from PySide6.QtWidgets import QListView

    view = QListView()
    model = QStringListModel(["a", "b", "c"])
    view.setModel(model)
    view.setCurrentIndex(model.index(1, 0))

    activated: list[int] = []
    view.activated.connect(lambda index: activated.append(index.row()))
    view.show()
    try:
        key_click(view, Qt.Key.Key_Space)
        key_click(view, Qt.Key.Key_Return)
        assert activated == [1, 1]
    finally:
        _dispose(view)


def test_item_view_without_current_item_leaves_space_alone(filtered_app):
    from PySide6.QtCore import QStringListModel, Qt
    from PySide6.QtWidgets import QListView

    view = QListView()
    view.setModel(QStringListModel([]))
    activated: list[int] = []
    view.activated.connect(lambda index: activated.append(index.row()))
    view.show()
    try:
        key_click(view, Qt.Key.Key_Space)
        assert activated == []
    finally:
        _dispose(view)


# ==================== Menu 键 / Shift+F10 打开菜单 ====================

def _menu_button():
    from PySide6.QtWidgets import QMenu, QPushButton

    button = QPushButton("更多")
    menu = QMenu(button)
    menu.addAction("导入源")
    menu.addAction("退出")
    button.setMenu(menu)
    button.show()
    return button, menu


def test_menu_key_opens_associated_menu(filtered_app):
    from PySide6.QtCore import Qt

    button, menu = _menu_button()
    try:
        key_click(button, Qt.Key.Key_Menu)
        assert menu.isVisible() is True
    finally:
        menu.hide()
        _dispose(button)


def test_shift_f10_opens_associated_menu(filtered_app):
    from PySide6.QtCore import Qt

    button, menu = _menu_button()
    try:
        key_click(button, Qt.Key.Key_F10, Qt.KeyboardModifier.ShiftModifier)
        assert menu.isVisible() is True
    finally:
        menu.hide()
        _dispose(button)


def test_menu_key_without_menu_is_left_alone(filtered_app):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QPushButton

    button = QPushButton("无菜单")
    button.show()
    try:
        key_click(button, Qt.Key.Key_Menu)  # 不崩、不弹
        assert button.isVisible() is True
    finally:
        _dispose(button)


def test_enter_opens_menu_button_menu_like_space(filtered_app):
    from PySide6.QtCore import Qt

    button, menu = _menu_button()
    try:
        key_click(button, Qt.Key.Key_Return)
        assert menu.isVisible() is True
    finally:
        menu.hide()
        _dispose(button)


# ==================== 内容网格：空格 = 预览，回车 = 详情 ====================

def _grid():
    from albuswall.ui.model import AssetGridModel
    from albuswall.ui.widgets.tile_view import TileView

    model = AssetGridModel()
    for row in range(9):
        model.append_asset(f"a{row}")
    grid = TileView(column_count=3)
    grid.setModel(model)  # 视图不持有模型：挂到 grid 上防止被回收
    grid._test_model = model
    grid.resize(400, 300)
    grid.set_cursor_index(0)
    grid.show()
    return grid


def _empty_grid():
    from albuswall.ui.model import AssetGridModel
    from albuswall.ui.widgets.tile_view import TileView

    model = AssetGridModel()
    grid = TileView()
    grid.setModel(model)
    grid._test_model = model
    grid.resize(400, 300)
    grid.show()
    return grid


def test_grid_space_previews_and_enter_activates(filtered_app):
    from PySide6.QtCore import Qt

    grid = _grid()
    previewed: list[int] = []
    activated: list[int] = []
    grid.unit_previewed.connect(previewed.append)
    grid.unit_activated.connect(activated.append)
    try:
        key_click(grid, Qt.Key.Key_Space)
        key_click(grid, Qt.Key.Key_Return)
        assert previewed == [0]
        assert activated == [0]
    finally:
        _dispose(grid)


def test_grid_without_cursor_leaves_space_alone(filtered_app):
    from PySide6.QtCore import Qt

    grid = _empty_grid()  # 空网格：没有光标（-1）
    assert grid.cursor_index() == -1
    previewed: list[int] = []
    grid.unit_previewed.connect(previewed.append)
    try:
        key_click(grid, Qt.Key.Key_Space)
        assert previewed == []
    finally:
        _dispose(grid)


# ==================== 详情页：预览模式的退出键 ====================

def test_preview_mode_space_exits_and_enter_promotes(qapp):
    from PySide6.QtCore import Qt

    from albuswall.ui.window.detail import Detail

    detail = Detail()
    detail.resize(600, 400)
    detail.show()
    closed: list[int] = []
    detail.close_requested.connect(lambda: closed.append(1))
    try:
        detail.set_preview_mode(True)
        assert detail.is_preview_mode is True
        key_click(detail.image_viewer, Qt.Key.Key_Return)
        assert detail.is_preview_mode is False
        assert closed == []  # 回车 = 升级成完整详情页，不关页

        detail.set_preview_mode(True)
        key_click(detail.image_viewer, Qt.Key.Key_Space)
        assert detail.is_preview_mode is False
        assert closed == [1]  # 空格 = 退出预览并收起详情页
    finally:
        _dispose(detail)


def test_hiding_detail_clears_preview_mode(qapp):
    from albuswall.ui.window.detail import Detail

    detail = Detail()
    detail.resize(600, 400)
    detail.show()
    detail.set_preview_mode(True)
    assert detail.is_preview_mode is True
    detail.hide()
    assert detail.is_preview_mode is False
    _dispose(detail)


# ==================== 窗口：工具栏「更多」按钮的菜单键 ====================

@pytest.fixture()
def window(qapp):
    from albuswall.ui.window import Window

    w = Window()
    w.resize(640, 480)
    w.show()
    try:
        yield w
    finally:
        _dispose(w)


def test_more_button_menu_key_opens_more_menu(window, filtered_app):
    from PySide6.QtCore import Qt

    button = window.title_bar.extra_button
    assert button.menu() is window.menu
    key_click(button, Qt.Key.Key_Menu)
    assert window.menu.isVisible() is True
    window.menu.hide()


def test_more_button_enter_opens_more_menu(window, filtered_app):
    from PySide6.QtCore import Qt

    button = window.title_bar.extra_button
    key_click(button, Qt.Key.Key_Return)
    assert window.menu.isVisible() is True
    window.menu.hide()


def test_more_button_shift_f10_opens_more_menu(window, filtered_app):
    from PySide6.QtCore import Qt

    button = window.title_bar.extra_button
    key_click(button, Qt.Key.Key_F10, Qt.KeyboardModifier.ShiftModifier)
    assert window.menu.isVisible() is True
    window.menu.hide()


# ==================== 过滤器契约本身 ====================

def test_activation_key_predicate(filtered_app):
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent

    from albuswall.ui.utils import ACTIVATION_KEYS, is_activation_key

    def event(key, *, repeat=False):
        return QKeyEvent(QEvent.Type.KeyPress, key,
                         Qt.KeyboardModifier.NoModifier, "", repeat)

    assert tuple(ACTIVATION_KEYS) == (
        Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter)
    for key in ACTIVATION_KEYS:
        assert is_activation_key(event(key)) is True
    assert is_activation_key(event(Qt.Key.Key_A)) is False


def test_auto_repeat_does_not_click_again(filtered_app):
    """按住不放（自动重复）与 Qt 原生按钮一致：只算一次。"""
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QApplication, QPushButton

    button = QPushButton("确定")
    clicks: list[int] = []
    button.clicked.connect(lambda: clicks.append(1))
    button.show()
    try:
        QApplication.sendEvent(button, QKeyEvent(
            QEvent.Type.KeyPress, Qt.Key.Key_Return,
            Qt.KeyboardModifier.NoModifier, "", True))
        assert clicks == []
    finally:
        _dispose(button)


# ==================== 可勾选分组：回车 = 空格 ====================

def test_enter_toggles_checkable_group_box(filtered_app):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QGroupBox

    group = QGroupBox("触发器")
    group.setCheckable(True)
    group.setChecked(False)
    group.show()
    try:
        key_click(group, Qt.Key.Key_Return)
        assert group.isChecked() is True
        key_click(group, Qt.Key.Key_Space)
        assert group.isChecked() is False
    finally:
        _dispose(group)


def test_enter_ignores_non_checkable_group_box(filtered_app):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QGroupBox

    group = QGroupBox("普通分组")
    group.show()
    try:
        key_click(group, Qt.Key.Key_Return)  # 不崩、不切换（本来就不能切换）
        assert group.isCheckable() is False
    finally:
        _dispose(group)
