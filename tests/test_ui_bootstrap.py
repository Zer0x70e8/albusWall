#
"""界面 boot 层的契约：被动主循环 + ``setup_ui`` / ``show_ui`` + ``builtin_ui``。

三组用例分别锁住重构后的三条边界：

* :class:`albuswall.ui.application.Application` 是**被动**的——没有
  ``setup`` / ``show`` / ``set_language`` 这类 boot 入口，也不再持有窗口与
  presenter（装配不再是它的职责）；
* :func:`albuswall.ui.bootstrap.setup_ui` 是装配的**唯一**落点：语言 →
  主窗口 → presenter，并登记 ``ui_app`` / ``i18n`` / ``main_window`` /
  ``presenter`` 四个容器键；``show_ui`` 只负责显示，没有窗口时静默跳过；
* ``builtin_ui`` 插件承担 boot 编排：``select_loop`` 投候选、候选工厂里跑
  ``setup_ui``、``loop`` hook 里跑 ``show_ui``（与 ``main.py::_boot`` 对位）。

Qt 走 offscreen（``QT_QPA_PLATFORM=offscreen``，必须在 import PySide6 之前
设好）；整个进程复用同一个 ``QApplication``，与 ``tests/test_ui_presenter.py``
同一策略。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest

from albuswall.core.container import Container

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")


# ==================== fixture ====================

@pytest.fixture(scope="module")
def qapp() -> Iterator[Any]:
    """进程里唯一的 ``QApplication``（``Application`` 不能再建第二个）。"""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def container() -> Container:
    return Container()


@pytest.fixture(autouse=True)
def reset_language() -> Iterator[None]:
    """``setup_ui`` 会把翻译器装到进程唯一的 ``QApplication`` 上（全局状态）。

    用例结束后撤掉，否则后面期待「源文（中文）」的用例会读到译文。
    """
    yield
    from albuswall.ui.i18n import get_manager

    get_manager().reset()


# ==================== 被动主循环 ====================

def test_application_has_no_boot_entrypoints():
    """UI 主类不提供装配入口：boot 归 bootstrap 层 / builtin_ui 插件。"""
    from albuswall.ui.application import Application

    for name in ("setup", "show", "_ensure_setup", "_ensure_show",
                 "set_language", "main_window", "presenter", "translations",
                 "nav_registry"):
        assert not hasattr(Application, name), (
            f"Application.{name} 又出现了：装配逻辑应留在 "
            f"albuswall.ui.bootstrap / builtin_ui")


def test_application_is_still_a_main_loop():
    """被动不等于残废：``MainLoop`` 的三个方法必须齐全。"""
    from albuswall.core.main_loop import MainLoop
    from albuswall.ui.application import Application

    assert issubclass(Application, MainLoop)
    for name in ("run", "quit", "call_soon"):
        assert callable(getattr(Application, name))


# ==================== setup_ui / show_ui ====================

def test_setup_ui_registers_container_keys(qapp, container):
    from albuswall.ui import bootstrap

    window = bootstrap.setup_ui(container, qapp)

    assert container.get(bootstrap.UI_APP_KEY) is qapp
    assert container.get(bootstrap.MAIN_WINDOW_KEY) is window
    assert container.get(bootstrap.I18N_KEY) is not None
    # presenter 树在窗口显示之前就建好（主题先套，避免闪一下无主题）
    assert container.get("presenter") is container.get("main_presenter")
    assert window.windowTitle() == bootstrap.DEFAULT_TITLE


# ---------------- 主窗口来自容器（派生类替换） ----------------

class _DerivedWindow:
    """派生窗口的最小替身：只需要 ``windowTitle/setWindowTitle/size/resize``。"""

    def __init__(self) -> None:
        self._title = ""
        self._size = (0, 0)

    def windowTitle(self) -> str:
        return self._title

    def setWindowTitle(self, title: str) -> None:
        self._title = title

    def size(self):
        from PySide6.QtCore import QSize

        return QSize(*self._size)

    def resize(self, width: int, height: int) -> None:
        self._size = (width, height)


def test_ensure_main_window_uses_window_from_container(container):
    """容器里已有主窗口：原样用，不再 new 默认 ``Window``。"""
    from albuswall.ui import bootstrap

    derived = _DerivedWindow()
    container.set(bootstrap.MAIN_WINDOW_KEY, derived)

    assert bootstrap.ensure_main_window(container) is derived
    assert container.get(bootstrap.MAIN_WINDOW_KEY) is derived


def test_ensure_main_window_fills_only_missing_defaults(container):
    """派生窗口自己设过的标题 / 尺寸不被覆盖，没设的才补默认值。"""
    from albuswall.ui import bootstrap

    derived = _DerivedWindow()
    derived.setWindowTitle("Custom")
    derived.resize(400, 300)
    container.set(bootstrap.MAIN_WINDOW_KEY, derived)

    bootstrap.ensure_main_window(container)

    assert derived.windowTitle() == "Custom"
    assert derived.size().toTuple() == (400, 300)

    empty = _DerivedWindow()
    container.set(bootstrap.MAIN_WINDOW_KEY, empty)
    bootstrap.ensure_main_window(container)

    assert empty.windowTitle() == bootstrap.DEFAULT_TITLE
    assert empty.size().toTuple() == bootstrap.DEFAULT_SIZE


def test_ensure_main_window_accepts_registered_factory(container):
    """``container.register`` 的工厂同样能替换主窗口（容器自己的解析规则）。"""
    from albuswall.ui import bootstrap

    container.register(bootstrap.MAIN_WINDOW_KEY, _DerivedWindow)

    window = bootstrap.ensure_main_window(container)

    assert isinstance(window, _DerivedWindow)
    assert container.get(bootstrap.MAIN_WINDOW_KEY) is window


def test_setup_ui_uses_derived_window_subclass(qapp, container):
    """派生窗口走完整 boot：不碰 ``ui/window/`` 一行代码，presenter 照样挂上。"""
    from albuswall.ui import bootstrap
    from albuswall.ui.window import Window

    class MyWindow(Window):
        """只继承、不改：boot 只认容器里的那一个。"""

    derived = MyWindow()
    container.set(bootstrap.MAIN_WINDOW_KEY, derived)

    window = bootstrap.setup_ui(container, qapp)

    assert window is derived
    assert container.get(bootstrap.MAIN_WINDOW_KEY) is derived
    assert container.get("presenter") is not None
    derived.close()


def test_setup_ui_applies_configured_language(qapp, container):
    from albuswall.ui import bootstrap

    bootstrap.setup_ui(container, qapp)

    i18n = container.get(bootstrap.I18N_KEY)
    assert i18n.language == bootstrap.configured_language()


def test_setup_ui_does_not_show_the_window(qapp, container):
    from albuswall.ui import bootstrap

    window = bootstrap.setup_ui(container, qapp)

    assert window.isVisible() is False


def test_show_ui_shows_the_window(qapp, container):
    from albuswall.ui import bootstrap

    window = bootstrap.setup_ui(container, qapp)
    bootstrap.show_ui(container)

    assert window.isVisible() is True
    window.hide()


def test_show_ui_shows_the_window_currently_in_container(qapp, container):
    """显示时**现取**容器：boot 与 show 之间换掉 ``main_window`` 显示的是新窗口。

    替换窗口的正经时机是 UI boot 之前（presenter 在 boot 时就绑定了窗口）；
    这条只锁「显示不缓存 setup 那一刻的窗口引用」。
    """
    from albuswall.ui import bootstrap
    from albuswall.ui.window import Window

    bootstrap.setup_ui(container, qapp)
    replacement = Window()
    container.set(bootstrap.MAIN_WINDOW_KEY, replacement)

    bootstrap.show_ui(container)

    assert replacement.isVisible() is True
    replacement.hide()
    replacement.close()


def test_show_ui_without_window_is_noop(qapp, container):
    """headless / 没跑过 setup_ui：静默跳过，不抛。"""
    from albuswall.ui import bootstrap

    bootstrap.show_ui(container)

    assert container.get(bootstrap.MAIN_WINDOW_KEY) is None


# ==================== builtin_ui 插件：boot 编排 ====================

class _FakeUIApp:
    """被动主循环的替身：工厂只需要「造出来 + 登记进容器」。"""


def test_builtin_ui_plugin_boots_ui_across_three_hooks(
        monkeypatch, container):
    """插件的 boot 流程：工厂造被动主循环 → init 装配界面 → loop 显示。"""
    from albuswall.core.application import Application as Facade
    from albuswall.core.registry import hooks_map
    from albuswall.plugins import builtin_ui as plugin_module
    from albuswall.ui import application as app_module
    from albuswall.ui import bootstrap

    booted: list[tuple[object, object]] = []
    shown: list[object] = []
    candidates: list[tuple[tuple[object, ...], dict[str, object]]] = []

    monkeypatch.setattr(app_module, "Application", _FakeUIApp)
    monkeypatch.setattr(
        bootstrap, "setup_ui", lambda c, app: booted.append((c, app)))
    monkeypatch.setattr(bootstrap, "show_ui", lambda c: shown.append(c))
    monkeypatch.setattr(
        Facade, "provide_slot",
        classmethod(
            lambda cls, *args, **kwargs: candidates.append((args, kwargs))))

    init_before = list(hooks_map["init"])
    loop_before = list(hooks_map["loop"])
    select_before = list(hooks_map["select_loop"])
    try:
        assert plugin_module.builtin_ui(container) is None

        added_init = [h for h in hooks_map["init"] if h not in init_before]
        added_loop = [h for h in hooks_map["loop"] if h not in loop_before]
        added_select = [
            h for h in hooks_map["select_loop"] if h not in select_before]
        assert len(added_init) == 1
        assert len(added_loop) == 1
        assert len(added_select) == 1
        # 装配 / 显示都插在头部：其它插件的同名 hook 跑之前就绪
        assert hooks_map["init"][0] is added_init[0]
        assert hooks_map["loop"][0] is added_loop[0]

        # ---- select_loop：投 main_loop 候选 ----
        added_select[0]()
        assert len(candidates) == 1
        args, kwargs = candidates[0]
        assert args[0] == "main_loop"
        assert args[1] == "builtin_ui"
        assert kwargs["source"] == "plugin:builtin_ui"
        factory = args[2]

        # ---- 候选工厂：只造被动主循环 + 登记 ui_app，**不**装配界面 ----
        app = factory(container)
        assert isinstance(app, _FakeUIApp)
        assert container.get(bootstrap.UI_APP_KEY) is app
        assert booted == []

        # ---- init hook：界面装配 ----
        added_init[0]()
        assert booted == [(container, app)]

        # ---- loop hook：显示窗口（hook 签名是 ``hook(app)``） ----
        added_loop[0](None)
        assert shown == [container]
    finally:
        hooks_map["init"][:] = init_before
        hooks_map["loop"][:] = loop_before
        hooks_map["select_loop"][:] = select_before


def test_builtin_ui_init_hook_skips_foreign_main_loop(
        monkeypatch, container):
    """主循环被换成别的实现（容器里没有 ui_app）：init hook 不装配界面。"""
    from albuswall.core.registry import hooks_map
    from albuswall.plugins import builtin_ui as plugin_module
    from albuswall.ui import bootstrap

    booted: list[tuple[object, object]] = []
    monkeypatch.setattr(
        bootstrap, "setup_ui", lambda c, app: booted.append((c, app)))

    init_before = list(hooks_map["init"])
    try:
        assert plugin_module.builtin_ui(container) is None
        added_init = [h for h in hooks_map["init"] if h not in init_before]
        assert len(added_init) == 1

        added_init[0]()

        assert booted == []
    finally:
        hooks_map["init"][:] = init_before


def test_builtin_ui_plugin_is_silent_without_pyside(monkeypatch, container):
    """PySide6 / ui 层不可用：不抛，也不挂任何 hook（主循环回落 headless）。"""
    import builtins

    from albuswall.core.registry import hooks_map
    from albuswall.plugins import builtin_ui as plugin_module

    real_import = builtins.__import__

    def fake_import(
            name: str, *args: Any, **kwargs: Any) -> Any:
        if name.startswith("albuswall.ui"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    loop_before = list(hooks_map["loop"])
    select_before = list(hooks_map["select_loop"])
    try:
        assert plugin_module.builtin_ui(container) is None
        assert hooks_map["loop"] == loop_before
        assert hooks_map["select_loop"] == select_before
    finally:
        hooks_map["loop"][:] = loop_before
        hooks_map["select_loop"][:] = select_before
