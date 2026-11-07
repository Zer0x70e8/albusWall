#
"""presenter ↔ 配置器：界面状态的读写闭环（offscreen Qt）。

覆盖三件事：

* **恢复**：``[window]`` 段里的几何 / 最大化 / 浮层页 / 网格列数，在
  ``MainPresenter.bootstrap`` 之后真的落到了窗口与视图上；
* **持久化**：``MainPresenter.persist_ui_state`` 汇总各子 presenter 的
  ``ui_state()``，**一次**提交进配置文件（窗口几何 / 浮层页 / 网格列数）；
* **更新入口**：``MainPresenter.update_settings`` 校验后写盘，``dry_run``
  不落盘也不改内存。

``configue`` 的登记表是进程全局的，这里照 ``tests/test_ui_presenter.py`` 的
做法清一遍再把应用侧的 ``@configclass`` 重新登记（清表会把 import 期完成的
声明一起清掉）。
"""

from __future__ import annotations

import os
from contextlib import suppress
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")


# ==================== fixture ====================

@pytest.fixture
def isolated_configue():
    """每个用例一张全新的 configue 登记表 + 单例，并重放应用侧声明。"""
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
        from albuswall.ui.window.settings import WindowState

        configclass("ui", "main")(ThemeConfig)
        configclass("window", "main")(WindowState)

    def reset() -> None:
        instance = Configue._instance
        if instance is not None:
            with suppress(Exception):
                instance.close()
        for table in (_FILES, _CLASSES, _PATHS, _FIELD_OWNER,
                      _LOAD_HOOKS, _PENDING_CONVERTERS):
            table.clear()
        Configue._instance = None
        # 设置页的「认领」是进程级手写登记；测试之间必须清掉，否则会
        # 泄漏到后面的用例（并污染整个 pytest 进程）。
        from albuswall.configue.schema import (
            _MANUAL_SCHEMA,
            invalidate_schema,
        )
        _MANUAL_SCHEMA.clear()
        invalidate_schema()
        with suppress(Exception):
            from albuswall.service.command.handlers._present import (
                PRESENT_OVERRIDES,
            )
            PRESENT_OVERRIDES.clear()
        redeclare()

    reset()
    try:
        yield
    finally:
        reset()


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(qapp):
    from albuswall.ui.window import Window

    win = Window()
    win.setWindowTitle("test")
    yield win
    win.close()


def _start(base_dir: Path, qapp, window):
    """在 ``base_dir`` 上装配配置并起 presenter 树，返回 ``(config, presenter)``。

    ``base_dir`` 里的 ``config.ini`` 由调用方先写好（或干脆不存在 ——
    ``ignore_missing=True`` 允许首次启动没有配置文件）。
    """
    from albuswall.configue.bootstrap import setup_config
    from albuswall.core import Container
    from albuswall.ui.presenter import MainPresenter

    container = Container()
    config = setup_config(
        container, base_dir=base_dir, portable=True, ignore_missing=True,
    )
    container.set("main_window", window)
    presenter = MainPresenter.bootstrap(container, app=qapp, window=window)
    return config, presenter


# ==================== 恢复 ====================

def test_presenter_restores_window_state_on_start(
        isolated_configue, qapp, window, tmp_path: Path,
) -> None:
    (tmp_path / "config.ini").write_text(
        "[window]\n"
        "geometry = 11,22,700,500\n"
        "maximized = true\n"
        "tile_columns = 4\n"
        "overlay = source\n",
        encoding="utf-8",
    )
    _, presenter = _start(tmp_path, qapp, window)

    assert window.geometry().getRect() == (11, 22, 700, 500)
    assert window.isMaximized() or (
        window.windowState() & window.windowState().WindowMaximized
    )
    assert window.overlay.currentWidget() is window.source
    assert window.content.column_count == 4
    presenter.stop()


def test_presenter_without_saved_state_keeps_its_defaults(
        isolated_configue, qapp, window, tmp_path: Path,
) -> None:
    (tmp_path / "config.ini").write_text("[ui]\ntheme = default\n", encoding="utf-8")
    _, presenter = _start(tmp_path, qapp, window)

    window_window = presenter.child("window")
    assert isinstance(window_window.ui_state()["window.geometry"], str)
    assert window.overlay.is_blank()
    presenter.stop()


# ==================== 持久化 ====================

def test_persist_ui_state_writes_every_presenter_state(
        isolated_configue, qapp, window, tmp_path: Path,
) -> None:
    (tmp_path / "config.ini").write_text("[ui]\ntheme = default\n", encoding="utf-8")
    _, presenter = _start(tmp_path, qapp, window)

    window.setGeometry(30, 40, 800, 600)
    window.show_album()
    # 非默认列数才会被记住（等于默认值的列数写 0 = 不指定）
    window.content.set_column_count(7)

    report = presenter.persist_ui_state()
    assert report["ok"] is True
    assert "window.geometry" in report["applied"]
    assert "window.tile_columns" in report["applied"]

    text = (tmp_path / "config.ini").read_text(encoding="utf-8")
    assert "[window]" in text
    assert "30,40,800,600" in text
    assert "overlay = album" in text

    from albuswall.ui.window.settings import window_state

    snapshot = window_state()
    assert snapshot.rect() == (30, 40, 800, 600)
    assert snapshot.overlay == "album"
    assert snapshot.tile_columns == 7
    presenter.stop()


def test_persist_ui_state_is_idempotent(
        isolated_configue, qapp, window, tmp_path: Path,
) -> None:
    """第二次提交没有新值可写：``applied`` 为空，不再落盘。"""
    (tmp_path / "config.ini").write_text("[ui]\ntheme = default\n", encoding="utf-8")
    _, presenter = _start(tmp_path, qapp, window)

    window.setGeometry(10, 20, 640, 480)
    assert presenter.persist_ui_state()["applied"]

    second = presenter.persist_ui_state()
    assert second["applied"] == []
    assert second["written"] == []
    presenter.stop()


def test_default_column_count_is_not_persisted(
        isolated_configue, qapp, window, tmp_path: Path,
) -> None:
    """没缩放过网格：``tile_columns`` 保持「不指定」，不把默认值冻进配置。"""
    (tmp_path / "config.ini").write_text("[ui]\ntheme = default\n", encoding="utf-8")
    _, presenter = _start(tmp_path, qapp, window)

    content = presenter.child("content")
    assert content.ui_state() == {"window.tile_columns": 0}
    assert "window.tile_columns" not in presenter.persist_ui_state()["applied"]
    presenter.stop()


# ==================== 更新入口 ====================

def test_update_settings_writes_and_reports(
        isolated_configue, qapp, window, tmp_path: Path,
) -> None:
    (tmp_path / "config.ini").write_text("[ui]\ntheme = default\n", encoding="utf-8")
    _, presenter = _start(tmp_path, qapp, window)

    report = presenter.update_settings({"window.maximized": True})

    assert report["ok"] is True
    assert report["applied"] == ["window.maximized"]
    assert "maximized" in (tmp_path / "config.ini").read_text(encoding="utf-8")
    presenter.stop()


def test_update_settings_applies_theme_change(
        isolated_configue, qapp, window, tmp_path: Path, caplog,
) -> None:
    """``ui.theme`` 写入后立刻重新解析并套用（未知主题回退到 default）。"""
    (tmp_path / "config.ini").write_text("[ui]\ntheme = default\n", encoding="utf-8")
    _, presenter = _start(tmp_path, qapp, window)

    with caplog.at_level("WARNING", logger="albuswall.ui.presenter"):
        report = presenter.update_settings({"ui.theme": "no-such-theme"})

    assert "ui.theme" in report["applied"]
    assert presenter.theme is not None and presenter.theme.name == "default"
    assert "no-such-theme" in caplog.text
    presenter.stop()


def test_update_settings_dry_run_leaves_config_untouched(
        isolated_configue, qapp, window, tmp_path: Path,
) -> None:
    (tmp_path / "config.ini").write_text("[ui]\ntheme = default\n", encoding="utf-8")
    _, presenter = _start(tmp_path, qapp, window)

    report = presenter.update_settings({"window.tile_columns": 3}, dry_run=True)

    assert report["dry_run"] is True
    assert report["applied"] == []
    from albuswall.ui.window.settings import window_state

    assert window_state().tile_columns == 0
    presenter.stop()


# ==================== 设置页接入 ====================

def test_settings_page_opens_with_a_real_dump(
        isolated_configue, qapp, window, tmp_path: Path,
) -> None:
    (tmp_path / "config.ini").write_text(
        "[ui]\ntheme = default\n", encoding="utf-8",
    )
    _, presenter = _start(tmp_path, qapp, window)
    settings = presenter.child("settings")

    window.menu.setting_action.trigger()

    assert window.overlay.currentWidget() is window.settings_page
    view = window.settings_page.view
    assert settings.view is view
    assert view.dump is not None
    assert view.binders
    assert "ui.theme" in view.binders
    presenter.stop()


def test_settings_save_goes_through_main_update_settings(
        isolated_configue, qapp, window, tmp_path: Path,
) -> None:
    (tmp_path / "config.ini").write_text(
        "[ui]\ntheme = default\n", encoding="utf-8",
    )
    config, presenter = _start(tmp_path, qapp, window)
    window.menu.setting_action.trigger()
    view = window.settings_page.view

    binder = view.binders["window.maximized"]
    binder.set_from_ui(True)
    view.submit_requested.emit("collect_errors", view.collect_values())

    assert config.resolver.get_typed("window", "maximized") is True
    assert "maximized = true" in (tmp_path / "config.ini").read_text(
        encoding="utf-8",
    )
    presenter.stop()


def test_settings_draft_then_retry_flushes_to_disk(
        isolated_configue, qapp, window, tmp_path: Path,
) -> None:
    (tmp_path / "config.ini").write_text(
        "[ui]\ntheme = default\n", encoding="utf-8",
    )
    config, presenter = _start(tmp_path, qapp, window)
    window.menu.setting_action.trigger()
    view = window.settings_page.view

    view.binders["window.tile_columns"].set_from_ui(5)
    view.submit_requested.emit("draft", view.collect_values())

    assert config.resolver.get_typed("window", "tile_columns") == 5
    assert "tile_columns" not in (tmp_path / "config.ini").read_text(
        encoding="utf-8",
    )

    view.retry_requested.emit()
    assert "tile_columns = 5" in (tmp_path / "config.ini").read_text(
        encoding="utf-8",
    )
    presenter.stop()


def test_settings_dry_run_does_not_write(
        isolated_configue, qapp, window, tmp_path: Path,
) -> None:
    (tmp_path / "config.ini").write_text(
        "[ui]\ntheme = default\n", encoding="utf-8",
    )
    config, presenter = _start(tmp_path, qapp, window)
    window.menu.setting_action.trigger()
    view = window.settings_page.view

    view.binders["window.tile_columns"].set_from_ui(9)
    view.submit_requested.emit("dry_run", view.collect_values())

    assert config.resolver.get_typed("window", "tile_columns") == 0
    assert "tile_columns" not in (tmp_path / "config.ini").read_text(
        encoding="utf-8",
    )
    if view.preview_dialog is not None:
        view.preview_dialog.close()
        qapp.processEvents()
    presenter.stop()


# ==================== 布局再处理 ====================

def test_move_item_to_new_group_keeps_writeback_key(
        isolated_configue, qapp, window, tmp_path: Path,
) -> None:
    (tmp_path / "config.ini").write_text(
        "[ui]\ntheme = default\n", encoding="utf-8",
    )
    config, presenter = _start(tmp_path, qapp, window)
    window.menu.setting_action.trigger()
    settings = presenter.child("settings")
    view = window.settings_page.view

    assert settings.move_item(
        "ui.theme", "appearance",
        group_title="外观", group_order=1,
    )
    appearance = next(
        group for group in view.dump["groups"] if group["key"] == "appearance"
    )
    assert [item["key"] for item in appearance["items"]] == ["ui.theme"]
    assert "ui.theme" in view.binders

    view.binders["ui.theme"].set_from_ui("dark")
    view.submit_requested.emit("collect_errors", view.collect_values())

    assert config.resolver.get_typed("ui", "theme") == "dark"
    assert "theme = dark" in (tmp_path / "config.ini").read_text(encoding="utf-8")

    # 布局是容器注入的运行时对象，不是文件
    assert settings.layout.move_for("ui.theme") is not None
    assert not (tmp_path / "setting_layout.json").exists()
    presenter.stop()


def test_claim_uncovered_node_makes_it_editable(
        isolated_configue, qapp, window, tmp_path: Path,
) -> None:
    (tmp_path / "config.ini").write_text(
        "[ui]\ntheme = default\ncustom_note = hello\n", encoding="utf-8",
    )
    config, presenter = _start(tmp_path, qapp, window)
    window.menu.setting_action.trigger()
    settings = presenter.child("settings")
    view = window.settings_page.view

    entry = next(
        item for item in view.dump["uncovered"]
        if item["path"] == "[ui]custom_note"
    )
    assert entry["typed"] is False

    assert settings.claim_node(
        entry,
        group="ui", name="custom_note", label="自定义备注", widget="text",
    )
    assert "ui.custom_note" in view.binders
    assert all(
        item["path"] != "[ui]custom_note" for item in view.dump["uncovered"]
    )

    view.binders["ui.custom_note"].set_from_ui("changed")
    view.submit_requested.emit("collect_errors", view.collect_values())
    assert config.resolver.get_typed("ui", "custom_note") == "changed"
    assert "custom_note = changed" in (tmp_path / "config.ini").read_text(
        encoding="utf-8",
    )

    assert settings.layout.has_claim("ui", "custom_note")
    assert not (tmp_path / "setting_layout.json").exists()
    presenter.stop()


def test_add_component_group_renders_label(
        isolated_configue, qapp, window, tmp_path: Path,
) -> None:
    (tmp_path / "config.ini").write_text(
        "[ui]\ntheme = default\n", encoding="utf-8",
    )
    _, presenter = _start(tmp_path, qapp, window)
    window.menu.setting_action.trigger()
    settings = presenter.child("settings")
    view = window.settings_page.view

    # 内置 About（组件分组）已经存在
    about = next(group for group in view.dump["groups"] if group["key"] == "about")
    assert about["components"]
    text = about["components"][0]["text"]
    assert "布局文件" not in text  # about 不再展示布局文件路径

    assert settings.add_component_group(
        "custom_about", title="自定义关于", order=1000, label="hello component",
    )
    group = next(
        item for item in view.dump["groups"] if item["key"] == "custom_about"
    )
    assert group["components"][0]["text"] == "hello component"
    assert "custom_about" in view.anchor_map
    presenter.stop()


def test_shortcuts_group_is_editable_and_not_uncovered(
        isolated_configue, qapp, window, tmp_path: Path,
) -> None:
    (tmp_path / "config.ini").write_text(
        "[ui]\ntheme = default\n", encoding="utf-8",
    )
    config, presenter = _start(tmp_path, qapp, window)
    window.menu.setting_action.trigger()
    view = window.settings_page.view
    group = next(
        item for item in view.dump["groups"] if item["key"] == "shortcuts"
    )
    assert group["title"] == "快捷键"
    assert [item["key"] for item in group["items"]]
    assert "shortcuts.app.quit" in view.binders

    row = view.row_for("shortcuts.app.quit")
    assert row is not None
    assert row.control_kind == "text"
    assert row.control.isEnabled()
    assert "Ctrl+Q" in view.binders["shortcuts.app.quit"].value

    # 改键位 → 正常走保存链路 → 写回用户 shortcuts.ini
    view.binders["shortcuts.app.quit"].set_from_ui('["Ctrl+Shift+Q"]')
    view.submit_requested.emit("collect_errors", view.collect_values())
    record = config.resolver.get_typed("shortcuts", "app.quit")
    assert tuple(record.keys) == ("Ctrl+Shift+Q",)
    assert "Ctrl+Shift+Q" in (tmp_path / "shortcuts.ini").read_text(
        encoding="utf-8",
    )

    # [shortcuts] 不再出现在「未覆盖」清单里
    assert all(
        item["section"] != "shortcuts" for item in view.dump["uncovered"]
    )
    assert not hasattr(view, "claim_requested")
    presenter.stop()


# ==================== 内置「主题」分组 ====================

def test_theme_items_land_in_builtin_theme_group(
        isolated_configue, qapp, window, tmp_path: Path,
) -> None:
    (tmp_path / "config.ini").write_text(
        "[ui]\ntheme = default\n", encoding="utf-8",
    )
    _, presenter = _start(tmp_path, qapp, window)
    window.menu.setting_action.trigger()
    settings = presenter.child("settings")
    view = window.settings_page.view

    theme = view.tree.group("theme")
    assert theme is not None and theme["title"] == "主题"
    assert {leaf.key for leaf in theme.leaves} == {"ui.theme", "path.theme"}
    # 行是真实可编辑的（不是只读展示）
    assert view.row_for("ui.theme") is not None
    assert view.row_for("path.theme") is not None

    # 用户可以把内置摆放挪走；「恢复默认位置」再落回「主题」分组
    assert settings.move_item("ui.theme", "ui")
    assert settings._leaf_group("ui.theme") == "ui"
    assert settings.reset_move("ui.theme")
    assert settings._leaf_group("ui.theme") == "theme"
    presenter.stop()


def test_row_context_menu_reaches_row_children(
        isolated_configue, qapp, window, tmp_path: Path,
) -> None:
    """整行都能右键出菜单（文本 / 徽标不是死区）。"""
    from PySide6.QtCore import Qt

    (tmp_path / "config.ini").write_text(
        "[ui]\ntheme = default\n", encoding="utf-8",
    )
    _, presenter = _start(tmp_path, qapp, window)
    window.menu.setting_action.trigger()
    view = window.settings_page.view
    row = view.row_for("ui.theme")
    assert row is not None

    label_policy = row._label.contextMenuPolicy()
    assert label_policy == Qt.ContextMenuPolicy.CustomContextMenu
    # 输入控件保留自己的原生菜单
    assert (
        row.control.contextMenuPolicy() != Qt.ContextMenuPolicy.CustomContextMenu
    )
    presenter.stop()
