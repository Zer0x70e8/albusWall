#
"""``handlers._present``：CLI 侧的展示策略纯函数。

策略层只吃 dict：本文件的用例**一律手搓 dump**，不建 ``Configue``——这正是
迁移的目的（展示策略不该依赖配置状态）。配置层的 dump 形状由
``tests/test_configue/test_setting_schema.py`` 负责。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from albuswall.service.command.handlers import _present
from albuswall.service.command.handlers._present import (
    PRESENT_OVERRIDES,
    apply_present,
    by_file_view,
    format_text,
    infer_widget,
    register_present_override,
    render_view,
)


@pytest.fixture(autouse=True)
def _isolated_overrides():
    """覆盖表是模块级的，用例之间必须清干净（数据不留状态）。"""
    PRESENT_OVERRIDES.clear()
    try:
        yield
    finally:
        PRESENT_OVERRIDES.clear()


# ══════════════════════════════════════════════════════════════
# 手搓 dump
# ══════════════════════════════════════════════════════════════

def dump_item(
        name: str,
        *,
        group: str = "demo",
        type_name: str = "int",
        choices=(),
        target_file: str | None = "/tmp/a.ini",
        **extra,
):
    """造一个叶子 dict（只带展示策略会读的键 + ``apply_present`` 会补的键）。"""
    item = {
        "key": f"{group}.{name}",
        "name": name,
        "group": group,
        "path": f"[{group.split('.')[0]}]{name}",
        "section": group.split(".")[0],
        "config_key": name,
        "label": name,
        "description": "",
        "value": 1,
        "value_repr": "1",
        "default": 1,
        "type": type_name,
        "choices": list(choices),
        "order": 0,
        "editable": True,
        "present": True,
        "dirty": False,
        "readonly": False,
        "origin_file": target_file,
        "target_file": target_file,
        "explicit_file": None,
        "read_error": None,
    }
    item.update(extra)
    return item


def make_group(group_id: str, items=(), groups=()):
    items = list(items)
    groups = list(groups)
    return {
        "id": group_id,
        "key": group_id,
        "title": group_id,
        "description": "",
        "order": 0,
        "editable": True,
        "file": "/tmp/a.ini",
        "items": items,
        "groups": groups,
        "item_count": len(items) + sum(child["item_count"] for child in groups),
    }


def make_dump(groups):
    groups = list(groups)
    items: list[dict] = []

    def walk(node):
        items.extend(node["items"])
        for child in node["groups"]:
            walk(child)

    for group in groups:
        walk(group)
    by_file: dict[str, list[str]] = {}
    unbound: list[str] = []
    for item in items:
        target = item["target_file"]
        if target is None:
            unbound.append(item["key"])
        else:
            by_file.setdefault(target, []).append(item["key"])
    return {
        "groups": groups,
        "items": items,
        "by_file": by_file,
        "unbound": unbound,
        "summary": {
            "group_count": len(groups),
            "item_count": len(items),
            "file_count": len(by_file),
        },
    }


@pytest.fixture
def dump():
    """两个分组：``demo``（两个叶子）+ ``advanced.secret``（一个叶子）。"""
    return make_dump([
        make_group("demo", [
            dump_item("batch_size", type_name="int"),
            dump_item("theme", type_name="Theme", choices=("dark", "light")),
        ]),
        make_group("advanced.secret", [
            dump_item("level", group="advanced.secret", type_name="int"),
        ]),
    ])


# ══════════════════════════════════════════════════════════════
# infer_widget：类型字符串 → 值类别
# ══════════════════════════════════════════════════════════════

@pytest.mark.parametrize(
    ("type_name", "expected"),
    [
        ("bool", "bool"),
        ("int", "int"),
        ("float", "float"),
        ("str", "text"),
        ("Path", "path"),
        ("int | None", "int"),
        ("bool | None", "bool"),
        ("Path | None", "path"),
        ("list[int]", "list"),
        ("list[str] | None", "list"),
        ("tuple[int, ...]", "list"),
        ("dict[str, int]", "dict"),
        ("Any", "auto"),
        ("?", "auto"),
        ("None", "auto"),
        ("Theme", "auto"),            # Enum 名认不出来，靠 choices
        ("int | str", "auto"),        # 多分支联合不猜
        ("object", "auto"),
        ("", "auto"),
    ],
)
def test_infer_widget_matrix(type_name, expected):
    assert infer_widget(type_name, ()) == expected


def test_infer_widget_choices_win():
    # Literal / Enum：选项是事实，不必再看内层类型
    assert infer_widget("int", ("a", "b")) == "choice"
    assert infer_widget("Theme", ["dark", "light"]) == "choice"
    assert infer_widget("auto", ()) == "auto"


def test_infer_widget_is_pure():
    """覆盖表不参与推导（否则就不是纯函数了）。"""
    register_present_override("demo", "batch_size", widget="choice")
    assert infer_widget("int", ()) == "int"


# ══════════════════════════════════════════════════════════════
# apply_present：补 widget / 合并覆盖 / 过滤高级
# ══════════════════════════════════════════════════════════════

def test_apply_present_adds_widget(dump):
    result = apply_present(dump)

    assert result is dump                     # 就地 mutate 且返回同一个对象
    items = {item["key"]: item for item in result["items"]}
    assert items["demo.batch_size"]["widget"] == "int"
    assert items["demo.theme"]["widget"] == "choice"
    assert items["demo.batch_size"]["constraints"] is None
    assert items["demo.batch_size"]["choice_labels"] == {}
    # 分组 / 叶子的高级标记按``advanced`` 约定 id 判
    assert result["groups"][1]["advanced"] is True
    assert result["groups"][0]["advanced"] is False
    assert items["advanced.secret.level"]["advanced"] is True


def test_apply_present_merges_override(dump):
    register_present_override(
        "demo", "batch_size",
        minimum=1, maximum=64, step=2,
        label="批量", description="一次处理多少",
    )
    items = {item["key"]: item for item in apply_present(dump)["items"]}

    batch = items["demo.batch_size"]
    assert batch["constraints"] == {"minimum": 1, "maximum": 64, "step": 2}
    assert batch["label"] == "批量"
    assert batch["description"] == "一次处理多少"
    assert batch["widget"] == "int"           # 没给 widget 就用推导值


def test_apply_present_override_widget_and_choice_labels(dump):
    register_present_override(
        "demo", "theme",
        widget="choice",
        choice_labels={"dark": "深色", "light": "浅色"},
    )
    items = {item["key"]: item for item in apply_present(dump)["items"]}
    assert items["demo.theme"]["widget"] == "choice"
    assert items["demo.theme"]["choice_labels"] == {"dark": "深色", "light": "浅色"}


def test_apply_present_override_advanced_marks_item(dump):
    register_present_override("demo", "batch_size", advanced=True)
    result = apply_present(dump)
    items = {item["key"]: item for item in result["items"]}
    assert items["demo.batch_size"]["advanced"] is True

    basic = apply_present(make_dump([
        make_group("demo", [dump_item("batch_size")]),
    ]), show_advanced=False)
    assert basic["items"] == []


def test_apply_present_multiple_registrations_accumulate(dump):
    register_present_override("demo", "batch_size", minimum=1)
    register_present_override("demo", "batch_size", maximum=8)
    items = {item["key"]: item for item in apply_present(dump)["items"]}
    assert items["demo.batch_size"]["constraints"] == {"minimum": 1, "maximum": 8}


def test_apply_present_override_is_copied(dump):
    labels = {"dark": "深色"}
    register_present_override("demo", "theme", choice_labels=labels)
    labels["dark"] = "改标题不生效"
    items = {item["key"]: item for item in apply_present(dump)["items"]}
    assert items["demo.theme"]["choice_labels"] == {"dark": "深色"}


def test_show_advanced_false_drops_advanced_subtree(dump):
    basic = apply_present(dump, show_advanced=False)

    assert [group["id"] for group in basic["groups"]] == ["demo"]
    assert [item["key"] for item in basic["items"]] == [
        "demo.batch_size", "demo.theme",
    ]
    assert basic["unbound"] == []
    assert basic["summary"] == {
        "group_count": 1,
        "item_count": 2,
        "file_count": 1,
    }
    assert basic["groups"][0]["item_count"] == 2


def test_show_advanced_false_recomputes_by_file():
    dump = make_dump([
        make_group("demo", [dump_item("a", target_file="/tmp/a.ini")]),
        make_group("advanced", [
            dump_item("b", group="advanced", target_file="/tmp/b.ini"),
        ]),
    ])
    basic = apply_present(dump, show_advanced=False)
    assert basic["by_file"] == {"/tmp/a.ini": ["demo.a"]}
    assert basic["summary"]["file_count"] == 1


def test_show_advanced_true_keeps_unbound():
    dump = make_dump([make_group("demo", [dump_item("a", target_file=None)])])
    presented = apply_present(dump)
    assert presented["unbound"] == ["demo.a"]
    assert presented["by_file"] == {}


def test_apply_present_on_items_only_dump():
    """只含 ``items`` 的 dump 也能用（视图装配的分支不依赖 groups）。"""
    dump = {"items": [dump_item("a", type_name="bool")]}
    assert apply_present(dump)["items"][0]["widget"] == "bool"


def test_apply_present_keeps_fact_fields(dump):
    """事实字段一概不动（迁移的硬约束）。"""
    facts = (
        "key", "name", "group", "path", "section", "config_key", "label",
        "value", "value_repr", "default", "type", "choices", "order",
        "editable", "present", "dirty", "readonly", "origin_file",
        "target_file", "explicit_file", "read_error",
    )
    before = json.dumps(
        [{name: item[name] for name in facts} for item in dump["items"]],
        sort_keys=True,
    )
    apply_present(dump)
    after = json.dumps(
        [{name: item[name] for name in facts} for item in dump["items"]],
        sort_keys=True,
    )
    assert before == after


# ══════════════════════════════════════════════════════════════
# 视图装配 / 文本排版
# ══════════════════════════════════════════════════════════════

def test_render_view_shapes(dump):
    presented = apply_present(dump)

    assert render_view(presented, "tree") is presented
    assert render_view(presented, "basic") is presented
    assert isinstance(render_view(presented, "flat"), list)
    assert len(render_view(presented, "flat")) == len(presented["items"])

    by_file = render_view(presented, "by_file")
    assert set(by_file) == {"files", "unbound"}
    assert by_file["files"][0]["file"] == "/tmp/a.ini"
    assert [item["key"] for item in by_file["files"][0]["items"]] == [
        "demo.batch_size", "demo.theme", "advanced.secret.level",
    ]

    text = render_view(presented, "text")
    assert isinstance(text, str)
    assert "Settings schema: 2 groups, 3 items, 1 files" in text


def test_by_file_view_keeps_unbound():
    dump = make_dump([
        make_group("demo", [dump_item("a", target_file=None)]),
    ])
    view = by_file_view(dump)
    assert view["files"] == []
    assert [item["key"] for item in view["unbound"]] == ["demo.a"]


def test_format_text_uses_widget_from_present(dump):
    register_present_override("demo", "batch_size", minimum=1)
    text = format_text(apply_present(dump))

    assert "[demo]batch_size" in text
    for marker in ("widget=int", "widget=choice", "default=1", "origin="):
        assert marker in text
    assert "Files:" in text
    assert "\n  /tmp/a.ini\n    demo.batch_size\n" in text


def test_format_text_empty_dump():
    text = format_text(make_dump([]))
    assert "(no setting groups registered)" in text
    assert "  (none)" in text


def test_format_text_can_hide_files(dump):
    text = format_text(apply_present(dump), show_files=False)
    assert "Files:" not in text


# ══════════════════════════════════════════════════════════════
# 依赖边界：策略层不碰配置层
# ══════════════════════════════════════════════════════════════

def test_present_module_does_not_import_configue():
    source = Path(_present.__file__).read_text(encoding="utf-8")
    imported = {
        node.module or ""
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom)
    } | {
        alias.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert not [name for name in imported if "configue" in name]
    assert not [name for name in imported if "PySide" in name]
