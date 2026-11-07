#
"""``Configue.value_sources``：一个节点的「值来自哪些文件」聚合出口。

用途是设置页的「这个值从哪来」视图与「我改了为什么没生效」排查：
``definitions_of`` / ``values_of_file`` / ``origin_of`` 都在 resolver 内部，
这里给一个**只读诊断**出口（不加缓存，跟着当前状态走）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from albuswall.configue import ConfigFile, Configue, configclass, reg


@pytest.fixture
def two_files(tmp_path: Path):
    """低优先级 ``a.ini`` 与高优先级 ``b.ini`` 都定义 ``[core] debug``。"""
    (tmp_path / "a.ini").write_text(
        "[core]\ndebug = false\nonly_in_a = ${core:debug}\n",
        encoding="utf-8",
    )
    (tmp_path / "b.ini").write_text("[core]\ndebug = true\n", encoding="utf-8")
    reg(ConfigFile.INI, "a.ini", name="a", order=10)
    reg(ConfigFile.INI, "b.ini", name="b", order=20)

    @configclass("core", "a")
    class Core:
        debug: bool = False
        only_in_a: str = "${core:debug}"

    cfg = Configue(base_dir=tmp_path)
    cfg.load()
    return cfg


def _paths(sources) -> list[Path]:
    return [source["file"] for source in sources]


def test_lists_all_defining_files_low_to_high(two_files):
    sources = two_files.value_sources("core", "debug")

    assert _paths(sources) == [
        two_files.base_dir / "a.ini",
        two_files.base_dir / "b.ini",
    ]
    assert [source["raw"] for source in sources] == ["false", "true"]


def test_effective_only_on_winning_file(two_files):
    sources = two_files.value_sources("core", "debug")
    assert [source["effective"] for source in sources] == [False, True]
    assert sources[-1]["effective"] is True
    assert two_files.resolver.origin_of("core", "debug") == sources[-1]["file"]


def test_files_are_path_objects_not_str(two_files):
    for source in two_files.value_sources("core", "debug"):
        assert isinstance(source["file"], Path)


def test_raw_is_unexpanded(two_files):
    """``raw`` 是各文件里写的原文，不展开 ``${}``。"""
    sources = two_files.value_sources("core", "only_in_a")

    assert len(sources) == 1
    assert sources[0]["raw"] == "${core:debug}"
    # 对照：读值路径会展开（生效的 debug 来自高优先级的 b.ini，即 "true"）
    assert two_files.resolver.get_str("core", "only_in_a") == "true"


def test_readonly_flag_follows_marked_file(two_files):
    two_files.resolver.mark_readonly_file(two_files.base_dir / "a.ini")

    sources = two_files.value_sources("core", "debug")
    assert [source["readonly"] for source in sources] == [True, False]


def test_undefined_node_returns_empty(two_files):
    assert two_files.value_sources("core", "ghost") == []
    assert two_files.value_sources("nope", "nothing") == []


def test_single_definition(two_files):
    sources = two_files.value_sources("core", "only_in_a")
    assert len(sources) == 1
    assert sources[0]["file"] == two_files.base_dir / "a.ini"
    assert sources[0]["effective"] is True
    assert sources[0]["readonly"] is False


def test_effective_follows_origin_after_runtime_set(two_files):
    """运行期把值写到另一个文件后，``effective`` 跟着 origin 走。"""
    two_files.resolver.set("core", "debug", "false")
    two_files.resolver.set_origin("core", "debug", two_files.base_dir / "b.ini")

    sources = two_files.value_sources("core", "debug")
    assert [source["effective"] for source in sources] == [False, True]
