#
"""``Configue.unload`` 与 ``Resolver._defaults`` 的解耦语义。

``_defaults`` 记录的是**声明**（``@configclass`` 字段的默认值），不是**文件
加载**的产物：
- 它由 ``load()`` 的 Step 4 登记，随声明存在，与「哪个文件被加载」无关；
- ``unload(name)`` 只撤文件对 Resolver 的贡献（``_raw`` / ``_origins`` /
  ``_definitions`` / ``_file_raw``），**不动** ``_defaults``；
- 这不会造成误用：``reset()`` 的分支 1 先看 ``origin``，``unload`` 已经把它
  摘掉了，所以残留的默认值在 reset 里走不到（直接 ``return False``）。

用例自带 ``Configue`` 隔离（登记表是进程全局的），互不依赖执行顺序。
"""

from __future__ import annotations

from contextlib import suppress
from pathlib import Path

import pytest

from albuswall.configue import Configue, configclass, field, reg
from albuswall.configue.registry import (
    _CLASSES,
    _FIELD_OWNER,
    _FILES,
    _PATHS,
    ConfigFile,
)

from .conftest import write_ini


def _reset_registry() -> None:
    for table in (_FILES, _CLASSES, _PATHS, _FIELD_OWNER):
        table.clear()


@pytest.fixture
def config(tmp_path: Path):
    """``[core] name`` 由文件提供；``Core`` 声明了它自己的默认值。"""
    previous = Configue._instance
    _reset_registry()
    write_ini(tmp_path / "config.ini", "[core]\nname = from-file\n")

    @configclass("core", "main")
    class Core:
        name: str = field(default="declared-default")
        other: str = field(default="other-default")

    reg(ConfigFile.INI, "config.ini", name="main", autosave="manual")
    instance = Configue(base_dir=tmp_path)
    instance.load()
    try:
        yield instance
    finally:
        with suppress(Exception):
            instance.close()
        Configue._instance = previous
        _reset_registry()


def test_unload_keeps_defaults_but_reset_skips_them(config: Configue) -> None:
    """``unload`` 不清 ``_defaults``；但 ``reset`` 因 origin 已摘而走分支 1。"""
    node = ("core", "name")
    resolver = config.resolver

    assert resolver.origin_of(*node) is not None      # 值来自文件
    assert resolver.default_of(*node) == (True, "declared-default")

    config.unload("main")

    # 1) 声明的默认值还在（与文件生命周期无关）
    assert resolver.default_of(*node) == (True, "declared-default")
    # 2) 但节点的值没了（唯一来源是刚被卸掉的文件）
    assert node not in resolver
    assert resolver.origin_of(*node) is None
    # 3) reset 走分支 1（无 origin）→ False，不会拿残留默认值做回退
    assert config.reset(*node) is False
    assert node not in resolver
    assert resolver.is_reset_pending(node) is False


def test_unload_does_not_change_the_defaults_table_size(
        config: Configue, tmp_path: Path,
) -> None:
    """``_defaults`` 的规模由声明决定，不随 load/unload 增减。"""
    before = len(config.resolver._defaults)  # noqa: SLF001 - 断言的就是这张表
    assert before >= 2                        # Core 的两个字段

    config.unload("main")
    assert len(config.resolver._defaults) == before  # noqa: SLF001

    config.load()                                     # 重新加载同一个文件
    assert len(config.resolver._defaults) == before  # noqa: SLF001
    assert config.resolver.get_typed("core", "name") == "from-file"


def test_reload_after_unload_reinjects_the_file_value(config: Configue) -> None:
    """卸掉再加载：文件里的值回来，声明默认值不参与（文件赢）。"""
    config.unload("main")

    config.load()

    assert config.resolver.get_typed("core", "name") == "from-file"
    assert config.resolver.origin_of("core", "name") == (
        config.base_dir / "config.ini")


def test_a_node_that_only_had_a_declared_default_survives_unload(
        config: Configue,
) -> None:
    """``other`` 只来自声明默认值（无 origin）：``unload`` 不该把它带走。"""
    node = ("core", "other")
    assert config.resolver.origin_of(*node) is None
    assert config.resolver.get_typed(*node) == "other-default"

    config.unload("main")

    assert config.resolver.get_typed(*node) == "other-default"
