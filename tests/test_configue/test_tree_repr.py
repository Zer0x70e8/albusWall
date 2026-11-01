#
"""三层视图的 ``__repr__`` / ``__str__``。

只断言「形态包含哪些片段」，不比对全串——格式细节不该让测试变脆。
不显示值：``_SectionView`` 的 repr 带 keys 就够，带值会刷屏。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from albuswall.configue import ConfigFile, Configue, configclass, reg

from .conftest import write_ini

INI = "[core]\ndebug = true\nname = hello\n"


@pytest.fixture
def loaded(tmp_path: Path):
    write_ini(tmp_path / "config.ini", INI)
    reg(ConfigFile.INI, "config.ini", name="main", order=10, autosave="off")

    @configclass("core", "main")
    class Core:
        debug: bool = False
        name: str = "x"

    c = Configue(tmp_path)
    c.load()
    return c


# ==================== _ConfigTree ====================

def test_tree_repr_shows_counts(loaded, tmp_path):
    c = loaded
    assert repr(c.tree) == "_ConfigTree(files=1, loaded=1)"

    write_ini(tmp_path / "extra.ini", "[x]\n")
    reg(ConfigFile.INI, "extra.ini", name="extra", order=5)

    # 新登记但未加载 → 只涨 files，不涨 loaded
    assert repr(c.tree) == "_ConfigTree(files=2, loaded=1)"


def test_tree_str_matches_repr(loaded):
    c = loaded
    assert str(c.tree) == repr(c.tree)


# ==================== _FileView ====================

def test_file_view_repr_loaded(loaded):
    c = loaded
    assert repr(c.tree.main) == "_FileView(name='main', sections=['core'])"


def test_file_view_repr_not_loaded(tmp_path):
    write_ini(tmp_path / "config.ini", INI)
    reg(ConfigFile.INI, "config.ini", name="main", autosave="off")
    c = Configue(tmp_path)

    text = repr(c.tree.main)
    assert text.startswith("_FileView(name='main',")
    assert "not loaded" in text


def test_file_view_repr_unregistered(loaded):
    c = loaded
    # 直接构造一个指向未登记文件名的视图（模拟「reg 被覆盖/清理」的边界）
    from albuswall.configue.tree import _FileView

    text = repr(_FileView(c, "ghost"))
    assert text == "_FileView(name='ghost', <unregistered>)"


def test_file_view_repr_after_unload(loaded):
    c = loaded
    c.unload("main")
    assert "not loaded" in repr(c.tree.main)


def test_file_view_str_matches_repr(loaded):
    c = loaded
    assert str(c.tree.main) == repr(c.tree.main)


# ==================== _SectionView ====================

def test_section_view_repr_lists_keys(loaded):
    c = loaded
    assert repr(c.tree.main.core) == (
        "_SectionView(file='main', section='core', keys=['debug', 'name'])"
    )


def test_section_view_repr_does_not_show_values(loaded):
    c = loaded
    text = repr(c.tree.main.core)

    assert "hello" not in text       # 值不进 repr
    assert "true" not in text.lower()


def test_section_view_repr_empty_section(loaded):
    c = loaded
    assert repr(c.tree.main.nope) == (
        "_SectionView(file='main', section='nope', keys=[])"
    )


def test_section_view_str_matches_repr(loaded):
    c = loaded
    assert str(c.tree.main.core) == repr(c.tree.main.core)


def test_tree_repr_with_json_file(tmp_path):
    from .conftest import write_json

    write_json(tmp_path / "state.json", {"ui": {"theme": "dark"}})
    reg(ConfigFile.JSON, "state.json", name="state", autosave="off")
    c = Configue(tmp_path)
    c.load()

    assert repr(c.tree.state) == "_FileView(name='state', sections=['ui'])"
    assert "keys=['theme']" in repr(c.tree.state.ui)
