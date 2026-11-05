#
"""按键层（``ui.keymap.qt_binder``）的契约：``QKeyEvent`` 真的能触发槽。

锁住四类行为：

* **能触发**：单键 / 修饰键组合 / 多步和弦（含「既是完整键位又是更长和弦
  开头」时的超时退化）；
* **不越界**：导航键（Tab / 方向键 / Home / End / PgUp / PgDn / Esc）、
  文本输入里的无修饰键、``keymap_passthrough`` 控件上的空格 / 回车、
  没有已登记槽的占位绑定，一律放行给控件；
* **别名**：物理 ``Shift+Tab`` 转出来是 ``Shift+Backtab``，仍能命中
  ``Shift+Tab`` / ``Backtab`` 的绑定（:class:`KeyAlias` 身份匹配）；
* **安装面**：合成事件默认不触发（避免 activation_keys 把回车改写成空格后
  再触发一遍）、owner 作用域以外的窗口不接管、重复安装只有一个过滤器、
  卸载后按键回到控件。

Qt 走 offscreen（``QT_QPA_PLATFORM=offscreen``，必须在 import PySide6 之前
设好）；按键用 ``QTest.keyClick``（事件是自发事件，与真实键盘同一路径）。
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")


#: ``show_focused`` 等焦点的上限（毫秒）。offscreen 激活通常 <100ms，
#: 给足余量是为了把「偶发慢」与「真的没焦点」区分开。
FOCUS_TIMEOUT_MS = 3000


# ==================== fixture ====================

@pytest.fixture(scope="module")
def qapp() -> Iterator[Any]:
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _close_windows(qapp):
    """每个用例结束收掉残留的顶层窗口。

    上一个用例的窗口还开着，会一直占着 ``QApplication.activeWindow()``，
    下一个用例的 ``show_focused`` 就抢不到焦点（表现是「焦点没落到控件上」）。
    """
    from PySide6.QtWidgets import QApplication

    yield
    for widget in QApplication.topLevelWidgets():
        widget.close()
        widget.deleteLater()
    qapp.processEvents()


class Recorder:
    """记录收到的 ``KeyPress``；配合 ``RecorderWidget`` 使用。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.presses: list[int] = []


class RecorderWidget:
    """一次性的「焦点控件」制造器：把按键写进共享的 :class:`Recorder`。"""

    @staticmethod
    def build(recorder: Recorder, *, passthrough: bool = False) -> Any:
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QWidget

        class _Widget(QWidget):
            def __init__(self) -> None:
                super().__init__()
                if passthrough:
                    self.setProperty("keymap_passthrough", True)
                self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

            def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt 命名
                recorder.presses.append(int(event.key()))
                event.accept()

        return _Widget()


def show_focused(widget: Any, qapp: Any) -> Any:
    """显示控件并确保它拿到焦点（offscreen 下也要 ``focusWidget`` 指向它）。

    offscreen 平台的「激活窗口」是**异步**的：``show()`` 之后要让事件循环真跑
    几轮才会落到 ``QApplication.activeWindow()`` 上，只用 ``processEvents()``
    不够。

    等待策略（**超时必抛**）：先让 Qt 自己等窗口 active / focused，再在
    ``FOCUS_TIMEOUT_MS`` 预算内轮询 ``QApplication.focusWidget()``；超时抛带
    诊断信息的 ``AssertionError``。旧实现是「重试 15×20ms 后静默失败」，失败时
    只会看到后面那条 ``recorder.calls == []`` 断言失败，看不出是「焦点没到位」
    还是「binder 没接管」——这正是审计 A7 里那次 flaky 的观感来源。

    为什么不用 ``QTest.qWaitFor(谓词)``：PySide6 6.11 的 ``QTest`` 没有暴露它
    （只有 ``qWaitForWindowActive`` / ``qWaitForWindowExposed`` /
    ``qWaitForWindowFocused``），所以「等谓词」这层只能自己轮询。
    """
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    widget.resize(200, 100)
    widget.show()
    qapp.processEvents()

    handle = widget.windowHandle()
    if handle is not None:
        handle.requestActivate()
    widget.setFocus()

    # Qt 自己的等待：能覆盖「窗口还没 active / 还没 focus」这两种异步状态
    QTest.qWaitForWindowActive(widget, FOCUS_TIMEOUT_MS)

    deadline = time.monotonic() + FOCUS_TIMEOUT_MS / 1000.0
    while time.monotonic() < deadline:
        if QApplication.focusWidget() is widget:
            return widget
        widget.setFocus()
        QTest.qWait(10)

    raise AssertionError(
        "focus never arrived: QApplication.focusWidget() is not the test "
        f"widget after {FOCUS_TIMEOUT_MS} ms "
        f"(focus={QApplication.focusWidget()!r}, "
        f"window={widget.window()!r}, isVisible={widget.isVisible()})"
    )


def send_key(widget: Any, key: Any, mods: Any = None) -> None:
    """投递一次真实的按键（自发事件路径，和用户按键一致）。"""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    QTest.keyClick(widget, key, Qt.KeyboardModifier.NoModifier if mods is None else mods)


def make_registry(commands: dict[str, str]) -> Any:
    """``{命令: 键位}`` → 注册表（每个命令一个记录用槽）。"""
    from albuswall.ui.keymap import ShortcutRegistry, ShortcutSlot

    registry = ShortcutRegistry()
    recorder = Recorder()

    def handler(command: str = ""):
        def call() -> bool:
            recorder.calls.append(command)
            return True

        return call

    for command, keys in commands.items():
        registry.register_slot(
            ShortcutSlot(id=command, handler=handler(command), default_keys=keys),
        )
    registry.load()
    return registry, recorder


@pytest.fixture
def binder_factory(qapp):
    """造一个装好过滤器的 binder，用例结束统一摘掉。"""
    from albuswall.ui.keymap import ShortcutBinder, uninstall_shortcut_binder

    created: list[Any] = []

    def make(registry: Any, **kwargs: Any) -> Any:
        binder = ShortcutBinder(registry, **kwargs)
        binder.install()
        created.append(binder)
        return binder

    yield make

    uninstall_shortcut_binder(qapp)
    for binder in created:
        binder.uninstall()


# ==================== 能触发 ====================

def test_single_key_triggers_slot(qapp, binder_factory):
    from PySide6.QtCore import Qt

    registry, recorder = make_registry({"view.fullscreen": "F11"})
    binder_factory(registry)
    widget = show_focused(RecorderWidget.build(recorder), qapp)

    send_key(widget, Qt.Key.Key_F11)

    assert recorder.calls == ["view.fullscreen"]
    assert recorder.presses == []      # 被消费掉了，控件收不到


def test_modifier_combo_triggers_slot(qapp, binder_factory):
    from PySide6.QtCore import Qt

    registry, recorder = make_registry({"album.new": "Ctrl+N"})
    binder_factory(registry)
    widget = show_focused(RecorderWidget.build(recorder), qapp)

    send_key(widget, Qt.Key.Key_N, Qt.KeyboardModifier.ControlModifier)

    assert recorder.calls == ["album.new"]
    # 组合键被消费：控件收不到 N（Ctrl 本身是纯修饰键，照常派发）
    assert Qt.Key.Key_N.value not in recorder.presses


def test_one_key_fires_every_bound_command(qapp, binder_factory):
    from PySide6.QtCore import Qt

    from albuswall.ui.keymap import ShortcutRegistry, ShortcutSlot

    seen: list[str] = []

    def make_slot(name: str) -> Any:
        return ShortcutSlot(
            id=name, handler=lambda: seen.append(name), default_keys="Ctrl+W")

    registry = ShortcutRegistry()
    registry.register_slot(make_slot("window.close"))
    registry.register_slot(make_slot("viewer.close"))
    registry.load()
    binder_factory(registry)
    widget = show_focused(RecorderWidget.build(Recorder()), qapp)

    send_key(widget, Qt.Key.Key_W, Qt.KeyboardModifier.ControlModifier)

    assert seen == ["window.close", "viewer.close"]


def test_chord_requires_both_steps(qapp, binder_factory):
    from PySide6.QtCore import Qt

    registry, recorder = make_registry({"test.chord": "G, G"})
    binder_factory(registry)
    widget = show_focused(RecorderWidget.build(recorder), qapp)

    send_key(widget, Qt.Key.Key_G)
    assert recorder.calls == []          # 只按了一半：等着

    send_key(widget, Qt.Key.Key_G)
    assert recorder.calls == ["test.chord"]


def test_chord_prefix_is_dropped_by_unrelated_key(qapp, binder_factory):
    from PySide6.QtCore import Qt

    registry, recorder = make_registry({"test.chord": "G, G"})
    binder_factory(registry)
    widget = show_focused(RecorderWidget.build(recorder), qapp)

    send_key(widget, Qt.Key.Key_G)
    send_key(widget, Qt.Key.Key_X)       # 前缀作废
    send_key(widget, Qt.Key.Key_G)       # 重新从头攒
    assert recorder.calls == []

    send_key(widget, Qt.Key.Key_G)
    assert recorder.calls == ["test.chord"]


def test_chord_timeout_falls_back_to_shorter_binding(qapp, binder_factory):
    """``G`` 与 ``G, G`` 并存：先等超时，再退化成 ``G``。"""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from albuswall.ui.keymap import ShortcutRegistry, ShortcutSlot

    seen: list[str] = []
    registry = ShortcutRegistry()
    registry.register_slot(ShortcutSlot(
        id="test.short", handler=lambda: seen.append("short"),
        default_keys="G"))
    registry.register_slot(ShortcutSlot(
        id="test.chord", handler=lambda: seen.append("chord"),
        default_keys="G, G"))
    registry.bind_command("test.short", "G", replace=False)
    registry.bind_command("test.chord", "G, G", replace=False)
    binder_factory(registry, chord_timeout_ms=20)
    widget = show_focused(RecorderWidget.build(Recorder()), qapp)

    send_key(widget, Qt.Key.Key_G)
    assert seen == []                    # 有更长和弦可用：先不退化成短键位

    QTest.qWait(80)
    assert seen == ["short"]


def test_escape_resets_the_pending_chord(qapp, binder_factory):
    from PySide6.QtCore import Qt

    registry, recorder = make_registry({"test.chord": "G, G"})
    binder_factory(registry)
    widget = show_focused(RecorderWidget.build(recorder), qapp)

    send_key(widget, Qt.Key.Key_G)
    send_key(widget, Qt.Key.Key_Escape)
    # Esc 归窗口/控件：本层不吞，只放弃前缀
    assert Qt.Key.Key_Escape.value in recorder.presses
    send_key(widget, Qt.Key.Key_G)
    assert recorder.calls == []
    send_key(widget, Qt.Key.Key_G)
    assert recorder.calls == ["test.chord"]


# ==================== 不越界 ====================

def test_autorepeat_is_ignored(qapp, binder_factory):
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QApplication

    registry, recorder = make_registry({"view.fullscreen": "F11"})
    binder = binder_factory(registry, allow_synthetic=True)
    widget = show_focused(RecorderWidget.build(recorder), qapp)

    repeat = QKeyEvent(
        QEvent.Type.KeyPress, Qt.Key.Key_F11,
        Qt.KeyboardModifier.NoModifier, "", True, 2)
    QApplication.sendEvent(widget, repeat)

    assert recorder.calls == []
    assert binder.pending == ()


def test_navigation_keys_are_never_swallowed(qapp, binder_factory):
    """Tab / 方向键 / Esc 即使被绑成快捷键，也照样交给控件。"""
    from PySide6.QtCore import Qt

    registry, recorder = make_registry({
        "test.tab": "Tab",
        "test.down": "Down",
        "test.home": "Home",
        "test.escape": "Esc",
    })
    binder_factory(registry)
    widget = show_focused(RecorderWidget.build(recorder), qapp)

    for key in (
            Qt.Key.Key_Tab, Qt.Key.Key_Down, Qt.Key.Key_Home,
            Qt.Key.Key_Escape,
    ):
        send_key(widget, key)
        assert recorder.presses[-1] == key.value
    assert recorder.calls == []


def test_unregistered_binding_is_not_swallowed(qapp, binder_factory):
    """绑定表里还没有槽的命令：按键不吞，照常给控件。"""
    from PySide6.QtCore import Qt

    from albuswall.ui.keymap import ShortcutRegistry

    registry = ShortcutRegistry()
    registry.bind_command("plugin.later", "Ctrl+J")
    binder_factory(registry)
    recorder = Recorder()
    widget = show_focused(RecorderWidget.build(recorder), qapp)

    send_key(widget, Qt.Key.Key_J, Qt.KeyboardModifier.ControlModifier)

    assert Qt.Key.Key_J.value in recorder.presses


def test_text_input_keeps_unmodified_keys(qapp, binder_factory):
    """文本框里打字归文本框；修饰键组合仍然能触发快捷键。"""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QLineEdit

    registry, recorder = make_registry({
        "test.letter": "G",
        "content.search": "Ctrl+F",
    })
    binder_factory(registry)
    edit = show_focused(QLineEdit(), qapp)

    send_key(edit, Qt.Key.Key_G)
    assert edit.text() == "g"
    assert recorder.calls == []

    send_key(edit, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
    assert recorder.calls == ["content.search"]


def test_passthrough_widget_keeps_widget_keys(qapp, binder_factory):
    """``keymap_passthrough`` 控件：空格 / 回车让渡，其它键照常接管。"""
    from PySide6.QtCore import Qt

    registry, recorder = make_registry({
        "test.preview": "Space",
        "test.activate": "Enter",
        "view.fullscreen": "F11",
    })
    binder_factory(registry)
    widget = show_focused(
        RecorderWidget.build(recorder, passthrough=True), qapp)

    send_key(widget, Qt.Key.Key_Space)
    send_key(widget, Qt.Key.Key_Return)
    assert recorder.presses == [Qt.Key.Key_Space.value, Qt.Key.Key_Return.value]
    assert recorder.calls == []

    send_key(widget, Qt.Key.Key_F11)
    assert recorder.calls == ["view.fullscreen"]


def test_button_keeps_space_and_enter(qapp, binder_factory):
    """按钮上的空格 / 回车是「点击」：即使绑了也不抢。"""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QPushButton

    registry, recorder = make_registry({"test.preview": "Space"})
    binder_factory(registry)
    button = QPushButton("ok")
    button.setProperty("keymap_passthrough", True)
    show_focused(button, qapp)

    clicks: list[bool] = []
    button.clicked.connect(lambda _checked=False: clicks.append(True))
    send_key(button, Qt.Key.Key_Space)
    qapp.processEvents()

    assert clicks == [True]
    assert recorder.calls == []


# ==================== 别名 ====================

def test_keypad_enter_matches_enter_binding(qapp, binder_factory):
    """小键盘回车（``Enter``）与主回车（``Return``）是同一个身份。"""
    from PySide6.QtCore import Qt

    registry, recorder = make_registry({"test.enter": "Enter"})
    binder_factory(registry)
    widget = show_focused(RecorderWidget.build(recorder), qapp)

    send_key(widget, Qt.Key.Key_Enter)       # 小键盘回车
    assert recorder.calls == ["test.enter"]
    send_key(widget, Qt.Key.Key_Return)      # 主回车：同一身份
    assert recorder.calls == ["test.enter", "test.enter"]


def test_return_binding_matches_keypad_enter(qapp, binder_factory):
    """反过来也成立：绑定写 ``Return``，小键盘回车照样命中。"""
    from PySide6.QtCore import Qt

    registry, recorder = make_registry({"test.enter": "Return"})
    binder_factory(registry)
    widget = show_focused(RecorderWidget.build(recorder), qapp)

    send_key(widget, Qt.Key.Key_Enter)

    assert recorder.calls == ["test.enter"]


def test_shift_tab_stays_with_the_focus_chain(qapp, binder_factory):
    """``Shift+Tab`` 的别名身份能被识别，但它属于焦点链：绑了也不接管。"""
    from PySide6.QtCore import Qt

    from albuswall.ui.keymap import KeyAlias, step_identity

    assert step_identity("Shift+Tab") == step_identity("Shift+Backtab")
    assert KeyAlias.of("Shift+Tab") is KeyAlias.BACKTAB

    registry, recorder = make_registry({"test.backtab": "Shift+Tab"})
    binder_factory(registry)
    widget = show_focused(RecorderWidget.build(recorder), qapp)

    send_key(widget, Qt.Key.Key_Backtab, Qt.KeyboardModifier.ShiftModifier)

    assert recorder.calls == []
    assert Qt.Key.Key_Backtab.value in recorder.presses


# ==================== 安装面 ====================

def test_synthetic_events_are_ignored_by_default(qapp, binder_factory):
    """activation_keys 合成的空格（非自发事件）不能触发快捷键。"""
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QApplication

    registry, recorder = make_registry({"test.preview": "Space"})
    binder_factory(registry)
    widget = show_focused(
        RecorderWidget.build(recorder, passthrough=True), qapp)

    synthetic = QKeyEvent(
        QEvent.Type.KeyPress, Qt.Key.Key_Space, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(widget, synthetic)

    assert recorder.calls == []
    # 合成事件照旧派发给控件（它自己处理），只是不进快捷键层
    assert recorder.presses == [int(Qt.Key.Key_Space)]


def test_binder_owner_scope_ignores_other_windows(qapp, binder_factory):
    from PySide6.QtCore import Qt

    registry, recorder = make_registry({"view.fullscreen": "F11"})
    owner = RecorderWidget.build(Recorder())
    other = RecorderWidget.build(recorder)
    owner.resize(200, 100)
    owner.show()
    binder_factory(registry, owner=owner)
    try:
        show_focused(other, qapp)
        send_key(other, Qt.Key.Key_F11)
        assert recorder.calls == []

        # 关掉「别的窗口」再切回 owner（offscreen 下先把旧窗口收掉才稳）
        other.close()
        qapp.processEvents()
        show_focused(owner, qapp)
        send_key(owner, Qt.Key.Key_F11)
        assert recorder.calls == ["view.fullscreen"]
    finally:
        owner.close()
        other.close()


def test_install_is_idempotent_per_application(qapp):
    from albuswall.ui.keymap import (
        install_shortcut_binder,
        uninstall_shortcut_binder,
    )

    registry, _recorder = make_registry({"view.fullscreen": "F11"})
    first = install_shortcut_binder(qapp, registry)
    second = install_shortcut_binder(qapp, registry)
    try:
        assert first is second       # 同一个应用只有一个过滤器
    finally:
        uninstall_shortcut_binder(qapp)


def test_uninstall_restores_widget_keys(qapp, binder_factory):
    from PySide6.QtCore import Qt

    registry, recorder = make_registry({"view.fullscreen": "F11"})
    binder = binder_factory(registry)
    widget = show_focused(RecorderWidget.build(recorder), qapp)

    send_key(widget, Qt.Key.Key_F11)
    assert recorder.calls == ["view.fullscreen"]

    binder.uninstall()
    send_key(widget, Qt.Key.Key_F11)
    assert recorder.presses == [Qt.Key.Key_F11.value]
    assert recorder.calls == ["view.fullscreen"]
