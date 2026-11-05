#
"""界面快捷键槽（``ui.presenter.shortcuts``）的契约：**只登记接口**。

覆盖四层：

* **登记表**：``UI_SLOTS`` 的命令集合与随包 ``shortcuts.ini`` 一一对应；
  重复登记幂等（``replace=True``），不会抛 ``DuplicateShortcutSlot``；
* **载入**：登记后 ``load()`` 能让每条槽都落到键位（``unbound_slots()``
  为空），键位以配置文件为准；
* **懒解析**：处理函数在触发时才从容器取 presenter —— 没有界面时静默
  no-op（返回 ``False``，不抛），有界面时落到对应子 presenter，
  ``asset.*`` 详情页优先、否则落到内容网格；
* **落点存在**：槽指向的方法在真实 presenter 类上确实存在（防止改名后
  静默失效），外加视图侧新增入口（全屏 / 关层 / 详情页缩放）的行为。

Qt 走 offscreen（``QT_QPA_PLATFORM=offscreen``，必须在 import PySide6 之前
设好）；整个进程复用同一个 ``QApplication``，与其它 UI 用例同一策略。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")


# ==================== fixture ====================

@pytest.fixture(scope="module")
def qapp() -> Iterator[Any]:
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _close_windows(qapp):
    """每个用例结束收掉残留的顶层窗口：旧窗口一直占着激活状态，下一个用例
    就抢不到焦点（按键到不了 builder）。"""
    from PySide6.QtWidgets import QApplication

    yield
    for widget in QApplication.topLevelWidgets():
        widget.close()
        widget.deleteLater()
    qapp.processEvents()


# ==================== 替身 ====================

class StubPresenter:
    """只暴露指定动作的子 presenter 替身：记下被调用的方法名。"""

    def __init__(
            self,
            name: str,
            methods: set[str],
            *,
            is_detail_open: bool = False,
    ) -> None:
        self.name = name
        self.calls: list[str] = []
        #: ``asset.*`` 的分流依据（只有 viewer 需要）。
        self.is_detail_open = is_detail_open
        for method in methods:
            setattr(self, method, self._recorder(method))

    def _recorder(self, method: str):
        def call() -> bool:
            self.calls.append(method)
            return True

        return call


class StubMain:
    """``MainPresenter`` 的替身：只需要 ``child(name)``。"""

    def __init__(self, children: dict[str, Any]) -> None:
        self._children = children

    def child(self, name: str) -> Any | None:
        return self._children.get(name)

    def child_names(self) -> list[str]:
        return list(self._children)


def _install(container, **kwargs):
    """用一个全新的注册表登记（不碰进程级单例，用例之间互不污染）。"""
    from albuswall.ui.keymap import ShortcutRegistry
    from albuswall.ui.presenter.shortcuts import install_ui_shortcuts

    return install_ui_shortcuts(
        container, registry=ShortcutRegistry(), **kwargs)


def _shipped_table():
    from albuswall.ui.keymap.config import ShortcutConfig

    return ShortcutConfig(user_path=None).load()


def _install_loaded(container):
    """登记 + 载入**随包**默认表（不看用户覆盖，用例才可重复）。"""
    from albuswall.ui.keymap import load_shortcuts
    from albuswall.ui.keymap.config import ShortcutConfig

    registry = _install(container, load=False)
    load_shortcuts(
        ShortcutConfig(user_path=None), registry=registry, strict=True)
    return registry


# ==================== 登记表 ====================

#: 随包表里写成 ``= []`` 的命令：功能在、默认不给键。
UNBOUND_BY_DEFAULT = frozenset({
    "window.album",
    "window.source",
    "viewer.next",
    "viewer.prev",
    "content.preview",
    "asset.restore",
})


def test_slot_table_matches_the_shipped_table():
    """代码里的命令集合 == 随包 ``shortcuts.ini`` 的命令集合。"""
    from albuswall.core import Container
    from albuswall.ui.presenter.shortcuts import UI_SLOTS

    registry = _install(Container(), load=False)

    assert registry.slot_ids() == tuple(spec.command for spec in UI_SLOTS)
    assert len(registry.slot_ids()) == 18
    assert set(registry.slot_ids()) == set(_shipped_table())
    # 只有那 6 条是默认解绑，别的一律有键位
    unbound = {command for command, binding in _shipped_table().items()
               if not binding.keys}
    assert unbound == set(UNBOUND_BY_DEFAULT)


def test_install_is_idempotent():
    """重复登记（两次 boot / 两个容器）不抛，槽数不翻倍。"""
    from albuswall.core import Container

    container = Container()
    first = _install(container, load=False)
    second = _install(container, load=False)

    assert first is not second  # 各用各的注册表
    assert second.slot_ids() == first.slot_ids()
    assert len(second.slots()) == 18


def test_install_loads_the_shipped_table():
    """载入默认表后每条槽都有键位，键位与配置文件一致。"""
    from albuswall.core import Container
    from albuswall.ui.keymap import load_shortcuts
    from albuswall.ui.keymap.config import ShortcutConfig

    registry = _install(Container(), load=False)
    assert registry.unbound_slots() == registry.slot_ids()  # 还没载入

    load_shortcuts(
        ShortcutConfig(user_path=None), registry=registry, strict=True)

    # 载入后只剩随包表刻意解绑的那几条
    assert set(registry.unbound_slots()) == set(UNBOUND_BY_DEFAULT)
    assert [str(k) for k in registry.keys_for("content.search")] == ["Ctrl+F"]
    assert registry.commands_for("Ctrl+F") == ("content.search",)
    assert [str(k) for k in registry.keys_for("window.fullscreen")] == [
        "F11", "Ctrl+Shift+F",
    ]


def test_install_default_load_path_does_not_raise():
    """默认 ``load=True``（读用户覆盖表）只做尽力而为：不抛异常。"""
    from albuswall.core import Container

    registry = _install(Container())

    assert len(registry.slots()) == 18


# ==================== 懒解析 / 触发 ====================

def test_trigger_without_presenter_is_a_noop():
    """没有界面（headless / 单测容器）时触发返回 ``False``，不抛。"""
    from albuswall.core import Container

    registry = _install_loaded(Container())

    for keys in ("Ctrl+Q", "Ctrl+,", "F11", "Ctrl+W", "Ctrl++",
                 "Ctrl+-", "Ctrl+0", "Ctrl+F", "Ctrl+N", "F2",
                 "Ctrl+D", "Delete"):
        assert registry.trigger(keys) == (False,), keys


def test_trigger_routes_to_the_matching_child():
    """键位 → 子 presenter 的公开方法（名字与 ``UI_SLOTS`` 一致）。"""
    from albuswall.core import Container

    window = StubPresenter(
        "window", {"quit", "open_settings", "toggle_fullscreen", "close_layer"})
    album = StubPresenter("album", {"create_album", "rename_current"})
    search = StubPresenter("search", {"focus_search"})
    viewer = StubPresenter(
        "viewer", {"zoom_in", "zoom_out", "reset_zoom"}, is_detail_open=True)
    content = StubPresenter("content", set())
    container = Container()
    container.set("presenter", StubMain({
        "window": window, "album": album, "search": search,
        "viewer": viewer, "content": content,
    }))

    registry = _install_loaded(container)

    assert registry.trigger("Ctrl+N") == (True,)
    assert album.calls == ["create_album"]
    assert registry.trigger("F2") == (True,)
    assert album.calls == ["create_album", "rename_current"]

    assert registry.trigger("F11") == (True,)
    assert window.calls == ["toggle_fullscreen"]
    assert registry.trigger("Ctrl+W") == (True,)
    assert window.calls == ["toggle_fullscreen", "close_layer"]
    assert registry.trigger("Ctrl+Q") == (True,)
    assert window.calls[-1] == "quit"
    assert registry.trigger("Ctrl+,") == (True,)
    assert window.calls[-1] == "open_settings"

    assert registry.trigger("Ctrl+F") == (True,)
    assert search.calls == ["focus_search"]

    assert registry.trigger("Ctrl++") == (True,)
    assert viewer.calls == ["zoom_in"]
    assert registry.trigger("Ctrl+-") == (True,)
    assert registry.trigger("Ctrl+0") == (True,)
    assert viewer.calls == ["zoom_in", "zoom_out", "reset_zoom"]


def test_asset_commands_prefer_the_detail_page():
    """``asset.*``：详情页开着落 viewer，收起后落内容网格。"""
    from albuswall.core import Container

    viewer = StubPresenter(
        "viewer", {"toggle_favorite", "trash_current"}, is_detail_open=True)
    content = StubPresenter(
        "content", {"toggle_favorite", "trash_current"}, is_detail_open=False)
    container = Container()
    container.set("presenter", StubMain({
        "viewer": viewer, "content": content,
    }))
    registry = _install_loaded(container)

    assert registry.trigger("Ctrl+D") == (True,)
    assert viewer.calls == ["toggle_favorite"]
    assert content.calls == []

    # 详情页收起（浮层换了页）：同样的键位改落到网格光标那一格
    viewer.is_detail_open = False
    assert registry.trigger("Ctrl+D") == (True,)
    assert content.calls == ["toggle_favorite"]
    assert viewer.calls == ["toggle_favorite"]

    assert registry.trigger("Delete") == (True,)
    assert content.calls == ["toggle_favorite", "trash_current"]


def test_missing_handler_is_skipped():
    """子 presenter 缺方法（或根本不是 presenter）：返回 ``False``，不抛。"""
    from albuswall.core import Container

    container = Container()
    container.set("presenter", StubMain({"album": object()}))
    registry = _install_loaded(container)

    assert registry.trigger("Ctrl+N") == (False,)


# ==================== 落点在真实 presenter 上存在 ====================

def test_slot_targets_exist_on_the_real_presenters():
    """``UI_SLOTS`` 提到的每个方法都真实存在（防止改名后静默失效）。"""
    from albuswall.ui.presenter.album_presenter import AlbumPresenter
    from albuswall.ui.presenter.content_presenter import ContentPresenter
    from albuswall.ui.presenter.search_presenter import SearchPresenter
    from albuswall.ui.presenter.shortcuts import CURRENT_ASSET, UI_SLOTS
    from albuswall.ui.presenter.viewer_presenter import ViewerPresenter
    from albuswall.ui.presenter.window_presenter import WindowPresenter

    presenters = {
        "window": WindowPresenter,
        "album": AlbumPresenter,
        "search": SearchPresenter,
        "viewer": ViewerPresenter,
        "content": ContentPresenter,
    }

    for spec in UI_SLOTS:
        targets = (
            (presenters["viewer"], presenters["content"])
            if spec.target == CURRENT_ASSET
            else (presenters[spec.target],)
        )
        for cls in targets:
            method = getattr(cls, spec.method, None)
            assert callable(method), f"{cls.__name__}.{spec.method} 不存在"
            assert not spec.method.startswith("_"), (
                f"{spec.command} 指向了私有方法 {spec.method}："
                f"快捷键的落点必须是 presenter 的公开接口")


# ==================== 视图侧新增入口 ====================

def test_window_fullscreen_toggle(qapp):
    from albuswall.ui.window import Window

    window = Window()
    window.show()
    try:
        assert window.toggle_fullscreen() is True
        assert window.isFullScreen() is True
        assert window.toggle_fullscreen() is False
        assert window.isFullScreen() is False
    finally:
        window.close()


def test_window_close_layer_closes_overlay_then_window(qapp):
    from albuswall.ui.window import Window

    window = Window()
    window.show()
    try:
        window.show_album()
        assert window.overlay.is_blank() is False

        # 第一次：只收浮层页，窗口还在
        assert window.close_layer() is True
        assert window.overlay.is_blank() is True
        assert window.isVisible() is True

        # 第二次：没有浮层页了 → 关窗口
        assert window.close_layer() is False
        assert window.isVisible() is False
    finally:
        window.close()


def test_detail_zoom_wrappers(qapp):
    """``view.zoom_*`` 的落点：Detail 转发到 ImageViewer。"""
    from PySide6.QtGui import QPixmap

    from albuswall.ui.window import Window

    window = Window()
    pixmap = QPixmap(20, 10)
    pixmap.fill()
    window.detail.set_image(pixmap)
    viewer = window.detail.image_viewer
    assert viewer.zoom_in() is True
    assert window.detail.zoom_in() is True
    assert window.detail.zoom_out() is True
    assert window.detail.reset_zoom() is True
    assert viewer._scale == pytest.approx(1.0)
    window.close()


def test_setup_ui_installs_a_working_binder(qapp):
    """界面 boot 之后**按键真的能用**：binder + 槽 + presenter 整条链。

    只挑与环境无关的键位：``Ctrl+F``（展开搜索栏）与 ``Ctrl+W``（收起浮层
    页）。``F11`` 在 offscreen 平台上会被平台插件强制 maximize、``showFullScreen``
    不生效，不适合当断言（``Window.toggle_fullscreen`` 本身有独立的视图用例）。
    """
    # 别的 UI 用例可能清过 configue 的登记表（``UIConf`` 的声明是 import
    # 期的副作用，清表不会重跑）：这里补一次，让 boot 读得到 ``[ui]``。
    from contextlib import suppress

    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from albuswall.configue import configclass
    from albuswall.core import Container
    from albuswall.ui import bootstrap
    from albuswall.ui.keymap import uninstall_shortcut_binder

    with suppress(Exception):
        configclass("ui", "main")(bootstrap.UIConf)

    container = Container()
    window = bootstrap.setup_ui(container, qapp)
    binder = container.get(bootstrap.SHORTCUT_BINDER_KEY)
    assert binder is not None and binder.installed
    assert binder.owner is window

    window.resize(320, 240)
    window.show()
    for _attempt in range(15):
        handle = window.windowHandle()
        if handle is not None:
            handle.requestActivate()
        window.content.setFocus()
        QTest.qWait(20)
        focused = qapp.focusWidget()
        # 上一个用例的窗口可能还占着焦点：等本窗口自己拿到焦点再用键盘
        if focused is not None and focused.window() is window:
            break

    try:
        # Ctrl+F：content.search → 展开搜索栏（焦点也跟着进去）
        QTest.keyClick(window.content, Qt.Key.Key_F,
                       Qt.KeyboardModifier.ControlModifier)
        assert window.title_bar.floating_search_bar.is_expanded is True

        # 搜完把焦点还给网格（真实使用里按键总是落到焦点控件上）
        window.title_bar.floating_search_bar.collapse()
        window.content.setFocus()
        qapp.processEvents()

        # Ctrl+W：window.close → 收起浮层页（相册页是纯视图，不需要服务）
        window.show_album()
        assert window.overlay.is_blank() is False
        QTest.keyClick(qapp.focusWidget() or window.content,
                       Qt.Key.Key_W, Qt.KeyboardModifier.ControlModifier)
        assert window.overlay.is_blank() is True
    finally:
        uninstall_shortcut_binder(qapp)
        window.close()


def test_search_presenter_focus_search(qapp):
    """``content.search`` 的落点：展开搜索栏并抢焦点。"""
    from albuswall.core import Container
    from albuswall.ui.presenter.search_presenter import SearchPresenter
    from albuswall.ui.window import Window

    window = Window()
    presenter = SearchPresenter(Container(), window=window)
    try:
        assert presenter.focus_search() is True
        bar = window.title_bar.floating_search_bar
        assert bar.is_expanded is True
        # offscreen 下窗口拿不到「激活」状态，``hasFocus()`` 不可靠；
        # 断言窗口的焦点控件就是输入框即可。
        assert window.focusWidget() is bar.line_edit
    finally:
        window.close()
