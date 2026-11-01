#
"""``configue.schema``：自动建树 / 控件推导 / 五种 dump 视图。

``tests/test_configue/conftest.py`` 的 ``isolated_configue`` 自动隔离全局
登记表；本文件只负责「声明 → 设置树」这一层，写回在
``test_setting_write.py``。
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Literal, Optional

import pytest

from albuswall.configue import ConfigFile, Configue, configclass, field, reg
from albuswall.configue.schema import (
    _MANUAL_SCHEMA,
    SettingSchemaRegistry,
    SettingWidget,
    build_schema,
    dump_schema,
    dump_schema_by_file,
    dump_schema_dict,
    format_schema_dump,
    get_schema,
    infer_widget,
    invalidate_schema,
    register_setting_group,
    register_setting_item,
    setting_group,
    setting_item,
)


class Theme(str, Enum):
    DARK = "dark"
    LIGHT = "light"


@pytest.fixture
def demo(tmp_path: Path):
    """一个已 reg + 已 load 的最小配置，声明了一批类型各异的字段。"""
    path = tmp_path / "a.ini"
    path.write_text("[demo]\nbatch_size = 32\n", encoding="utf-8")

    reg(ConfigFile.INI, "a.ini", name="a", order=10)

    @configclass("demo", "a")
    class Demo:
        batch_size: int = field(
            default=32, label="批量大小", description="一次处理多少",
        )
        enabled: bool = False
        ratio: float = 1.5
        cache_dir: Path = Path("/tmp/cache")
        name: str = "albus"
        retries: Optional[int] = None
        theme: Theme = Theme.DARK

    cfg = Configue(base_dir=tmp_path)
    cfg.load()
    return cfg


def _manual_cleanup():
    _MANUAL_SCHEMA.clear()


@pytest.fixture(autouse=True)
def _isolated_manual_schema():
    """手写覆盖表是进程级的，用例之间必须清干净。"""
    _manual_cleanup()
    invalidate_schema()
    try:
        yield
    finally:
        _manual_cleanup()
        invalidate_schema()


# ══════════════════════════════════════════════════════════════
# 自动枚举
# ══════════════════════════════════════════════════════════════

def test_auto_build_from_declarations(demo):
    dump = dump_schema(view="tree")

    assert [group["id"] for group in dump["groups"]] == ["demo"]
    group = dump["groups"][0]
    assert group["key"] == "demo"
    assert group["title"] == "demo"
    assert group["item_count"] == len(dump["items"])

    items = {item["name"]: item for item in group["items"]}
    batch = items["batch_size"]
    assert batch["key"] == "demo.batch_size"
    assert batch["path"] == "[demo]batch_size"
    assert batch["label"] == "批量大小"
    assert batch["description"] == "一次处理多少"
    assert batch["section"] == "demo"
    assert batch["config_key"] == "batch_size"
    assert batch["widget"] == "int"
    assert batch["default"] == 32
    assert batch["value"] == 32          # 文件里写了 32，覆盖默认值
    assert batch["present"] is True
    assert batch["dirty"] is False
    assert batch["type"] == "int"

    assert dump["summary"] == {
        "group_count": 1,
        "item_count": 7,
        "file_count": 1,
    }
    assert dump["by_file"] == {
        str(demo.base_dir / "a.ini"): [item["key"] for item in dump["items"]],
    }
    assert dump["unbound"] == []


def test_widget_inference_matrix():
    assert infer_widget(bool) is SettingWidget.BOOL
    assert infer_widget(int) is SettingWidget.INT
    assert infer_widget(float) is SettingWidget.FLOAT
    assert infer_widget(Path) is SettingWidget.PATH
    assert infer_widget(str) is SettingWidget.TEXT
    assert infer_widget(Literal["x", "y"]) is SettingWidget.CHOICE
    assert infer_widget(Optional[int]) is SettingWidget.INT
    assert infer_widget(Theme) is SettingWidget.CHOICE
    assert infer_widget(object) is SettingWidget.AUTO
    # 显式选项优先于类型
    assert infer_widget(int, ("a", "b")) is SettingWidget.CHOICE


def test_widget_inference_through_dump(demo):
    items = {item["name"]: item for item in dump_schema(view="flat")}

    assert items["enabled"]["widget"] == "bool"
    assert items["ratio"]["widget"] == "float"
    assert items["cache_dir"]["widget"] == "path"
    assert items["name"]["widget"] == "text"
    assert items["retries"]["widget"] == "int"
    assert items["retries"]["type"] == "int | None"
    assert items["theme"]["widget"] == "choice"
    assert items["theme"]["choices"] == ["dark", "light"]


# ══════════════════════════════════════════════════════════════
# 五种视图
# ══════════════════════════════════════════════════════════════

def test_view_tree_shape(demo):
    dump = dump_schema(view="tree")
    assert set(dump) == {"groups", "items", "by_file", "unbound", "summary"}

    item = dump["items"][0]
    assert set(item) == {
        "key", "name", "group", "path", "section", "config_key", "label",
        "description", "widget", "value", "value_repr", "default", "type",
        "choices", "choice_labels", "constraints", "order", "editable",
        "present", "dirty", "readonly", "origin_file", "target_file",
        "explicit_file", "read_error",
    }
    group = dump["groups"][0]
    assert set(group) == {
        "id", "key", "title", "description", "order", "editable", "advanced",
        "file", "items", "groups", "item_count",
    }


def test_view_flat_is_item_list(demo):
    flat = dump_schema(view="flat")
    assert isinstance(flat, list)
    assert len(flat) == dump_schema(view="tree")["summary"]["item_count"]
    assert all(isinstance(item, dict) for item in flat)


def test_view_by_file(demo):
    by_file = dump_schema_by_file()
    assert [entry["file"] for entry in by_file["files"]] == [
        str(demo.base_dir / "a.ini"),
    ]
    assert len(by_file["files"][0]["items"]) == 7
    assert by_file["unbound"] == []


def test_view_basic_drops_advanced(tmp_path: Path):
    path = tmp_path / "a.ini"
    path.write_text("", encoding="utf-8")
    reg(ConfigFile.INI, "a.ini", name="a", order=10)

    @configclass("demo", "a")
    class Demo:
        batch_size: int = 8

    @configclass("advanced", "a")
    class Advanced:
        secret_path: str = "/secret"

    Configue(base_dir=tmp_path).load()

    dump = dump_schema(view="tree")
    assert {group["id"] for group in dump["groups"]} == {"demo", "advanced"}

    basic = dump_schema(view="basic")
    assert [group["id"] for group in basic["groups"]] == ["demo"]
    assert [item["key"] for item in basic["items"]] == ["demo.batch_size"]
    assert basic["by_file"] == {
        str(path.resolve()): ["demo.batch_size"],
    }
    assert basic["summary"]["item_count"] == 1


def test_view_text(demo):
    text = dump_schema(view="text")
    assert isinstance(text, str)
    assert "Settings schema:" in text
    assert "Files:" in text
    assert "[demo]batch_size" in text
    assert "├─ " in text or "└─ " in text
    # 四行元信息都得在
    for marker in ("widget=int", "default=32", "file=", "origin="):
        assert marker in text

    assert format_schema_dump(config=demo) == text


def test_dump_dict_matches_tree(demo):
    assert dump_schema_dict() == dump_schema(view="tree")


# ══════════════════════════════════════════════════════════════
# 降级 / 只读
# ══════════════════════════════════════════════════════════════

def test_unloaded_declaration_is_unbound(tmp_path: Path):
    reg(ConfigFile.INI, "missing.ini", name="missing", order=10)

    @configclass("ghost", "missing")
    class Ghost:
        level: int = 3

    cfg = Configue(base_dir=tmp_path)
    # 不 load：文件不存在，声明也没有已解析路径
    dump = dump_schema(view="tree", config=cfg)

    assert dump["items"][0]["target_file"] is None
    assert dump["items"][0]["key"] in dump["unbound"]
    assert dump["by_file"] == {}
    assert dump["summary"]["file_count"] == 0


def test_dump_without_configue(tmp_path: Path):
    @configclass("demo", "a")
    class Demo:
        batch_size: int = 16

    dump = dump_schema(view="tree")   # Configue 未构造

    item = dump["items"][0]
    assert item["value"] == 16        # 退回声明默认值
    assert item["present"] is False
    assert item["target_file"] is None


def test_readonly_origin_marks_item(demo):
    path = demo.base_dir / "a.ini"
    demo.resolver.mark_readonly_file(path)

    items = {item["name"]: item for item in dump_schema(view="flat")}
    assert items["batch_size"]["readonly"] is True
    # 只读来源的文件仍可作为「来源」，但写回目标要另投别处（这里没有备选，
    # 于是退回声明文件本身——它同时也是只读的，由 flush 拒绝写入）
    assert items["batch_size"]["origin_file"] == str(path)


def test_read_error_is_degraded(tmp_path: Path):
    path = tmp_path / "a.ini"
    path.write_text("[demo]\nnum = not-a-number\n", encoding="utf-8")
    reg(ConfigFile.INI, "a.ini", name="a", order=10)

    @configclass("demo", "a")
    class Demo:
        num: int = 1

    # partial=True：坏值只让 load 的报告带错误，文件仍然算加载成功
    Configue(base_dir=tmp_path).load(partial=True)

    item = dump_schema(view="flat")[0]
    assert item["read_error"] is not None
    assert item["value"] == 1          # 退回默认值，dump 不抛


# ══════════════════════════════════════════════════════════════
# 手写覆盖
# ══════════════════════════════════════════════════════════════

def test_manual_item_overrides_auto_leaf(demo):
    before = dump_schema(view="tree")["summary"]["item_count"]

    register_setting_item("demo", setting_item("batch_size", label="批量"))

    dump = dump_schema(view="tree")
    items = {item["name"]: item for item in dump["items"]}
    assert items["batch_size"]["label"] == "批量"
    assert items["batch_size"]["default"] == 32      # 其余字段沿用自动值
    assert dump["summary"]["item_count"] == before   # 没有多出叶子


def test_manual_item_with_path_overrides_by_node(demo):
    register_setting_item(
        "demo",
        setting_item("并发", "demo", "batch_size", label="并发数", minimum=1),
    )

    items = {item["name"]: item for item in dump_schema(view="flat")}
    assert items["并发"]["label"] == "并发数"
    assert items["并发"]["constraints"] == {"minimum": 1}
    assert items["并发"]["path"] == "[demo]batch_size"


def test_manual_group_adds_new_leaf(demo):
    group = setting_group("demo", title="演示配置", order=5)
    group.add(
        setting_item("extra_knob", "demo", "extra_knob", label="额外开关"),
    )
    register_setting_group(group)

    dump = dump_schema(view="tree")
    root = dump["groups"][0]
    assert root["title"] == "演示配置"
    assert root["order"] == 5
    assert "demo.extra_knob" in {item["key"] for item in dump["items"]}


def test_manual_group_not_in_declarations_is_root(demo):
    register_setting_group(
        setting_group("plugins", title="插件").add(
            setting_item("hello", "hello", "enabled", label="启用"),
        ),
    )
    schema = build_schema(demo)
    assert "plugins" in schema
    assert schema.item_count == dump_schema(view="tree")["summary"]["item_count"]


def test_get_schema_cache_and_invalidate(demo):
    first = get_schema()
    assert get_schema() is first

    register_setting_item("demo", setting_item("batch_size", label="批量"))
    assert get_schema() is first        # 还没失效

    invalidate_schema()
    second = get_schema()
    assert second is not first
    names = {item.name for _group, item in second.iter_items()}
    assert "batch_size" in names


def test_registry_rejects_duplicate_group():
    registry = SettingSchemaRegistry()
    registry.register(setting_group("dup"))
    with pytest.raises(ValueError):
        registry.register(setting_group("dup"))
    # replace=True 是允许的
    registry.register(setting_group("dup"), replace=True)
    assert registry.group_count == 1
