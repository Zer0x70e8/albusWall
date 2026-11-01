#
"""同一个 ``Configue`` 下 INI + JSON 混用。

关注四件事：

1. 两种格式的文件可以在同一个实例里共存，各自用各自的后端读写；
2. ``order`` 跨格式生效（JSON 的 order 更大 → 覆盖 INI 的同名 key）；
3. 回写只动「归属文件」，不会把 JSON 的值写到 INI（反之亦然）；
4. 类型不同的值（``Path`` / ``Enum`` / ``list`` / ``None``）在 JSON 里能往返。
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path

import pytest

from albuswall.configue import (
    ConfigError,
    ConfigFile,
    Configue,
    configclass,
    field,
    reg,
)
from albuswall.configue.registry import _FILES

from .conftest import read_json, write_ini, write_json


class Theme(Enum):
    DARK = "dark"
    LIGHT = "light"


class Opaque:
    """JSON 序列化不认识的类型（测试「宁可失败也不写坏值」）。"""


@pytest.fixture
def mixed(tmp_path: Path):
    write_ini(tmp_path / "config.ini", "[ui]\nname = ini-name\nwidth = 800\n")
    write_json(
        tmp_path / "state.json",
        {
            "ui": {
                "name": "json-name",
                "theme": "dark",
                "recent": ["/a.png", "/b.png"],
                "last": None,
            },
        },
    )

    reg(ConfigFile.INI, "config.ini", name="main", order=10, autosave="off")
    reg(ConfigFile.JSON, "state.json", name="state", order=20, autosave="off")

    @configclass("ui", "main")
    class UIMain:
        name: str = "dflt"
        width: int = 0

    @configclass("ui", "state")
    class UIState:
        theme: Theme = Theme.LIGHT
        recent: list[Path] = []          # 文件里是 JSON 数组
        last: Path | None = None         # 文件里是 null

    c = Configue(tmp_path)
    c.load()
    return c, UIMain, UIState


# ==================== 读 ====================

def test_both_backends_loaded(mixed):
    c, UIMain, UIState = mixed

    assert set(c._path_to_file.values()) == {"main", "state"}
    assert UIMain().width == 800                       # INI（字符串 → int）
    assert UIState().theme is Theme.DARK               # JSON（str → Enum）
    assert UIState().recent == [Path("/a.png"), Path("/b.png")]
    assert UIState().last is None                      # JSON null


def test_order_works_across_backends(mixed):
    c, UIMain, _UIState = mixed

    # name 两边都有；state 的 order 更大 → 赢
    assert UIMain().name == "json-name"
    assert c.tree.state.ui.name == "json-name"


# ==================== 写 ====================

def test_flush_each_file_keeps_formats(mixed, tmp_path):
    c, UIMain, UIState = mixed

    UIMain().width = 1024
    UIState().theme = Theme.LIGHT

    written = c.flush()

    assert sorted(p.name for p in written) == ["config.ini", "state.json"]
    assert "width = 1024" in (tmp_path / "config.ini").read_text(encoding="utf-8")
    assert read_json(tmp_path / "state.json")["ui"]["theme"] == "light"


def test_flush_file_only_touches_that_backend(mixed, tmp_path):
    c, UIMain, UIState = mixed

    UIMain().width = 1
    UIState().theme = Theme.LIGHT

    written = c.tree.state.save()

    assert written == [(tmp_path / "state.json").resolve()]
    assert read_json(tmp_path / "state.json")["ui"]["theme"] == "light"
    assert "width = 800" in (tmp_path / "config.ini").read_text(encoding="utf-8")
    # main 还是 dirty，等下一次 flush
    assert ("ui", "width") in c.resolver.dirty_nodes()


def test_tree_view_write_to_json_keeps_native_types(mixed, tmp_path):
    c, _UIMain, _UIState = mixed

    c.tree.state.ui.recent = ["/c.png"]
    c.tree.state.save()

    data = read_json(tmp_path / "state.json")
    assert data["ui"]["recent"] == ["/c.png"]     # 原生数组，不是字符串


def test_json_none_roundtrip(mixed, tmp_path):
    c, _UIMain, UIState = mixed
    UIState().last = Path("/x.png")

    c.flush()

    assert read_json(tmp_path / "state.json")["ui"]["last"] == "/x.png"

    # 再设回 None → 写 null
    UIState().last = None
    c.flush()
    assert read_json(tmp_path / "state.json")["ui"]["last"] is None


# ==================== 卸载 / 回退 ====================

def test_unload_json_falls_back_to_ini(mixed):
    c, UIMain, _UIState = mixed
    assert UIMain().name == "json-name"

    c.unload("state")

    assert UIMain().name == "ini-name"
    assert c.resolver.origin_of("ui", "name") == (
        Path(c.base_dir) / "config.ini"
    ).resolve()
    assert _FILES["state"].loaded is False


def test_unload_json_then_reload(mixed, tmp_path):
    c, UIMain, UIState = mixed
    c.unload("state")
    write_json(tmp_path / "state.json", {"ui": {"theme": "light"}})

    report = c.load()

    assert report.loaded_files == ("state",)
    assert UIState().theme is Theme.LIGHT
    assert UIMain().name == "ini-name"


# ==================== 错误 ====================

def test_invalid_json_reported_as_read_failed(tmp_path):
    (tmp_path / "state.json").write_text("{oops", encoding="utf-8")
    reg(ConfigFile.JSON, "state.json", name="state")
    c = Configue(tmp_path)

    report = c.load(partial=True)

    assert report.ok is False
    assert report.errors[0].kind == "read_failed"
    assert "invalid JSON" in report.errors[0].message


def test_unserializable_value_keeps_node_dirty(tmp_path):
    write_json(tmp_path / "state.json", {"ui": {}})
    reg(ConfigFile.JSON, "state.json", name="state", autosave="off")

    @configclass("ui", "state")
    class State:
        blob: Opaque = field(default=None)

    c = Configue(tmp_path)
    c.load()
    c.resolver.set("ui", "blob", Opaque(), persist=True)

    assert c.flush() == []
    assert c.last_flush_error is not None
    assert ("ui", "blob") in c.resolver.dirty_nodes()

    with pytest.raises(ConfigError, match="cannot serialize"):
        c.flush(strict=True)


# ==================== 删除（remove → DELETE） ====================

def test_remove_deletes_key_from_both_formats(mixed, tmp_path):
    c, _UIMain, _UIState = mixed

    c.resolver.remove("ui", "width")     # INI
    c.resolver.remove("ui", "theme")     # JSON

    c.flush()

    assert "width" not in (tmp_path / "config.ini").read_text(encoding="utf-8")
    data = read_json(tmp_path / "state.json")
    assert "theme" not in data["ui"]
    assert data["ui"]["recent"] == ["/a.png", "/b.png"]   # 其它 key 不动


def test_remove_only_key_cleans_empty_json_section(tmp_path):
    write_json(tmp_path / "state.json", {"ui": {"theme": "dark"}, "keep": {"x": 1}})
    reg(ConfigFile.JSON, "state.json", name="state", autosave="off")

    @configclass("ui", "state")
    class State:
        theme: str = "light"

    c = Configue(tmp_path)
    c.load()
    c.resolver.remove("ui", "theme")
    c.flush()

    assert read_json(tmp_path / "state.json") == {"keep": {"x": 1}}


# ==================== 候选路径（无后缀写法） ====================

def test_json_file_path_without_suffix_resolves(tmp_path):
    write_json(tmp_path / "state.json", {"ui": {"theme": "dark"}})
    reg(ConfigFile.JSON, "state", name="state")

    @configclass("ui", "state")
    class State:
        theme: str = "light"

    c = Configue(tmp_path)
    report = c.load()

    assert report.ok is True
    assert _FILES["state"].resolved_path == (tmp_path / "state.json").resolve()
    assert State().theme == "dark"


def test_json_missing_file_reported(tmp_path):
    reg(ConfigFile.JSON, "state.json", name="state")
    c = Configue(tmp_path)

    report = c.check()
    assert report.errors[0].kind == "file_not_found"


def test_json_ambiguous_candidates_reported(tmp_path):
    write_json(tmp_path / "state.json", {})
    write_json(tmp_path / "state.json.json", {})
    reg(ConfigFile.JSON, "state.json", name="state")
    c = Configue(tmp_path)

    report = c.check()
    assert report.errors[0].kind == "ambiguous_file"
