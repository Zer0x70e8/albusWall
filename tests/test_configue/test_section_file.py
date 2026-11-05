#
"""``target_file_of`` 的兜底优先级：``_FIELD_OWNER`` **赢过** 段级映射。

背景（审计 A8 的可查部分）：当前 ``[shortcuts]`` 全部是「加载钩子注册类型」
的动态 key，没有 ``@configclass`` 字段，所以 ``_FIELD_OWNER`` 与
``Configue.register_section_file`` 不冲突——但这只是**暂时状态**。将来谁给
``[shortcuts]`` 加一个声明字段，两条兜底就会同时命中，优先级必须写进测试。

规则（``Configue.target_file_of`` 的实现顺序）：

    origin → 其它定义文件（可写） → ``_FIELD_OWNER`` → 段级映射 → ``ConfigError``

本文件把三条锁住：

1. 声明字段（有 ``_FIELD_OWNER``）→ 指向**声明它的文件**，段级映射不参与；
2. 无声明、只有只读文件定义过的 key（钩子注册类型）→ 段级映射指定文件；
3. 段级映射**精确匹配段名**：登记 ``shortcuts`` 不影响 ``shortcuts.extra``；
   且映射与文件加载状态解耦（``unload`` 后映射还在，只是目标文件未加载 → 报错）。

用例自带 ``Configue`` 隔离（登记表是进程全局的），互不依赖执行顺序。
"""

from __future__ import annotations

from contextlib import suppress
from pathlib import Path

import pytest

from albuswall.common.keymap import SECTION
from albuswall.configue import Configue, configclass, field
from albuswall.configue.bootstrap import setup_config
from albuswall.configue.errors import ConfigError
from albuswall.configue.registry import (
    _CLASSES,
    _FIELD_OWNER,
    _FILES,
    _PATHS,
    field_owner,
)
from albuswall.core import Container

from .conftest import write_ini


def _reset_registry() -> None:
    for table in (_FILES, _CLASSES, _PATHS, _FIELD_OWNER):
        table.clear()


@pytest.fixture
def config(tmp_path: Path):
    """portable 配置：主文件 + 随包默认表 + 用户覆盖表，段级映射指向用户表。

    额外声明一个 ``[shortcuts]`` 字段（``profile``），模拟「将来有人给这个段
    加了 ``@configclass`` 字段」的场景——它必须在 ``setup_config`` 之前装饰，
    这样 ``load()`` 的类型自检与默认值注入才认得它。
    """
    previous = Configue._instance
    _reset_registry()
    write_ini(tmp_path / "config.ini", "[ui]\ntheme = default\n")

    @configclass(SECTION, "main")
    class ShortcutExtras:
        #: 假设的设置页可见项：声明在 main，写回也必须落在 main。
        profile: str = field(default="default")

    instance = setup_config(Container(), base_dir=tmp_path, portable=True)
    try:
        yield instance
    finally:
        with suppress(Exception):
            instance.close()
        Configue._instance = previous
        _reset_registry()


# ==================== 1. 声明字段赢过段级映射 ====================

def test_field_owner_wins_over_section_file(config: Configue) -> None:
    """``_FIELD_OWNER`` 命中的节点：写回声明文件（config.ini），不走段级映射。"""
    node = (SECTION, "profile")

    # 声明存在（装饰期就填好，不依赖 load）
    assert field_owner(*node) == "main"
    # 段级映射也确实登记了（否则这条测试等于没测冲突）
    assert config._section_files[SECTION] == "shortcuts"  # noqa: SLF001

    target = config.target_file_of(*node)

    assert target == config.base_dir / "config.ini", (
        "声明字段必须赢过段级映射"
    )
    assert target != config.base_dir / "shortcuts.ini"


def test_declared_shortcut_field_appears_in_the_schema(config: Configue) -> None:
    """顺带确认：声明了字段之后 ``[shortcuts]`` 才会进 schema 树。"""
    from albuswall.configue.schema import dump_schema_dict

    dump = dump_schema_dict(config)
    shortcuts_items = [
        item for item in dump["items"] if item["section"] == SECTION
    ]

    assert [item["key"] for item in shortcuts_items] == [
        f"{SECTION}.profile",
    ]
    assert shortcuts_items[0]["target_file"] == str(
        config.base_dir / "config.ini")


# ==================== 2. 无声明 key 走段级映射 ====================

def test_section_file_fallback_for_undeclared_key(config: Configue) -> None:
    """钩子注册类型、且只有随包（只读）表定义过的 key → 落到段级映射的文件。"""
    node = (SECTION, "view.reset")

    assert field_owner(*node) is None            # 没有 @configclass 字段
    origin = config.resolver.origin_of(*node)
    assert origin is not None and config.is_readonly_path(origin)
    assert [str(p) for p in config.resolver.definitions_of(*node)] == [str(origin)]

    assert config.target_file_of(*node) == config.base_dir / "shortcuts.ini"


# ==================== 3. 精确匹配 + 与文件生命周期解耦 ====================

def test_section_file_matches_the_section_name_exactly(config: Configue) -> None:
    """登记 ``shortcuts`` 不影响 ``shortcuts.extra``（不做前缀匹配）。"""
    config.resolver.inject_raw("shortcuts.extra", "x", "1")   # 无 origin

    assert config._section_files.get("shortcuts.extra") is None  # noqa: SLF001
    with pytest.raises(ConfigError, match="no owning file"):
        config.target_file_of("shortcuts.extra", "x")

    # 同一个文件里，精确命中的段照常可用
    assert config.target_file_of(SECTION, "view.reset") == (
        config.base_dir / "shortcuts.ini")


def test_section_file_survives_unload(config: Configue) -> None:
    """``unload`` 不清段级映射：它描述的是「段 → 文件」的静态约定。

    映射还在，但目标文件已经不是「已加载」状态 → ``target_file_of`` 报
    「declares file ... which is not loaded」，而不是悄悄换个文件写。
    """
    assert config._section_files[SECTION] == "shortcuts"  # noqa: SLF001

    config.unload("shortcuts")

    assert config._section_files[SECTION] == "shortcuts"  # noqa: SLF001  映射还在
    with pytest.raises(ConfigError, match="not loaded"):
        config.target_file_of(SECTION, "view.reset")
