#
"""``NavPresenter``：键盘事件 → nav 决策 → 真实焦点。

这里是 nav 第一次接到真实按键上，用例覆盖四件事：

* **分派顺序**（快捷键 → 让渡 → nav 动作 → 冒泡）与「消费」的判据；
* **焦点执行**：拿到落点后 ``focus_node`` 真的动了焦点；
* **反向同步**：``focusChanged`` / 程序化 ``setFocus`` 之后光标跟着走；
* **暂停 / 模态**：不做任何分派。

> 为什么用 ``presenter.target_widget()`` 而不是直接读 ``QApplication.focusWidget()``：
> 本机环境里 ``QT_QPA_PLATFORM`` 已是 ``wayland;xcb``，没有合成器时窗口不会
> 激活、``focusWidget()`` 恒为 ``None``。用例把「焦点在哪」设成确定值，剩下的
> 链路（应用级 eventFilter / 真实 ``sendEvent`` / 真正的 Qt 控件）都照跑。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from albuswall.core.container import Container  # noqa: E402
from albuswall.ui.nav.registry import NavRegistry  # noqa: E402
from albuswall.ui.nav.wiring import NAV_PAGE_ID, register_window_nav  # noqa: E402

pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from albuswall.ui.keymap import ShortcutRegistry, ShortcutSlot  # noqa: E402
from albuswall.ui.presenter.nav_presenter import NavPresenter  # noqa: E402
from albuswall.ui.window import Window  # noqa: E402


# ==================== fixtures ====================

@pytest.fixture(scope="module")
def qapp() -> Iterator[QApplication]:
    app = QApplication.instance() or QApplication([])
    yield app


class Rig:
    """一个窗口 + 一张注册表 + 一个已启动的分发器。"""

    def __init__(
            self,
            app: QApplication,
            window: Window,
            nav: NavRegistry,
            shortcuts: ShortcutRegistry,
            presenter: NavPresenter,
    ) -> None:
        self.app = app
        self.window = window
        self.nav = nav
        self.shortcuts = shortcuts
        self.presenter = presenter
        self._focus: Any = None
        # 「焦点在哪」由用例显式指定（见模块 docstring）
        presenter.target_widget = lambda: self._focus  # type: ignore[method-assign]

    # ---- 焦点 ----
    def focus(self, widget: Any) -> None:
        """模拟一次 focusChanged：设定焦点控件并触发反向同步。"""
        self._focus = widget
        self.presenter.sync_to_widget(widget)

    def blur(self) -> None:
        self._focus = None

    # ---- 事件 ----
    def press(
            self,
            key: Qt.Key,
            modifiers: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier,
    ) -> QKeyEvent:
        """走**真实事件链**：``sendEvent`` → 应用级 ``eventFilter`` → nav。

        **只**发一次事件：不再额外调 ``handle_key_event``，否则同一下按键会被
        分派两遍（光标一次跳两格）。
        """
        target = self._focus or self.window
        event = QKeyEvent(QEvent.Type.KeyPress, key, modifiers)
        QApplication.sendEvent(target, event)
        self.app.processEvents()
        return event

    def handle(self, key: Qt.Key,
               modifiers: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier,
               ) -> bool:
        """只问分派结果，不发事件（不触发控件自己的行为）。"""
        return self.presenter.handle_key_event(
            QKeyEvent(QEvent.Type.KeyPress, key, modifiers))

    # ---- 状态 ----
    @property
    def frame(self):
        return self.nav.current_frame(NAV_PAGE_ID)

    @property
    def current(self) -> str | None:
        frame = self.frame
        return None if frame is None else frame.current

    def reset(self) -> None:
        """同一窗口内复位：overlay 收起 + 重新 wiring + 光标回根 + 清焦点。"""
        self.window.overlay.show_blank()
        self.app.processEvents()
        register_window_nav(self.window, registry=self.nav)
        self.shortcuts.clear()
        self.blur()

    def spy_focus(self) -> list[str]:
        """记录 ``focus_node`` 的调用（真实焦点在无 WM 平台上看不出来）。"""
        focused: list[str] = []
        original = self.nav.focus_node

        def record(node) -> None:
            focused.append(node.id)
            original(node)

        self.nav.focus_node = record  # type: ignore[method-assign]
        return focused

    def open_page(self, page: str) -> None:
        """打开 overlay 的某一页（``album`` / ``source`` / ``detail``）。"""
        widget = getattr(self.window, page)
        self.window.overlay.setCurrentWidget(widget)
        self.app.processEvents()

    def enter_region(self, gateway: str, widget: Any) -> None:
        """把光标放进某个区域，并让焦点落在该区域的某个控件上。"""
        self.nav.set_current(NAV_PAGE_ID, gateway)
        self.nav.enter(NAV_PAGE_ID, how="tab")
        self.focus(widget)


@pytest.fixture(scope="module")
def _rig(qapp) -> Iterator[Rig]:
    window = Window()
    window.resize(900, 700)
    window.show()
    qapp.processEvents()

    nav = NavRegistry()
    register_window_nav(window, registry=nav)   # boot phase 3b 做的事
    shortcuts = ShortcutRegistry()
    container = Container()
    container.set("nav_registry", nav)
    container.set("shortcuts", shortcuts)

    presenter = NavPresenter(container, window=window)
    presenter.start()
    rig = Rig(qapp, window, nav, shortcuts, presenter)
    try:
        yield rig
    finally:
        presenter.stop()
        window.close()


@pytest.fixture
def rig(_rig: Rig) -> Rig:
    """每个用例拿到干净的导航状态（窗口复用：offscreen 之外重建窗口拿不到焦点）。"""
    _rig.reset()
    return _rig


# ==================== 生命周期 ====================

def test_idle_without_nav_registry(qapp):
    """容器里没有 nav 注册表：安静退化，不装过滤器。"""
    container = Container()
    window = Window()
    presenter = NavPresenter(container, window=window)
    presenter.start()
    try:
        assert presenter._nav is None  # noqa: SLF001 - 断言退化状态
        assert presenter.handle_key_event(
            QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Tab,
                      Qt.KeyboardModifier.NoModifier)) is False
    finally:
        presenter.stop()
        window.close()


def test_idle_without_page(qapp):
    """有注册表但页面没 wiring：后退（不索引、不装过滤器）。"""
    container = Container()
    container.set("nav_registry", NavRegistry())
    window = Window()
    presenter = NavPresenter(container, window=window)
    presenter.start()
    try:
        assert presenter._installed_app is None  # noqa: SLF001
    finally:
        presenter.stop()
        window.close()


def test_start_installs_the_filter(rig):
    assert rig.presenter._installed_app is not None  # noqa: SLF001
    assert rig.presenter._widget_index  # noqa: SLF001 - 索引建好了


def test_stop_removes_the_filter(rig):
    rig.enter_region("window.titlebar", rig.window.title_bar.album_button)
    before = rig.current

    rig.presenter.stop()
    try:
        assert rig.presenter._installed_app is None  # noqa: SLF001
        rig.handle(Qt.Key.Key_Right)
        assert rig.current == before
    finally:
        rig.presenter.start()  # 让 module 级 fixture 收尾时状态一致


# ==================== 分派：方向键 / Enter ====================

def test_move_executes_focus(rig):
    """方向键 → nav.move → ``focus_node`` 被调用到正确节点。"""
    focused = rig.spy_focus()
    rig.enter_region("window.titlebar", rig.window.title_bar.album_button)
    assert rig.current == "titlebar.album"
    focused.clear()

    rig.press(Qt.Key.Key_Right)
    assert rig.current == "titlebar.search"
    assert focused == ["titlebar.search"]

    rig.press(Qt.Key.Key_Left)
    assert rig.current == "titlebar.album"
    assert focused == ["titlebar.search", "titlebar.album"]


def test_move_at_boundary_is_not_consumed(rig):
    """区域边界上方向键没得走 → 不消费（事件继续冒泡）。"""
    rig.open_page("album")
    rig.enter_region("window.album", rig.window.album.close_button)
    rig.nav.set_current(NAV_PAGE_ID, "album.close")   # 第 0 行，↑ 是边界
    assert rig.current == "album.close"

    assert rig.handle(Qt.Key.Key_Up) is False
    assert rig.current == "album.close"


def test_enter_runs_node_action(rig):
    """Enter 落在按钮节点上：``action``（= widget.click）被调用。"""
    clicks: list[int] = []
    rig.window.album.add_button.clicked.connect(lambda: clicks.append(1))

    rig.open_page("album")
    rig.enter_region("window.album", rig.window.album.album_view)
    rig.nav.set_current(NAV_PAGE_ID, "album.add")

    rig.press(Qt.Key.Key_Return)
    assert clicks == [1]


def test_enter_on_leaf_is_not_consumed(rig):
    """叶域里 Enter 让渡给控件：nav 不消费（列表激活由控件自己做）。"""
    rig.enter_region("window.content", rig.window.content)
    assert rig.current == "content.body"

    assert rig.handle(Qt.Key.Key_Return) is False
    assert rig.current == "content.body"


# ==================== 分派：Tab / Shift+Tab / Esc ====================

def test_tab_pushes_from_region_gateway(rig):
    """区域入口上 Tab：进区域内部（父帧 current 不更新）。

    焦点必须落在**入口控件本身**（``title_bar`` 容器，祖先链映射到
    ``window.titlebar``）。若停在入口内的某个按钮上，反向同步会先把光标
    push 进区域（``titlebar.album``），那一下 Tab 就变成了「叶节点上的
    Tab」——nav 不消费，交给 Qt 焦点链，用例断言会随平台漂移。
    """
    rig.nav.set_current(NAV_PAGE_ID, "window.titlebar")
    rig.focus(rig.window.title_bar)
    assert rig.current == "window.titlebar"

    # 还没 push：栈 = [page_focus, window]，父帧就是栈顶那一帧
    parent_before = rig.nav.restore_page(NAV_PAGE_ID).stack[-1].current
    assert parent_before == "window.titlebar"

    rig.press(Qt.Key.Key_Tab)

    assert rig.frame.domain_id == "titlebar"
    assert rig.current == "titlebar.album"
    # ``enter`` 不动父帧：入口还是我们刚才 ``set_current`` 的那个
    assert rig.nav.restore_page(NAV_PAGE_ID).stack[-2].current == parent_before


def test_shift_tab_pops_the_region(rig):
    rig.enter_region("window.titlebar", rig.window.title_bar.album_button)

    rig.press(Qt.Key.Key_Backtab, Qt.KeyboardModifier.ShiftModifier)
    assert rig.frame.domain_id == "window"
    # 反向同步的 push 把父帧 current 设成了入口，于是弹回落在入口上
    assert rig.current == "window.titlebar"


def test_esc_pops_the_region(rig):
    rig.enter_region("window.titlebar", rig.window.title_bar.album_button)

    rig.press(Qt.Key.Key_Escape)
    assert rig.frame.domain_id == "window"


def test_esc_at_root_is_not_consumed(rig):
    """根域虚拟根上按 Esc：nav 无事可做 → 不消费（交给上层关页）。"""
    assert rig.current is None
    # 焦点放在没有注册祖先的控件上（背景层），否则反向同步会先把光标推进区域
    rig.focus(rig.window.back_ground)
    assert rig.current is None
    assert rig.handle(Qt.Key.Key_Escape) is False
    assert rig.frame.domain_id == "window"      # 根帧没被弹掉
    assert rig.nav.restore_page(NAV_PAGE_ID).stack


# ==================== 让渡 ====================

def test_leaf_domain_passes_arrows_through(rig):
    """内容层叶域：方向键不归 nav（由虚拟滚动网格自己消费）。"""
    rig.enter_region("window.content", rig.window.content)
    assert rig.current == "content.body"

    for key in (Qt.Key.Key_Up, Qt.Key.Key_Down,
                Qt.Key.Key_Left, Qt.Key.Key_Right):
        assert rig.handle(key) is False
        assert rig.current == "content.body"


def test_leaf_domain_keeps_esc_for_nav(rig):
    """叶域只让渡方向键 + 回车：Esc 仍归 nav（弹出内容层）。"""
    rig.enter_region("window.content", rig.window.content)

    rig.press(Qt.Key.Key_Escape)
    assert rig.frame.domain_id == "window"


def test_non_nav_key_bubbles(rig):
    """非导航键一律冒泡（字母 / F 键 / 空格都不碰）。"""
    rig.enter_region("window.titlebar", rig.window.title_bar.album_button)

    for key in (Qt.Key.Key_A, Qt.Key.Key_F5, Qt.Key.Key_Space):
        assert rig.handle(key) is False


# ==================== 快捷键（keymap） ====================

def test_ctrl_key_goes_to_keymap(rig):
    """带修饰键 → keymap.trigger，命中即消费，nav 光标不动。"""
    calls: list[str] = []
    rig.shortcuts.register_slot(ShortcutSlot(
        id="view.fullscreen", handler=lambda: calls.append("hit"),
        default_keys=("Ctrl+F",)))
    rig.shortcuts.bind("Ctrl+F", "view.fullscreen")

    rig.enter_region("window.titlebar", rig.window.title_bar.album_button)
    before = rig.current

    event = rig.press(Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)

    assert calls == ["hit"]
    assert event.isAccepted() is True
    assert rig.current == before


def test_unbound_ctrl_key_bubbles(rig):
    rig.enter_region("window.titlebar", rig.window.title_bar.album_button)
    before = rig.current

    assert rig.handle(Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier) is False
    assert rig.current == before


def test_unmodified_shortcut_is_documented_gap(rig):
    """无修饰键的快捷键暂不分派给 keymap（已知缺口，见模块 docstring）。"""
    calls: list[str] = []
    rig.shortcuts.register_slot(ShortcutSlot(
        id="view.zoom_all", handler=lambda: calls.append("hit"),
        default_keys=("F5",)))
    rig.shortcuts.bind("F5", "view.zoom_all")
    rig.enter_region("window.titlebar", rig.window.title_bar.album_button)

    assert rig.handle(Qt.Key.Key_F5) is False
    assert calls == []


def test_shortcut_without_registry_is_skipped(rig, monkeypatch):
    monkeypatch.setattr(rig.presenter, "_shortcuts", None)
    rig.enter_region("window.titlebar", rig.window.title_bar.album_button)
    assert rig.handle(Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier) is False


def test_lone_modifier_key_bubbles(rig):
    """单独按修饰键（Meta / Ctrl / Alt）不是快捷键：不问 keymap，直接冒泡。

    回归：Qt 会把「只按住 Meta」派发成 ``Key_Meta + MetaModifier``，PortableText
    出来是 ``Meta+Meta`` —— 非法序列，交给 keymap 会让每次按修饰键都抛
    ``InvalidKeySequence`` 并在日志里刷 traceback。
    """
    calls: list[str] = []
    rig.shortcuts.register_slot(ShortcutSlot(
        id="view.fullscreen", handler=lambda: calls.append("hit"),
        default_keys=("Ctrl+F",)))
    rig.shortcuts.bind("Ctrl+F", "view.fullscreen")

    rig.enter_region("window.titlebar", rig.window.title_bar.album_button)
    before = rig.current

    for key, modifier in (
            (Qt.Key.Key_Meta, Qt.KeyboardModifier.MetaModifier),
            (Qt.Key.Key_Control, Qt.KeyboardModifier.ControlModifier),
            (Qt.Key.Key_Alt, Qt.KeyboardModifier.AltModifier),
            (Qt.Key.Key_Shift, Qt.KeyboardModifier.ShiftModifier),
    ):
        assert rig.handle(key, modifier) is False

    assert calls == []
    assert rig.current == before


def test_invalid_key_text_does_not_explode(rig, caplog):
    """``_run_shortcut`` 拿到非法键位文本时安静冒泡，不抛异常、不打 traceback。"""
    rig.enter_region("window.titlebar", rig.window.title_bar.album_button)

    with caplog.at_level("DEBUG"):
        assert rig.presenter._run_shortcut(
            QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Meta,
                      Qt.KeyboardModifier.MetaModifier)) is False

    assert not caplog.records or all(
        record.exc_info is None for record in caplog.records
    )


# ==================== 反向同步 ====================

def test_sync_within_region(rig):
    rig.enter_region("window.titlebar", rig.window.title_bar.album_button)

    rig.focus(rig.window.title_bar.extra_button)
    assert rig.current == "titlebar.extra"


def test_sync_across_regions(rig):
    """点开相册页再点按钮：nav 从根域逐层 push 到 ``album`` 域。"""
    rig.open_page("album")
    assert rig.frame.domain_id == "window"

    rig.focus(rig.window.album.add_button)
    assert rig.frame.domain_id == "album"
    assert rig.current == "album.add"


def test_sync_from_deeper_region_pops_first(rig):
    """从详情页点回内容层：先弹栈再落到正确区域。

    叠层开着时内容层被盖住、不可落点（``wiring._overlay_blank``），所以先把
    页收起来 —— 真实交互里也只有这时候才点得到内容层。
    """
    rig.open_page("detail")
    rig.focus(rig.window.detail.image_viewer)
    assert rig.frame.domain_id == "detail"

    rig.window.close_overlay()
    rig.app.processEvents()
    rig.focus(rig.window.content)
    assert rig.frame.domain_id == "content"
    assert rig.current == "content.body"


def test_sync_on_composite_child_maps_to_leaf(rig):
    """焦点落在复合控件内部输入框 → 同步到它所属的叶节点。"""
    rig.open_page("detail")

    rig.focus(rig.window.detail.tag_bar.tag_line_edit)
    assert rig.frame.domain_id == "detail"
    assert rig.current == "detail.tagbar"


def test_sync_ignores_widget_without_registered_ancestor(rig):
    """祖先链上没有注册节点的控件（背景层）：光标不动，也不报错。"""
    rig.nav.set_current(NAV_PAGE_ID, "window.titlebar")
    assert rig.presenter.node_id_for_widget(rig.window.back_ground) is None
    assert rig.presenter.sync_to_widget(rig.window.back_ground) is False
    assert rig.current == "window.titlebar"


def test_sync_maps_child_widgets_to_ancestor_node(rig):
    """子控件（标题标签）沿祖先链落到它所属的区域入口上。"""
    assert rig.presenter.node_id_for_widget(
        rig.window.title_bar.window_title) == "window.titlebar"


def test_focus_changed_handler_delegates(rig):
    """``focusChanged`` 的槽就是 ``sync_to_widget`` 的薄包装。"""
    # 相册页没开 → 该区域的控件不可见，不同步
    rig.presenter._on_focus_changed(None, rig.window.album.album_view)  # noqa: SLF001
    assert rig.frame.domain_id == "window"

    rig.open_page("album")
    rig.presenter._on_focus_changed(None, rig.window.album.album_view)  # noqa: SLF001
    assert rig.frame.domain_id == "album"
    assert rig.current == "album.cover"


def test_sync_skips_hidden_target(rig):
    """隐藏节点的焦点（页面没开）不把光标带进看不见的区域。"""
    assert rig.presenter.sync_to_widget(rig.window.detail.image_viewer) is False
    assert rig.frame.domain_id == "window"


def test_foreign_window_widget_is_not_dispatched(rig):
    """别的顶层窗口里的控件：没有 page 映射 → 不同步、不分派。"""
    from PySide6.QtWidgets import QWidget

    other = QWidget()
    try:
        assert rig.presenter._page_id_for_widget(other) is None  # noqa: SLF001
        assert rig.presenter.sync_to_widget(other) is False
    finally:
        other.close()
        other.deleteLater()
        rig.app.processEvents()


# ==================== 暂停 / 模态 ====================

def test_suspend_blocks_dispatch(rig):
    rig.enter_region("window.titlebar", rig.window.title_bar.album_button)
    before = rig.current

    with rig.presenter.suspended():
        assert rig.presenter.handle_key_event(
            QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Right,
                      Qt.KeyboardModifier.NoModifier)) is False
        assert rig.current == before

    rig.press(Qt.Key.Key_Right)          # 恢复后照常
    assert rig.current == "titlebar.search"


def test_suspend_is_nestable(rig):
    rig.presenter.suspend()
    rig.presenter.suspend()
    rig.presenter.resume()
    assert rig.presenter.is_suspended is True
    rig.presenter.resume()
    assert rig.presenter.is_suspended is False


def test_is_blocked_when_modal(rig, monkeypatch):
    """模态 / 弹窗期间整层不分派（``QDialog.exec`` / ``QMenu.exec`` 的情形）。"""
    rig.enter_region("window.titlebar", rig.window.title_bar.album_button)
    before = rig.current

    monkeypatch.setattr(
        rig.presenter, "is_blocked_for_test", None, raising=False)
    monkeypatch.setattr(
        QApplication, "activeModalWidget", staticmethod(lambda: object()))

    assert rig.presenter._is_blocked() is True  # noqa: SLF001
    assert rig.handle(Qt.Key.Key_Right) is False
    assert rig.current == before


def test_event_filter_swallows_consumed_keys(rig):
    """事件过滤器：消费时不冒泡（控件收不到 KeyPress）。"""
    from PySide6.QtWidgets import QPushButton

    seen: list[QEvent] = []

    class Watcher(QPushButton):
        def keyPressEvent(self, event):  # noqa: N802 - Qt 命名
            seen.append(event)
            super().keyPressEvent(event)

    watcher = Watcher(rig.window.title_bar.tool_bar_holder)
    watcher.show()
    rig.enter_region("window.titlebar", rig.window.title_bar.album_button)
    rig.nav.set_current(NAV_PAGE_ID, "titlebar.album")

    event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Right,
                      Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(watcher, event)

    assert event.isAccepted() is True
    assert rig.current == "titlebar.search"
    assert seen == []          # 被 nav 吃掉，控件没收到

    watcher.deleteLater()
    rig.app.processEvents()


# ==================== 工厂登记 ====================

def test_registered_as_child_presenter():
    """工厂登记进 presenter 表：没有窗口时安静返回 ``None``。"""
    from albuswall.ui.presenter import registered_presenters

    factories = [
        factory for factory in registered_presenters()
        if getattr(factory, "__module__", "") ==
        "albuswall.ui.presenter.nav_presenter"
    ]
    assert factories, "NavPresenter 的工厂没有登记进 presenter 表"

    class _NoWindow:
        window = None

    assert [factory(Container(), _NoWindow()) for factory in factories] == [None]


# ==================== 自因焦点不反推（退出区域要「弹得出去」） ====================

def test_leaving_a_region_stays_left(rig):
    """区域最后一行再按 ↓：``exit_back`` 弹出后停在入口上，不回弹。"""
    rig.enter_region("window.titlebar", rig.window.title_bar.album_button)
    rig.nav.set_current(NAV_PAGE_ID, "titlebar.minimize")   # 第二行

    _ = rig.press(Qt.Key.Key_Down)

    assert rig.frame.domain_id == "window"
    # 反向同步的 push 会把父帧 current 设成入口，于是停在入口上
    assert rig.current == "window.titlebar"

    # 再 Tab 才是「重新进入」，不是自动弹回去
    rig.press(Qt.Key.Key_Tab)
    assert rig.frame.domain_id == "titlebar"
    assert rig.current == "titlebar.album"


def test_self_induced_focus_is_not_reinterpreted(rig):
    """自己发起的 ``focus_node`` 期间到达的 focusChanged 不反推光标。"""
    rig.enter_region("window.titlebar", rig.window.title_bar.album_button)
    assert rig.current == "titlebar.album"

    rig.presenter._applying_focus = True          # noqa: SLF001 - 模拟 _focus 期间
    try:
        rig.presenter._on_focus_changed(               # noqa: SLF001
            None, rig.window.title_bar.extra_button)
        assert rig.current == "titlebar.album"
    finally:
        rig.presenter._applying_focus = False      # noqa: SLF001
