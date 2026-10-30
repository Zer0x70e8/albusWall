#
"""树视图的 dict-like 协议：``keys`` / ``values`` / ``items`` / ``in`` / ``len`` / 迭代。

三层各自的语义差异（本文件的核心断言）：

- ``tree``：所有已 ``reg`` 的文件名（含未加载），``order`` 降序；
- ``file``：该文件**当前提供生效值**的 section；未 loaded → 空（枚举宽容）；
- ``section``：该 section 的所有 key（声明过的 + 文件里写过的）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from albuswall.configue1 import (
    ConfigFile,
    Configue,
    configclass,
    field,
    reg,
)

from .conftest import write_ini, write_json

INI = "[core]\ndebug = true\nname = hello\n"


@pytest.fixture
def loaded(tmp_path: Path):
    write_ini(tmp_path / "config.ini", INI)
    reg(ConfigFile.INI, "config.ini", name="main", order=10, autosave="off")

    @configclass("core", "main")
    class Core:
        debug: bool = field(default=False)
        name: str = field(default="x")
        # 声明了但文件里没写、也没有默认值 → 只出现在 keys() 里
        port: int = field(default=8000)

    c = Configue(tmp_path)
    c.load()
    return c, Core


# ==================== _ConfigTree ====================

def test_tree_keys_sorted_by_order_desc(loaded, tmp_path):
    c, _ = loaded
    write_json(tmp_path / "state.json", {"ui": {}})
    reg(ConfigFile.JSON, "state.json", name="state", order=20)
    reg(ConfigFile.INI, "low.ini", name="low", order=1)

    assert c.tree.keys() == ["state", "main", "low"]


def test_tree_keys_include_unloaded_files(loaded, tmp_path):
    c, _ = loaded
    write_ini(tmp_path / "extra.ini", "[x]\n")
    reg(ConfigFile.INI, "extra.ini", name="extra", order=5)

    assert "extra" in c.tree
    assert len(c.tree) == 2


def test_tree_values_items_len_iter(loaded):
    c, _ = loaded

    # 视图无状态、每次新建，所以不能比对象相等，只比名字与内容
    assert [name for name, _view in c.tree.items()] == c.tree.keys()
    assert [v._name for v in c.tree.values()] == c.tree.keys()
    assert len(c.tree) == 1
    assert [name for name in c.tree] == ["main"]


def test_tree_contains(loaded):
    c, _ = loaded
    assert "main" in c.tree
    assert "nope" not in c.tree
    assert "_secret" not in c.tree       # 下划线开头视为内部
    assert 123 not in c.tree             # 非字符串不炸


def test_tree_dir_matches_keys(loaded):
    c, _ = loaded
    assert dir(c.tree) == c.tree.keys()


# ==================== _FileView ====================

def test_file_view_keys_are_sections(loaded):
    c, _ = loaded
    assert c.tree.main.keys() == ["core"]
    assert len(c.tree.main) == 1
    assert list(c.tree.main) == ["core"]
    assert "core" in c.tree.main
    assert "nope" not in c.tree.main


def test_file_view_values_are_section_views(loaded):
    c, _ = loaded
    views = c.tree.main.values()
    assert len(views) == 1
    assert views[0].keys() == ["debug", "name", "port"]
    assert dict(c.tree.main.items()).keys() == {"core"}


def test_file_view_keys_empty_when_not_loaded(tmp_path):
    """枚举宽容：未加载 → 空列表，不抛错（``__getattr__`` 才抛）。"""
    write_ini(tmp_path / "config.ini", INI)
    reg(ConfigFile.INI, "config.ini", name="main", autosave="off")
    c = Configue(tmp_path)

    assert c.tree.main.keys() == []
    assert len(c.tree.main) == 0
    assert list(c.tree.main) == []
    assert "core" not in c.tree.main
    assert c.tree.main.values() == []
    assert c.tree.main.items() == []


def test_file_view_only_lists_sections_it_contributes(tmp_path):
    """被高优先级文件完全覆盖的 section 不出现在低优先级文件的 keys() 里。"""
    write_ini(tmp_path / "config.ini", "[core]\nname = hello\n")
    reg(ConfigFile.INI, "config.ini", name="main", order=10, autosave="off")
    write_ini(tmp_path / "machine.ini", "[core]\nname = machine\n")
    reg(ConfigFile.INI, "machine.ini", name="machine", order=20, autosave="off")

    c = Configue(tmp_path)
    c.load()

    # main 的 core/name 被 machine 覆盖 → main 不再提供任何生效值
    assert c.tree.main.keys() == []
    assert c.tree.machine.keys() == ["core"]


def test_file_view_keys_after_unload(loaded):
    c, _ = loaded
    c.unload("main")

    assert c.tree.main.keys() == []
    assert len(c.tree.main) == 0


# ==================== _SectionView ====================

def test_section_keys_values_items(loaded):
    c, _ = loaded
    section = c.tree.main.core

    assert section.keys() == ["debug", "name", "port"]
    assert section.values() == [True, "hello", 8000]
    assert section.items() == [
        ("debug", True), ("name", "hello"), ("port", 8000),
    ]
    assert len(section) == 3
    assert list(section) == ["debug", "name", "port"]
    assert "debug" in section
    assert "nope" not in section
    assert 1 not in section


def test_section_keys_skip_declared_but_valueless_fields(loaded):
    """声明了但没有默认值、文件里也没写的字段：不进 keys()，访问即抛。

    这是有意的：``keys()`` / ``values()`` / ``items()`` 必须一一对应，
    否则 ``for k in keys(): view[k]`` 会在缺值字段上炸。
    """
    c, _ = loaded

    @configclass("core", "main")
    class Extender:
        missing: str          # 无默认值，文件里也没有

    assert "missing" not in c.tree.main.core
    assert "missing" not in c.tree.main.core
    with pytest.raises(AttributeError):
        _ = c.tree.main.core.missing


def test_section_dir(loaded):
    c, _ = loaded
    assert dir(c.tree.main.core) == ["debug", "name", "port"]


def test_section_keys_include_runtime_new_keys(loaded):
    c, _ = loaded
    c.tree.main.core.brand_new = 1

    assert "brand_new" in c.tree.main.core
    assert c.tree.main.core.brand_new == 1


def test_section_view_of_missing_section_is_empty(loaded):
    c, _ = loaded
    section = c.tree.main.nope        # __getattr__ 只要求 file loaded

    assert section.keys() == []
    assert len(section) == 0
    assert section.values() == []
    assert list(section) == []


def test_section_items_reflect_sets(loaded):
    c, _ = loaded
    c.tree.main.core.name = "changed"

    assert ("name", "changed") in c.tree.main.core.items()
