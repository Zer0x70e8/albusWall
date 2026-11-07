#
"""被动视图 / binder 的契约测试（offscreen，不需要真配置）。"""

from __future__ import annotations

import threading
from typing import Any

import pytest
from PySide6.QtWidgets import QApplication

from albuswall.ui.setting import (
    LeafBinder,
    Scope,
    SettingView,
    SubmitMode,
    UnsupportedSchemaVersionError,
    normalize_dump,
    normalize_leaf,
)
from albuswall.ui.setting.demo_data import (
    LOG_FILE,
    MAIN_FILE,
    build_demo_dump,
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def view(qapp):
    widget = SettingView()
    widget.set_dump_schema(build_demo_dump())
    yield widget
    widget.deleteLater()
    qapp.processEvents()


# ============================================================================
# 契约 / 归一化
# ============================================================================

def test_unknown_schema_version_is_rejected(view):
    before = len(view.binders)
    with pytest.raises(UnsupportedSchemaVersionError):
        view.set_dump_schema({"schema_version": 99, "groups": []})
    assert len(view.binders) == before


def test_normalize_dump_derives_indexes_and_falls_back_widget():
    raw = {
        "schema_version": 2,
        "groups": [
            {
                "key": "demo",
                "title": "Demo",
                "items": [
                    {
                        "key": "demo.count",
                        "section": "demo",
                        "config_key": "count",
                        "target_file": "/tmp/config.ini",
                        "type": "int",
                        "default": 1,
                        "value": 2,
                    },
                ],
            },
        ],
    }
    dump = normalize_dump(raw)
    leaf = dump["items"][0]
    assert leaf["widget"] == "int"
    assert dump["by_file"] == {"/tmp/config.ini": ("demo.count",)}
    assert dump["unbound"] == ()
    assert dump["summary"]["item_count"] == 1


def test_collect_values_is_full_editable_set_not_visible_scope(view):
    total = len(view.collect_values())
    view.set_scope(Scope.for_file(LOG_FILE))
    assert len(view.collect_values()) == total
    assert len(view.rows) < len(view.binders)


# ============================================================================
# 范围 / 高级 / 锚点
# ============================================================================

def test_scope_rebuild_clears_anchors_and_invalid_scope_falls_back(view):
    view.set_scope(Scope.for_file(LOG_FILE))
    assert set(view.rows) == set(view.dump["by_file"][LOG_FILE])
    assert view.current_anchor is None
    assert "__unbound__" not in view.anchor_map

    view.set_scope(Scope.for_file("/does/not/exist.ini"))
    assert view.current_scope == Scope.all()


def test_advanced_toggle_only_changes_visibility(view):
    assert view.row_for("advanced.http_timeout") is not None
    assert view.row_for("ui.debug_overlay") is not None
    assert "ui.debug_overlay" in view.collect_values()

    view.set_show_advanced(False)
    assert view.row_for("advanced.http_timeout") is None
    assert view.row_for("ui.debug_overlay") is None
    assert "advanced" not in view.anchor_map
    # binder 仍在，提交范围不受布局影响
    assert "ui.debug_overlay" in view.collect_values()
    assert view.binders["advanced.http_timeout"].value == 5.0


def test_anchor_map_contains_groups_and_leaves(view):
    anchors = view.anchor_map
    for key in ("ui", "ui.theme", "thumbnail", "thumbnail.batch_size"):
        assert key in anchors
    assert "__unbound__" in anchors
    assert "__uncovered__" in anchors


def test_select_anchor_expands_parent_chain(view, qapp):
    group = view._group_boxes["thumbnail"]
    group.setChecked(False)
    assert view.select_anchor("thumbnail.batch_size") is True
    qapp.processEvents()
    assert group.isChecked()
    assert view.current_anchor == "thumbnail.batch_size"


# ============================================================================
# 通知（含跨线程）
# ============================================================================

def test_node_changed_updates_value_and_clears_dirty(view):
    binder = view.binders["thumbnail.batch_size"]
    assert binder.dirty  # demo 数据里这个节点是 dirty

    view.on_node_changed(("thumbnail", "batch_size"), 128, "set")
    assert binder.value == 128
    assert binder.dirty is False
    row = view.row_for("thumbnail.batch_size")
    assert row is not None
    assert row.control.value() == 128

    view.on_node_changed(("thumbnail", "batch_size"), 32, "remove")
    assert binder.present is False
    assert binder.value == 32


def test_threaded_notification_is_queued_to_main_thread(view, qapp):
    done = threading.Event()

    def worker() -> None:
        view.on_node_changed(("ui", "theme"), "light", "set")
        done.set()

    thread = threading.Thread(target=worker)
    thread.start()
    assert done.wait(2.0)
    thread.join()

    for _ in range(10):
        qapp.processEvents()
        if view.binders["ui.theme"].value == "light":
            break
    assert view.binders["ui.theme"].value == "light"


def test_unknown_source_is_rejected(view):
    with pytest.raises(ValueError):
        view.on_node_changed(("ui", "theme"), "x", "wat")


# ============================================================================
# 交互 / 提交
# ============================================================================

def test_reset_button_only_changes_ui_state(view):
    binder = view.binders["ui.theme"]
    row = view.row_for("ui.theme")
    emitted: list[Any] = []
    view.submit_requested.connect(lambda mode, values: emitted.append(mode))

    row.reset_button.click()

    assert binder.value == "default"
    assert binder.user_dirty is True
    assert emitted == []


def test_submit_result_routes_errors_and_footer(view):
    binder = view.binders["ui.accent_color"]
    row = view.row_for("ui.accent_color")

    view.show_submit_result({
        "validation_errors": [
            {"key": "ui.accent_color", "message": "bad color"},
        ],
        "pending": [],
        "ok": False,
        "written": [],
        "applied": [],
        "nodes": [],
        "deferred": False,
        "dry_run": False,
    })
    assert binder.error == "bad color"
    assert row.error_icon.text() == "⚠"
    assert "bad color" in row.error_icon.toolTip()
    assert "校验未通过" in view._footer_status.text()

    view.show_submit_result({
        "validation_errors": [],
        "pending": ["[ui]window_title"],
        "ok": False,
        "error": "Permission denied",
        "written": [],
        "applied": ["ui.window_title"],
        "nodes": [["ui", "window_title"]],
        "deferred": False,
        "dry_run": False,
    })
    assert binder.error is None
    assert not view._retry_button.isHidden()
    assert "1 项未落盘" in view._footer_status.text()

    view.show_submit_result({
        "validation_errors": [],
        "pending": [],
        "ok": True,
        "error": None,
        "written": [MAIN_FILE],
        "applied": ["ui.window_title"],
        "nodes": [["ui", "window_title"]],
        "deferred": False,
        "dry_run": False,
    })
    assert view._retry_button.isHidden()
    assert "已保存" in view._footer_status.text()


def test_dry_run_opens_preview_dialog(view, qapp):
    view.show_submit_result({
        "validation_errors": [],
        "pending": [],
        "ok": True,
        "dry_run": True,
        "written": [],
        "applied": [],
        "nodes": [],
        "deferred": False,
        "changes": [
            {
                "key": "ui.theme",
                "path": "[ui]theme",
                "old": "dark",
                "new": "light",
                "text": "light",
                "file": MAIN_FILE,
                "changed": True,
            },
        ],
    })
    assert view.preview_dialog is not None
    assert view.preview_dialog.isHidden() is False
    assert "预览" in view._footer_status.text()
    view.preview_dialog.close()
    qapp.processEvents()


# ============================================================================
# binder 纯逻辑
# ============================================================================

def _leaf() -> dict[str, Any]:
    return normalize_leaf({
        "key": "demo.x",
        "section": "demo",
        "config_key": "x",
        "target_file": "/tmp/config.ini",
        "type": "int",
        "default": 1,
        "value": 1,
        "present": True,
    })


def test_binder_dirty_error_and_persist():
    binder = LeafBinder(_leaf())
    assert binder.dirty is False

    assert binder.set_from_ui(2) is True
    assert binder.user_dirty is True
    assert binder.dirty is True

    binder.show_error("nope")
    assert binder.error == "nope"
    assert binder.set_from_ui(2) is False  # 同值不触发

    binder.set_external(1, "set")
    assert binder.value == 1
    assert binder.initial == 1
    assert binder.dirty is False
    assert binder.error is None
    assert binder.present is True

    binder.set_from_ui(9)
    binder.mark_persisted(pending=True)
    assert binder.dirty is True

    binder.mark_persisted()
    assert binder.dirty is False
    assert binder.initial == 9


def test_binder_external_rejects_reload():
    binder = LeafBinder(_leaf())
    with pytest.raises(TypeError):
        binder.set_external(1, "reload")


def test_binder_editability_reasons():
    read_only = normalize_leaf({
        "key": "demo.y",
        "section": "demo",
        "config_key": "y",
        "target_file": "/tmp/config.ini",
        "type": "str",
        "default": "a",
        "value": "a",
        "readonly": True,
    })
    binder = LeafBinder(read_only)
    assert binder.editable is False
    assert binder.reason == "readonly"

    unbound = normalize_leaf({
        "key": "demo.z",
        "section": "demo",
        "config_key": "z",
        "type": "str",
        "default": "a",
        "value": "a",
    })
    binder = LeafBinder(unbound)
    assert binder.editable is False
    assert binder.reason == "unbound"


def test_submit_mode_enum_values():
    assert SubmitMode.DRY_RUN.value == "dry_run"
    assert SubmitMode.COLLECT_ERRORS.value == "collect_errors"
    assert SubmitMode.DRAFT.value == "draft"


def test_group_editable_is_inherited_to_descendants():
    dump = {
        "schema_version": 2,
        "groups": [
            {
                "key": "locked",
                "title": "Locked",
                "editable": False,
                "items": [
                    {
                        "key": "locked.a",
                        "section": "locked",
                        "config_key": "a",
                        "target_file": "/tmp/config.ini",
                        "type": "int",
                        "default": 1,
                        "value": 1,
                        "editable": True,
                    },
                ],
                "groups": [
                    {
                        "key": "locked.child",
                        "title": "Child",
                        "items": [
                            {
                                "key": "locked.child.b",
                                "section": "locked",
                                "config_key": "b",
                                "target_file": "/tmp/config.ini",
                                "type": "str",
                                "default": "x",
                                "value": "x",
                            },
                        ],
                    },
                ],
            },
        ],
    }
    normalized = normalize_dump(dump)
    assert normalized["items"][0]["editable"] is False
    assert normalized["groups"][0]["groups"][0]["items"][0]["editable"] is False


def test_read_error_shows_warning_icon(view):
    row = view.row_for("network.endpoint")
    assert row.error_icon.text() == "⚠"
    assert "读取失败" in row.error_icon.toolTip()


def test_jump_frame_without_focus_then_second_jump_focuses(view, qapp):
    view.show()
    qapp.processEvents()
    row = view.row_for("ui.theme")
    assert row is not None

    assert view.select_anchor("ui.theme") is True
    qapp.processEvents()
    assert view.jump_frame_target == "ui.theme"
    assert view.jump_frame_visible is True
    assert view.jump_focus_given is False
    assert row.control.hasFocus() is False

    assert view.select_anchor("ui.theme") is True
    qapp.processEvents()
    assert view.jump_focus_given is True
    assert view.jump_frame_visible is False
    assert row.control.hasFocus() is True

    view.hide()


def test_jump_to_another_anchor_restarts_frame_only(view, qapp):
    view.show()
    qapp.processEvents()
    view.select_anchor("ui.theme")
    qapp.processEvents()
    assert view.jump_frame_visible is True

    view.select_anchor("ui.language")
    qapp.processEvents()
    assert view.jump_frame_target == "ui.language"
    assert view.jump_frame_visible is True
    assert view.jump_focus_given is False
    assert view.row_for("ui.language").control.hasFocus() is False
    view.hide()


def test_second_jump_on_non_focusable_row_keeps_frame(view, qapp):
    view.show()
    qapp.processEvents()
    view.select_anchor("network.endpoint")
    qapp.processEvents()
    view.select_anchor("network.endpoint")
    qapp.processEvents()
    assert view.jump_focus_given is False
    assert view.jump_frame_visible is True
    view.hide()


def test_second_jump_on_group_focuses_group_box(view, qapp):
    view.show()
    qapp.processEvents()
    view.select_anchor("thumbnail")
    qapp.processEvents()
    assert view.jump_frame_visible is True
    view.select_anchor("thumbnail")
    qapp.processEvents()
    assert view.jump_focus_given is True
    assert view.jump_frame_visible is False
    assert view._group_boxes["thumbnail"].hasFocus() is True
    view.hide()


def test_right_scroll_does_not_move_left_anchor_selection(view, qapp):
    """右侧滚动不改变左侧跳转按钮的选中态（不跟随 scrollspy）。"""
    view.show()
    qapp.processEvents()
    assert view.select_anchor("ui.theme") is True
    qapp.processEvents()
    selected = [key for key, button in view._sidebar_buttons.items()
                if button.isChecked()]
    assert selected == ["ui.theme"]
    anchor = view.current_anchor

    changes: list[object] = []
    view.anchor_changed.connect(changes.append)
    bar = view._right_scroll.verticalScrollBar()
    bar.setValue(bar.maximum())
    qapp.processEvents()

    assert view.current_anchor == anchor
    assert [
        key for key, button in view._sidebar_buttons.items()
        if button.isChecked()
    ] == ["ui.theme"]
    assert changes == []
    view.hide()
