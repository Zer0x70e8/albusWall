#
"""纯键盘操作（一站到底）的回归测试。

覆盖：

  1. 缩略图网格的光标移动 / 回车激活 / Home 等（``VirtualScrollWidget``）；
  2. 设置页折叠分组：获得焦点后 Enter / Space 展开折叠；
  3. 焦点区域循环（F6 / Shift+F6）与焦点跟随浮层；
  4. ``window.escape`` 的逐层收起（信息面板 → 详情页 → 浮层）；
  5. ``window.menu`` 用键盘打开标题栏菜单。

用 offscreen Qt 平台运行，不需要显示器：

    QT_QPA_PLATFORM=offscreen python test/test_keyboard_navigation.py
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"

try:  # 允许直接以文件路径运行
    import albuswall  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication, QPushButton, QWidget

_app = QApplication.instance() or QApplication([])

from albuswall.ui.builtin.keymap import navigation  # noqa: E402
from albuswall.ui.builtin.keymap.actions import (  # noqa: E402
    ensure_builtin_actions,
)
from albuswall.ui.builtin.keymap.manager import KeymapManager  # noqa: E402


# ══════════════════════════════════════════════════════════════
# 工具
# ══════════════════════════════════════════════════════════════
def _key(key, modifiers=Qt.KeyboardModifier.NoModifier) -> QKeyEvent:
    return QKeyEvent(QEvent.Type.KeyPress, key, modifiers)


class _FakeOverlay(QWidget):
    currentChanged = Signal(int)
    show_blank_signal = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._current: QWidget | None = None

    def setCurrentWidget(self, widget: QWidget) -> None:  # noqa: N802
        self._current = widget
        self.currentChanged.emit(0)

    def currentWidget(self) -> QWidget | None:  # noqa: N802
        return self._current

    def is_blank(self) -> bool:
        return self._current is None

    def show_blank(self) -> None:
        self._current = None
        self.currentChanged.emit(0)


class _FakeDetail(QWidget):
    close_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.panel = QWidget(self)
        self.panel.hide()
        self.information_button = QPushButton("info", self)
        self.image_viewer = QWidget(self)

    def show_information(self, visible: bool = True) -> None:  # noqa: N802
        self.panel.setVisible(visible)

    def isVisible(self) -> bool:  # noqa: N802
        # 测试里不真的显示浮层，只关心父窗口的可见性
        return self.parent() is not None and self.parent().isVisible()


class _FakeTitleBar(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.album_button = QPushButton("album", self)
        self.extra_button = _FakeMenuButton(self)


class _FakeMenuButton(QPushButton):
    def __init__(self, parent=None):
        super().__init__("more", parent)
        self.menu_shown = 0

    def showMenu(self) -> None:  # noqa: N802 - Qt 命名
        self.menu_shown += 1


class _FakeWindow(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("fake")
        self.overlay = _FakeOverlay(self)
        self.title_bar = _FakeTitleBar(self)
        self.content = QWidget(self)
        self.content.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.album = QWidget(self)
        self.source = QWidget(self)
        self.setting = QWidget(self)
        self.detail = _FakeDetail(self)


def _make_manager(window: _FakeWindow) -> KeymapManager:
    ensure_builtin_actions()
    manager = KeymapManager(window, parent=window)
    manager.setup()
    window.show()
    _app.processEvents()
    return manager


# ══════════════════════════════════════════════════════════════
# 1. 网格键盘光标
# ══════════════════════════════════════════════════════════════
def test_grid_keyboard_cursor_and_activation() -> None:
    from albuswall.ui.builtin.widgets.virtual_scroll import VirtualScrollWidget

    grid = VirtualScrollWidget()
    grid.resize(300, 200)
    grid.show()
    _app.processEvents()

    grid.reset_source(9)  # 10 项
    grid.set_column_count(3)
    assert grid.cursor_index() == 0

    grid.setFocus()
    grid.keyPressEvent(_key(Qt.Key.Key_Right))
    assert grid.cursor_index() == 1
    grid.keyPressEvent(_key(Qt.Key.Key_Down))
    assert grid.cursor_index() == 1 + grid.single_row_num
    grid.keyPressEvent(_key(Qt.Key.Key_Home))
    assert grid.cursor_index() == 0
    grid.keyPressEvent(_key(Qt.Key.Key_End))
    assert grid.cursor_index() == 9

    activated: list[int] = []
    grid.unit_activated.connect(activated.append)
    grid.set_cursor_index(4)
    grid.keyPressEvent(_key(Qt.Key.Key_Return))
    assert activated == [4]

    # 数据源重置：光标回到第一项；没有数据时清空
    grid.reset_source(2)
    assert grid.cursor_index() == 0
    grid.reset_source(-1)
    assert grid.cursor_index() == -1

    grid.deleteLater()
    _app.processEvents()


# ══════════════════════════════════════════════════════════════
# 2. 设置页分组键盘展开 / 折叠
# ══════════════════════════════════════════════════════════════
def test_collapsible_group_box_toggles_with_keyboard() -> None:
    from albuswall.ui.builtin.widgets.collapsible_group_box import (
        CollapsibleGroupBox,
    )

    box = CollapsibleGroupBox("Group")
    box.show()
    _app.processEvents()
    assert box.focusPolicy() == Qt.FocusPolicy.StrongFocus

    assert box.isChecked() is True
    box.keyPressEvent(_key(Qt.Key.Key_Return))
    assert box.isChecked() is False
    box.keyPressEvent(_key(Qt.Key.Key_Space))
    assert box.isChecked() is True

    box.deleteLater()
    _app.processEvents()


# ══════════════════════════════════════════════════════════════
# 3. F6 焦点区域循环
# ══════════════════════════════════════════════════════════════
def test_focus_region_cycle() -> None:
    window = _FakeWindow()
    window.show()
    _app.processEvents()

    class _Ctx:
        def __init__(self, win):
            self.window = win
            self.container = None
            self.presenters = None

    ctx = _Ctx(window)
    window.album_button = window.title_bar.album_button
    regions = navigation.regions(ctx)
    assert window.title_bar.album_button in regions
    assert window.content in regions

    window.content.setFocus()
    assert QApplication.focusWidget() is window.content
    assert navigation.cycle(ctx, 1) is True
    assert QApplication.focusWidget() is window.title_bar.album_button
    assert navigation.cycle(ctx, -1) is True
    assert QApplication.focusWidget() is window.content

    window.deleteLater()
    _app.processEvents()


# ══════════════════════════════════════════════════════════════
# 4. Esc 逐层收起
# ══════════════════════════════════════════════════════════════
def test_escape_closes_layers_in_order() -> None:
    window = _FakeWindow()
    manager = _make_manager(window)

    # ① 详情页 + 信息面板：Esc 先收面板
    window.overlay.setCurrentWidget(window.detail)
    window.detail.show_information(True)
    _app.processEvents()
    assert manager.handle_key_event(_key(Qt.Key.Key_Escape), focus=window)
    assert window.detail.panel.isVisible() is False

    # ② 面板关掉后：Esc 关详情页（发 close_requested）
    closed: list[bool] = []
    window.detail.close_requested.connect(lambda: closed.append(True))
    assert manager.handle_key_event(_key(Qt.Key.Key_Escape), focus=window)
    assert closed == [True]

    # ③ 其它浮层：Esc 回到空白页
    window.overlay.setCurrentWidget(window.setting)
    assert manager.handle_key_event(_key(Qt.Key.Key_Escape), focus=window)
    assert window.overlay.is_blank() is True

    # ④ 已经是空白页：不消费 Esc
    assert not manager.handle_key_event(_key(Qt.Key.Key_Escape), focus=window)

    manager.teardown()
    window.deleteLater()
    _app.processEvents()


# ══════════════════════════════════════════════════════════════
# 5. 回车激活按钮；在网格 / 列表里回车不抢
# ══════════════════════════════════════════════════════════════
def test_enter_activates_buttons_but_not_widgets() -> None:
    window = _FakeWindow()
    manager = _make_manager(window)

    clicked: list[bool] = []
    button = QPushButton("ok", window)
    button.clicked.connect(lambda: clicked.append(True))

    assert manager.handle_key_event(_key(Qt.Key.Key_Return), focus=button)
    assert clicked == [True]

    # 网格 / 列表的 Enter 不能被键位系统吃掉：交给控件自己（激活当前项）
    assert not manager.handle_key_event(
        _key(Qt.Key.Key_Return), focus=window.content,
    )

    manager.teardown()
    window.deleteLater()
    _app.processEvents()


# ══════════════════════════════════════════════════════════════
# 6. 键盘打开「更多」菜单
# ══════════════════════════════════════════════════════════════
def test_menu_action_opens_title_bar_menu() -> None:
    window = _FakeWindow()
    manager = _make_manager(window)

    event = _key(Qt.Key.Key_M, Qt.KeyboardModifier.AltModifier)
    assert manager.handle_key_event(event, focus=window)
    assert window.title_bar.extra_button.menu_shown == 1

    manager.teardown()
    window.deleteLater()
    _app.processEvents()


# ══════════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════════
def main() -> int:
    tests = [
        (name, obj)
        for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    failures: list[tuple[str, BaseException]] = []
    for name, fn in tests:
        try:
            fn()
        except BaseException as exc:  # noqa: BLE001
            failures.append((name, exc))
            print(f"FAIL {name}: {exc}")
            traceback.print_exc()
        else:
            print(f"ok   {name}")

    print()
    if failures:
        print(f"{len(failures)}/{len(tests)} 个用例失败")
        return 1
    print(f"全部 {len(tests)} 个用例通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
