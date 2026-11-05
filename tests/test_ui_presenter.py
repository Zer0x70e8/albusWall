#
"""UI 表现层：主题解析 + presenter 树的生命周期编排。

分两半：

* ``theme`` 的用例**不需要 Qt** —— 它只读配置 / 找内置 QSS 文件；
* presenter 的用例走 offscreen Qt（``QT_QPA_PLATFORM=offscreen``，必须在
  import PySide6 之前设好），复用进程里唯一的 ``QApplication``：``Application``
  不能再建第二个 ``QApplication``，所以这里用真实的 ``QApplication`` +
  ``Window`` 走 ``MainPresenter.bootstrap`` 的完整路径。

``configue`` 的登记表是进程全局的（同 ``tests/test_configue/conftest.py``），
本模块自己清一遍，避免把 ``[ui]`` 的声明 / 文件登记漏给别的用例。
"""

from __future__ import annotations

import os
from contextlib import suppress

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")


# ==================== fixture ====================

@pytest.fixture
def isolated_configue():
    """每个用例一张全新的 configue 登记表 + 单例（进程全局状态）。

    清表会把**已经 import 过的** ``@configclass`` 声明一起清掉（声明是 import
    期的副作用，不会重跑），所以清完要把应用侧的配置类重新登记一遍 ——
    与真实启动顺序（先 import 声明、再 ``setup_config``）等价。
    """
    from albuswall.configue import (
        _LOAD_HOOKS,
        _PENDING_CONVERTERS,
        Configue,
        configclass,
    )
    from albuswall.configue.registry import (
        _CLASSES,
        _FIELD_OWNER,
        _FILES,
        _PATHS,
    )

    def redeclare() -> None:
        from albuswall.ui.presenter.theme import ThemeConfig

        configclass("ui", "main")(ThemeConfig)

    def reset() -> None:
        instance = Configue._instance
        if instance is not None:
            with suppress(Exception):
                instance.close()
        for table in (_FILES, _CLASSES, _PATHS, _FIELD_OWNER,
                      _LOAD_HOOKS, _PENDING_CONVERTERS):
            table.clear()
        Configue._instance = None
        redeclare()

    reset()
    try:
        yield
    finally:
        reset()


@pytest.fixture
def presenter_registry():
    """快照 / 恢复全局 presenter 工厂表（跨用例隔离）。"""
    from albuswall.ui.presenter import base

    saved = list(base.PRESENTER_FACTORIES)
    try:
        yield base.PRESENTER_FACTORIES
    finally:
        base.PRESENTER_FACTORIES[:] = saved


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def clean_app(qapp):
    """用完把应用样式表与主窗口收干净，避免拖给后面的用例。"""
    yield qapp
    qapp.setStyleSheet("")


# ==================== 测试用 presenter ====================

def _make_recorder():
    from albuswall.ui.presenter import Presenter

    class RecorderPresenter(Presenter):
        """把生命周期写进共享日志的 presenter。"""

        name = "recorder"

        def __init__(self, container, log, name, parent=None):
            super().__init__(container, parent=parent)
            self.log = log
            self.name = name

        def on_start(self):
            self.log.append(f"start:{self.name}")

        def on_stop(self):
            self.log.append(f"stop:{self.name}")

    return RecorderPresenter


# ==================== 主题解析（无 Qt） ====================

def test_resolve_theme_uses_bundled_default():
    from albuswall.ui.presenter import theme

    resolved = theme.resolve_theme()

    assert resolved.name == theme.DEFAULT_THEME
    assert resolved.source == "bundled"
    assert resolved.qss_path is not None
    assert resolved.qss_path.is_file()
    assert not resolved.is_empty


def test_resolve_theme_unknown_name_falls_back_to_default(caplog):
    from albuswall.ui.presenter import theme

    with caplog.at_level("WARNING", logger="albuswall.ui.presenter"):
        resolved = theme.resolve_theme("no-such-theme")

    assert resolved.name == theme.DEFAULT_THEME
    assert resolved.source == "bundled"
    assert "no-such-theme" in caplog.text


def test_resolve_theme_blank_name_means_configured_default():
    from albuswall.ui.presenter import theme

    assert theme.resolve_theme("   ").name == theme.DEFAULT_THEME
    assert theme.resolve_theme(None).name == theme.DEFAULT_THEME


def test_available_themes_contains_default():
    from albuswall.ui.presenter import theme

    assert theme.DEFAULT_THEME in theme.available_themes()


def test_explicit_style_sheet_wins_over_theme(isolated_configue, tmp_path):
    """``[ui] style_sheet`` 指向的文件存在时，直接用它的内容。"""
    from albuswall.configue.bootstrap import setup_config
    from albuswall.core import Container
    from albuswall.ui.presenter import theme

    qss = tmp_path / "custom.qss"
    qss.write_text("QWidget { color: red; }", encoding="utf-8")
    (tmp_path / "config.ini").write_text(
        "[ui]\ntheme = default\nstyle_sheet = " + str(qss) + "\n",
        encoding="utf-8",
    )
    setup_config(Container(), base_dir=tmp_path, portable=True)

    resolved = theme.resolve_theme()

    assert resolved.source == "config"
    assert resolved.qss_path == qss
    assert "color: red" in resolved.style_sheet


def test_missing_style_sheet_falls_back_to_bundled_theme(
        isolated_configue, tmp_path, caplog
):
    from albuswall.configue.bootstrap import setup_config
    from albuswall.core import Container
    from albuswall.ui.presenter import theme

    (tmp_path / "config.ini").write_text(
        "[ui]\nstyle_sheet = /nonexistent/theme.qss\n", encoding="utf-8",
    )
    setup_config(Container(), base_dir=tmp_path, portable=True)

    with caplog.at_level("WARNING", logger="albuswall.ui.presenter"):
        resolved = theme.resolve_theme()

    assert resolved.source == "bundled"
    assert resolved.name == theme.DEFAULT_THEME
    assert "style_sheet" in caplog.text


def test_configured_theme_without_configue_uses_declared_default():
    """UI 被单独 import（没有 ``Configue`` 单例）时读默认值，不抛。"""
    from albuswall.ui.presenter import theme

    assert theme.configured_theme() == theme.DEFAULT_THEME
    assert theme.configured_style_sheet() == ""


# ------------ 配置器插值：主题路径由 [path] / [files] / [ui] 组合 ------------

def _write_shipped_conf(tmp_path):
    """把随包 ``conf.ini`` 拷成用户配置（它的 [ui] 组合式就是默认值文档）。"""
    from albuswall import resources

    shipped = resources.CURRENT_PATH / "config" / "conf.ini"
    (tmp_path / "config.ini").write_text(
        shipped.read_text(encoding="utf-8"), encoding="utf-8")


def test_shipped_conf_composes_theme_paths(isolated_configue, tmp_path):
    """``[ui]`` 的组合式由配置器展开：路径 = [path] + [ui] theme + [files]。"""
    from albuswall.configue.bootstrap import setup_config
    from albuswall.core import Container
    from albuswall.ui.presenter.theme import ThemeConfig

    _write_shipped_conf(tmp_path)
    setup_config(Container(), base_dir=tmp_path, portable=True)

    theme_root = tmp_path.resolve() / "theme"
    config = ThemeConfig()
    assert config.style_sheet == str(
        theme_root / "default" / "main_window.qss")
    assert config.album_button == str(
        theme_root / "default" / "icons" / "album_button.svg")
    assert config.favorite_icon == str(
        theme_root / "default" / "icons" / "favorite_icon.svg")


def test_defaults_equal_shipped_conf(isolated_configue, tmp_path):
    """没写配置文件时，声明期默认值展开出的路径与 ``conf.ini`` 的**一致**。

    两边必须逐字等价：模板是给人看的文档，真正兜底的是 ``ThemeConfig`` /
    ``Paths`` 的默认值，二者漂移过一次（``${ui:theme}`` 多写了一段）。
    """
    from albuswall.configue.bootstrap import setup_config
    from albuswall.core import Container
    from albuswall.ui.presenter.theme import ThemeConfig

    setup_config(
        Container(), base_dir=tmp_path, portable=True, ignore_missing=True)

    theme_root = tmp_path.resolve() / "theme" / "default"
    config = ThemeConfig()
    assert config.style_sheet == str(theme_root / "main_window.qss")
    assert config.app_icon == str(theme_root / "icons" / "albuswall.svg")
    assert config.more_button == str(
        theme_root / "icons" / "more_button.svg")


def test_switching_theme_moves_every_composed_path(isolated_configue, tmp_path):
    """回写 ``[ui] theme`` 就够：派生路径跟着 ``${ui:theme}`` 重新展开。

    这是「UI 走配置器解析」的核心证据 —— UI 侧没有任何重新拼路径的代码。
    """
    from albuswall.configue.bootstrap import Paths, setup_config
    from albuswall.core import Container
    from albuswall.ui.presenter.theme import ThemeConfig

    _write_shipped_conf(tmp_path)
    setup_config(Container(), base_dir=tmp_path, portable=True)

    ThemeConfig().theme = "midnight"

    theme_root = tmp_path.resolve() / "theme"
    assert Paths().icons == theme_root / "midnight" / "icons"
    assert ThemeConfig().style_sheet == str(
        theme_root / "midnight" / "main_window.qss")
    assert ThemeConfig().more_button == str(
        theme_root / "midnight" / "icons" / "more_button.svg")


def test_user_theme_dir_wins_over_bundled(isolated_configue, tmp_path):
    """用户主题目录里有文件就用它；缺的图标回退到随包主题。"""
    from albuswall.configue.bootstrap import setup_config
    from albuswall.core import Container
    from albuswall.ui.presenter import theme

    _write_shipped_conf(tmp_path)
    setup_config(Container(), base_dir=tmp_path, portable=True)

    theme_dir = tmp_path / "theme" / "default"
    icons = theme_dir / "icons"
    icons.mkdir(parents=True)
    (theme_dir / "main_window.qss").write_text(
        "QWidget { color: red; }", encoding="utf-8")
    album = icons / "album_button.svg"
    album.write_text("<svg/>", encoding="utf-8")

    resolved = theme.resolve_theme()

    assert resolved.source == "config"
    assert "color: red" in resolved.style_sheet
    assert resolved.icon("album_button") == album
    # 用户主题没放的图标：落到随包 default 主题
    fallback = resolved.icon("more_button")
    assert fallback is not None and fallback.is_file()
    assert fallback.parent.name == theme.ICON_DIR_NAME
    assert fallback.parent.parent.name == theme.DEFAULT_THEME


def test_icon_key_points_at_any_file(isolated_configue, tmp_path):
    """「图标都可以被配置」：``[ui] <key>`` 指到任意文件都算数。"""
    from albuswall.configue.bootstrap import setup_config
    from albuswall.core import Container
    from albuswall.ui.presenter import theme

    _write_shipped_conf(tmp_path)
    custom = tmp_path / "art" / "heart.svg"
    custom.parent.mkdir()
    custom.write_text("<svg/>", encoding="utf-8")
    (tmp_path / "config.ini").write_text(
        (tmp_path / "config.ini").read_text(encoding="utf-8").replace(
            "favorite_icon = ${path:icons}/${files:favorite_icon}",
            f"favorite_icon = {custom}",
        ),
        encoding="utf-8",
    )
    setup_config(Container(), base_dir=tmp_path, portable=True)

    resolved = theme.resolve_theme()

    assert resolved.icon("favorite_icon") == custom
    assert resolved.icon("album_button") is not None  # 没改的键照旧


def test_explicit_name_uses_the_same_layout(isolated_configue, tmp_path):
    """显式主题名按同一套目录布局找用户主题（不认 ``[ui] style_sheet``）。"""
    from albuswall.configue.bootstrap import setup_config
    from albuswall.core import Container
    from albuswall.ui.presenter import theme

    _write_shipped_conf(tmp_path)
    setup_config(Container(), base_dir=tmp_path, portable=True)

    target = tmp_path / "theme" / "midnight"
    target.mkdir(parents=True)
    (target / "main_window.qss").write_text(
        "QWidget { color: teal; }", encoding="utf-8")

    resolved = theme.resolve_theme("midnight")

    assert resolved.name == "midnight"
    assert resolved.source == "config"
    assert resolved.qss_path == target / "main_window.qss"
    assert theme.configured_theme_qss("midnight") == target / "main_window.qss"


# ==================== Presenter 树 ====================

def test_children_start_in_order_and_stop_in_reverse(presenter_registry):
    from albuswall.core import Container

    Recorder = _make_recorder()
    log: list[str] = []

    root = Recorder(Container(), log, "root")
    child_a = Recorder(Container(), log, "a")
    child_b = Recorder(Container(), log, "b")
    root.add_child(child_a)
    root.add_child(child_b)

    root.start()

    assert log == ["start:root", "start:a", "start:b"]
    assert child_a.parent is root
    assert root.child("b") is child_b
    assert root.require_child("a") is child_a

    log.clear()
    root.stop()

    assert log == ["stop:b", "stop:a", "stop:root"]
    assert not root.is_started
    assert not child_a.is_started


def test_start_stop_are_idempotent(presenter_registry):
    from albuswall.core import Container

    Recorder = _make_recorder()
    log: list[str] = []

    root = Recorder(Container(), log, "root")
    root.add_child(Recorder(Container(), log, "a"))
    root.start()
    root.start()
    root.stop()
    root.stop()

    assert log == ["start:root", "start:a", "stop:a", "stop:root"]


def test_add_child_rejects_duplicate_name(presenter_registry):
    from albuswall.core import Container

    Recorder = _make_recorder()
    root = Recorder(Container(), [], "root")
    root.add_child(Recorder(Container(), [], "dup"))

    with pytest.raises(ValueError):
        root.add_child(Recorder(Container(), [], "dup"))


def test_hot_add_starts_child_immediately(presenter_registry):
    from albuswall.core import Container

    Recorder = _make_recorder()
    root = Recorder(Container(), [], "root")
    root.start()

    log: list[str] = []
    child = Recorder(Container(), log, "late")
    root.add_child(child)

    assert child.is_started
    assert log == ["start:late"]

    assert root.remove_child("late") is child
    assert log == ["start:late", "stop:late"]
    assert child.parent is None


def test_failing_start_rolls_back_started_children(presenter_registry):
    from albuswall.core import Container
    from albuswall.ui.presenter import Presenter

    class Boom(Presenter):
        name = "boom"

        def on_start(self):
            raise RuntimeError("boom")

    Recorder = _make_recorder()
    log: list[str] = []

    root = Recorder(Container(), log, "root")
    child = Recorder(Container(), log, "child")
    root.add_child(child)
    root.add_child(Boom(Container()))

    with pytest.raises(RuntimeError, match="boom"):
        root.start()

    # 整棵树回滚：先起的子节点被停掉，根节点标记为未启动。
    assert not root.is_started
    assert not child.is_started
    assert log == ["start:root", "start:child", "stop:child"]


# ==================== 主 presenter ====================

def test_main_presenter_bootstrap_registers_aliases_and_theme(
        clean_app, presenter_registry
):
    from albuswall.core import Container
    from albuswall.ui.presenter import MainPresenter
    from albuswall.ui.window import Window

    container = Container()
    window = Window()
    try:
        presenter = MainPresenter.bootstrap(
            container, app=clean_app, window=window)

        assert container.get("presenter") is presenter
        assert container.get("main_presenter") is presenter
        assert presenter.app is clean_app
        assert presenter.window is window
        assert presenter.is_started
        assert presenter.theme is not None
        assert presenter.theme_name == "default"
        assert clean_app.styleSheet().strip() != ""

        # 幂等：再 bootstrap 一次拿到同一个对象，不会重复启动
        again = MainPresenter.bootstrap(container, app=clean_app)
        assert again is presenter
        assert again.window is window
    finally:
        container.get("presenter").stop()
        window.close()


def test_main_presenter_builds_and_starts_children(
        clean_app, presenter_registry
):
    from albuswall.core import Container
    from albuswall.ui.presenter import MainPresenter, register_presenter
    from albuswall.ui.window import Window

    Recorder = _make_recorder()
    log: list[str] = []

    @register_presenter
    def _child(container, main):
        # 工厂拿到容器与父 presenter：这就是子 presenter 的依赖注入入口。
        assert isinstance(main, MainPresenter)
        return Recorder(container, log, "child")

    container = Container()
    window = Window()
    try:
        presenter = MainPresenter.bootstrap(
            container, app=clean_app, window=window)

        child = presenter.child("child")
        assert child is not None
        assert child.is_started
        assert child.parent is presenter
        assert log == ["start:child"]
    finally:
        container.get("presenter").stop()
        window.close()


def test_set_theme_persists_only_when_it_takes_effect(
        clean_app, presenter_registry, isolated_configue, tmp_path, monkeypatch
):
    from albuswall.configue.bootstrap import setup_config
    from albuswall.core import Container
    from albuswall.ui.presenter import MainPresenter, theme
    from albuswall.ui.window import Window

    (tmp_path / "config.ini").write_text(
        "[ui]\ntheme = default\n", encoding="utf-8")
    setup_config(Container(), base_dir=tmp_path, portable=True)

    container = Container()
    window = Window()
    try:
        presenter = MainPresenter.bootstrap(
            container, app=clean_app, window=window)

        # 生效的名字才回写配置
        monkeypatch.setattr(
            "albuswall.ui.presenter.main.resolve_theme",
            lambda name=None: theme.Theme(name=name or "wanted",
                                          source="bundled"),
        )
        presenter.set_theme("wanted")
        assert theme.ThemeConfig().theme == "wanted"

        # resolve 回退（请求的名字没落地）时，配置保持原样
        monkeypatch.setattr(
            "albuswall.ui.presenter.main.resolve_theme",
            lambda name=None: theme.Theme(name="default", source="bundled"),
        )
        presenter.set_theme("typo-theme")
        assert theme.ThemeConfig().theme == "wanted"
    finally:
        container.get("presenter").stop()
        window.close()
