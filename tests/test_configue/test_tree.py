#
"""``configue.tree``：``_ConfigTree`` / ``_FileView`` / ``_SectionView``。"""

from __future__ import annotations

from pathlib import Path

import pytest

from albuswall.configue import (
    ConfigFile,
    Configue,
    configclass,
    field,
    reg,
)

from .conftest import write_ini

INI = "[core]\ndebug = true\nname = hello\nport = 1234\n"


@pytest.fixture
def loaded(tmp_path: Path):
    write_ini(tmp_path / "config.ini", INI)
    reg(ConfigFile.INI, "config.ini", name="main", autosave="off")

    @configclass("core", "main")
    class Core:
        debug: bool = field(default=False)
        name: str = field(default="x")
        port: int = field(default=0)

    c = Configue(tmp_path)
    c.load()
    return c, Core


def test_tree_attribute_and_item_access(loaded):
    c, _ = loaded
    assert c.tree.main is not None
    assert c.tree["main"].__class__ is c.tree.main.__class__
    assert c.tree.main.core.debug is True
    assert c.tree["main"]["core"]["port"] == 1234


def test_tree_unknown_file_raises_attribute_error(tmp_path):
    reg(ConfigFile.INI, "config.ini", name="main")
    c = Configue(tmp_path)

    with pytest.raises(AttributeError, match="no config file named"):
        _ = c.tree.nope


def test_tree_dir_and_repr(tmp_path):
    write_ini(tmp_path / "config.ini", INI)
    reg(ConfigFile.INI, "config.ini", name="main")
    c = Configue(tmp_path)

    assert dir(c.tree) == ["main"]
    assert repr(c.tree) == "_ConfigTree(files=1, loaded=0)"


def test_file_view_requires_loaded(tmp_path):
    write_ini(tmp_path / "config.ini", INI)
    reg(ConfigFile.INI, "config.ini", name="main", autosave="off")
    c = Configue(tmp_path)

    with pytest.raises(AttributeError, match="not loaded"):
        _ = c.tree.main.core


def test_tree_private_names_raise(tmp_path):
    c = Configue(tmp_path)
    with pytest.raises(AttributeError):
        _ = c.tree._secret


def test_section_view_reads_and_missing_key(loaded):
    c, _ = loaded
    assert c.tree.main.core.name == "hello"
    assert c.tree.main["core"]["name"] == "hello"

    with pytest.raises(AttributeError):
        _ = c.tree.main.core.nope

    with pytest.raises(AttributeError):
        _ = c.tree.main.core._private


def test_section_view_write_updates_resolver(loaded):
    c, Core = loaded

    c.tree.main.core.port = 4711

    assert c.tree.main.core.port == 4711
    assert Core().port == 4711
    assert ("core", "port") in c.resolver.dirty_nodes()


def test_section_view_write_sets_origin_to_its_file(loaded):
    c, _ = loaded
    expected = (Path(c.base_dir) / "config.ini").resolve()

    c.tree.main.core.name = "changed"

    assert c.resolver.origin_of("core", "name") == expected


def test_section_view_write_requires_loaded(tmp_path):
    write_ini(tmp_path / "config.ini", INI)
    reg(ConfigFile.INI, "config.ini", name="main", autosave="off")
    c = Configue(tmp_path)

    # 未加载时连 _FileView -> _SectionView 都拿不到（AttributeError），
    # 这里直接构造 section view 验证写入守卫。
    from albuswall.configue.tree import _SectionView

    view = _SectionView(c, "main", ("core",))
    with pytest.raises(RuntimeError, match="not loaded"):
        view.port = 1


def test_tree_view_does_not_type_check(loaded):
    """树是值总线：``debug: bool`` 也拦不住字符串。"""
    c, _ = loaded

    c.tree.main.core.debug = "yes"  # 不抛 TypeError

    assert c.resolver.get_str("core", "debug") == "yes"


def test_section_view_keys_items_contains(loaded):
    c, _ = loaded
    section = c.tree.main.core

    assert section.keys() == ["debug", "name", "port"]
    assert dict(section.items())["port"] == 1234
    assert "debug" in section
    assert "nope" not in section


def test_file_view_dir_lists_sections(loaded):
    c, _ = loaded
    assert dir(c.tree.main) == ["core"]


def test_reprs(loaded):
    c, _ = loaded
    assert repr(c.tree.main) == "_FileView(name='main', sections=['core'])"
    assert repr(c.tree.main.core) == (
        "_SectionView(file='main', section='core', keys=['debug', 'name', 'port'])"
    )


def test_file_view_after_unload(loaded):
    c, _ = loaded
    c.unload("main")

    with pytest.raises(AttributeError, match="not loaded"):
        _ = c.tree.main.core


def test_tree_section_view_allows_unregistered_key(loaded):
    """树视图是值总线：写未 ``@configclass`` 声明的 key 也允许。

    与 ``Config()`` 视图形成对比——``Config`` 的字段必须在装饰时声明过，
    否则类上根本没有这个属性；树视图只要求「文件已加载」，写完 origin 指向
    该文件，因此 ``save()`` 能把它落盘。
    """
    c, _ = loaded

    assert "brand_new" not in c.tree.main.core

    c.tree.main.core.brand_new = 42

    assert c.tree.main.core.brand_new == 42
    assert "brand_new" in c.tree.main.core
    assert c.tree.main.save() == [(Path(c.base_dir) / "config.ini").resolve()]
    assert "brand_new = 42" in (
        Path(c.base_dir) / "config.ini"
    ).read_text(encoding="utf-8")
