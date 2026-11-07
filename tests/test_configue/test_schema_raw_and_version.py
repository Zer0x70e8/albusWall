#
"""``dump`` 的两个增量字段：``raw`` / ``interpolated``，以及 ``schema_version``。

* ``raw`` 是 ``${...}`` **展开前**的原始值。设置页必须回显它——把展开后的
  绝对路径写回文件会破坏插值结构（``theme = ${path:theme}/default`` 变成
  一个死路径）。
* ``interpolated`` 是 ``raw`` 是否含插值标记的布尔事实（UI 自己决定怎么提示，
  dump 不做任何「raw vs value 比较」的逻辑）。
* ``schema_version`` 是 dump 结构的版本号：**只进返回值，不进配置文件**。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from albuswall.configue import (
    SCHEMA_DUMP_VERSION,
    ConfigFile,
    Configue,
    configclass,
    reg,
)
from albuswall.configue.resolver import _MISSING
from albuswall.configue.schema import (
    _MANUAL_SCHEMA,
    dump_schema,
    dump_schema_by_file,
    dump_schema_dict,
    invalidate_schema,
)


@pytest.fixture(autouse=True)
def _isolated_manual_schema():
    _MANUAL_SCHEMA.clear()
    invalidate_schema()
    try:
        yield
    finally:
        _MANUAL_SCHEMA.clear()
        invalidate_schema()


@pytest.fixture
def interp_cfg(tmp_path: Path):
    (tmp_path / "a.ini").write_text(
        "[path]\n"
        "theme = /usr/share\n"
        "[demo]\n"
        "theme = ${path:theme}/default\n"
        "name = plain\n"
        "count = 7\n",
        encoding="utf-8",
    )
    reg(ConfigFile.INI, "a.ini", name="a", order=10)

    @configclass("path", "a")
    class Paths:
        theme: str = "/usr/share"

    @configclass("demo", "a")
    class Demo:
        theme: str = "${path:theme}/default"
        name: str = "plain"
        count: int = 7
        missing: str = "fallback"

    cfg = Configue(base_dir=tmp_path)
    cfg.load()
    return cfg


def _items(cfg: Configue) -> dict[str, dict]:
    """按 ``group.name``（``"demo.theme"``）索引叶子——同名叶子会撞名字。"""
    return {
        item["key"]: item
        for item in dump_schema(view="flat", config=cfg)["items"]
    }


# ══════════════════════════════════════════════════════════════
# raw / interpolated
# ══════════════════════════════════════════════════════════════

def test_interpolated_item_exposes_raw_and_expanded_value(interp_cfg):
    theme = _items(interp_cfg)["demo.theme"]

    # value 仍是展开后的 typed 值（语义没变）
    assert theme["value"] == "/usr/share/default"
    assert theme["raw"] == "${path:theme}/default"
    assert theme["interpolated"] is True


def test_plain_value_raw_equals_value(interp_cfg):
    items = _items(interp_cfg)

    assert items["demo.name"]["value"] == "plain"
    assert items["demo.name"]["raw"] == "plain"
    assert items["demo.name"]["interpolated"] is False

    # int 字段：raw 是文件里的**文本**（展开 / 转换前），value 是 typed 值
    assert items["demo.count"]["value"] == 7
    assert items["demo.count"]["raw"] == "7"
    assert items["demo.count"]["interpolated"] is False


def test_missing_node_raw_falls_back_to_default(tmp_path: Path):
    """节点不存在（``Configue`` 未构造）时 ``raw`` 与 ``value`` 一样回落 default。"""
    @configclass("solo", "solo")
    class Solo:
        level: str = "info"

    item = dump_schema(view="flat")["items"][0]
    assert item["present"] is False
    assert item["value"] == "info"
    assert item["raw"] == "info"
    assert item["interpolated"] is False


def test_resolver_raw_of_is_unexpanded(interp_cfg):
    resolver = interp_cfg.resolver
    assert resolver.raw_of("demo", "theme") == "${path:theme}/default"
    assert resolver.get_str("demo", "theme") == "/usr/share/default"


def test_resolver_raw_of_missing_returns_sentinel(interp_cfg):
    assert interp_cfg.resolver.raw_of("demo", "ghost") is _MISSING


def test_dump_without_configue_uses_defaults(tmp_path: Path):
    @configclass("solo", "solo")
    class Solo:
        level: str = "info"

    dump = dump_schema(view="tree")
    item = dump["items"][0]
    assert item["raw"] == "info"
    assert item["interpolated"] is False
    assert item["present"] is False


# ══════════════════════════════════════════════════════════════
# schema_version
# ══════════════════════════════════════════════════════════════

def test_schema_dump_version_constant():
    # 2 — 叶子加 source / target_source，顶层加 source 计数与 uncovered
    assert SCHEMA_DUMP_VERSION == 2


def test_all_views_carry_schema_version(interp_cfg):
    for view in ("tree", "basic", "by_file", "flat"):
        dump = dump_schema(view=view, config=interp_cfg)
        assert "schema_version" in dump, view
        assert dump["schema_version"] == SCHEMA_DUMP_VERSION


def test_dict_and_by_file_carry_version(interp_cfg):
    assert dump_schema_dict(interp_cfg)["schema_version"] == SCHEMA_DUMP_VERSION
    by_file = dump_schema_by_file(interp_cfg)
    assert by_file["schema_version"] == SCHEMA_DUMP_VERSION
    # 版本号不被写进配置文件
    text = (interp_cfg.base_dir / "a.ini").read_text(encoding="utf-8")
    assert "schema_version" not in text


def test_flat_view_shape(interp_cfg):
    flat = dump_schema(view="flat", config=interp_cfg)
    assert set(flat) == {"schema_version", "items"}
    assert flat["items"]
