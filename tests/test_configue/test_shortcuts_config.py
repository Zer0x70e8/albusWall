#
"""快捷键表接入配置器（缺口 1）：``ShortcutRecord`` + 双文件 ``reg``。

两半：

* ``tests/test_common_keymap.py`` 的解析/格式化语义在这里**不重复**测；
  本文件测的是「接进 ``configue`` 之后」的读值链路；
* 双文件合并（随包默认表 order=20 → 用户表 order=30，per-key 覆盖）、
  写回落点（``target_file_of`` 指向用户表、默认表只读）、以及
  ``setup_config`` 的登记副作用。

用例自带 ``Configue`` 隔离（登记表是进程全局的），互不依赖执行顺序。
"""

from __future__ import annotations

from contextlib import suppress
from pathlib import Path

import pytest

from albuswall.common.keymap import SECTION, ShortcutRecord
from albuswall.configue import Configue
from albuswall.configue.bootstrap import (
    SHORTCUT_DEFAULTS_FILE,
    SHORTCUT_USER_FILE,
    setup_config,
)
from albuswall.configue.registry import (
    _CLASSES,
    _FIELD_OWNER,
    _FILES,
    _PATHS,
)
from albuswall.core import Container

from .conftest import write_ini

USER_OVERRIDE = 'app.quit = ["Ctrl+Alt+Q"]\n'


def _reset_registry() -> None:
    for table in (_FILES, _CLASSES, _PATHS, _FIELD_OWNER):
        table.clear()


@pytest.fixture
def config(tmp_path: Path):
    """一个 portable 的配置：随包默认表 + 空用户表。"""
    previous = Configue._instance
    _reset_registry()
    write_ini(tmp_path / "config.ini", "[ui]\ntheme = default\n")
    instance = setup_config(Container(), base_dir=tmp_path, portable=True)
    try:
        yield instance
    finally:
        with suppress(Exception):
            instance.close()
        Configue._instance = previous
        _reset_registry()


# ==================== 登记 ====================

def test_setup_config_registers_both_shortcut_files(config: Configue) -> None:
    assert SHORTCUT_DEFAULTS_FILE in _FILES
    assert SHORTCUT_USER_FILE in _FILES

    defaults = _FILES[SHORTCUT_DEFAULTS_FILE]
    user = _FILES[SHORTCUT_USER_FILE]
    assert defaults.loaded and user.loaded
    # 用户表 order 更大 → load() 升序注入时后写、逐 key 覆盖
    assert defaults.order < user.order
    # 默认表随包（不在 base_dir 下），用户表在 base_dir 下
    assert defaults.resolved_path is not None
    assert "resources" in defaults.resolved_path.parts
    assert user.resolved_path == (config.base_dir / "shortcuts.ini")


def test_first_run_without_user_file_uses_the_bundled_defaults(
        config: Configue, tmp_path: Path,
) -> None:
    """用户表不存在（首次启动）：默认值照常可读，且不报错。"""
    assert not (tmp_path / "shortcuts.ini").exists()

    record = config.resolver.get_typed(SECTION, "window.fullscreen")

    assert isinstance(record, ShortcutRecord)
    assert record.keys == ("F11", "Ctrl+Shift+F")


# ==================== per-key 合并 ====================

def test_user_override_wins_per_key_only(tmp_path: Path) -> None:
    previous = Configue._instance
    _reset_registry()
    write_ini(tmp_path / "config.ini", "[ui]\ntheme = default\n")
    write_ini(tmp_path / "shortcuts.ini", f"[{SECTION}]\n{USER_OVERRIDE}")
    instance = setup_config(Container(), base_dir=tmp_path, portable=True)
    try:
        overridden = instance.resolver.get_typed(SECTION, "app.quit")
        untouched = instance.resolver.get_typed(SECTION, "view.reset")

        assert overridden.keys == ("Ctrl+Alt+Q",)
        # 用户没写的 key 仍然是随包默认值（per-key 合并，不是整表覆盖）
        assert untouched.keys == ("Ctrl+0",)
    finally:
        with suppress(Exception):
            instance.close()
        Configue._instance = previous
        _reset_registry()


def test_unbound_entries_read_as_empty_records(config: Configue) -> None:
    """``= []`` 的条目：记录存在但键位为空（显式解绑，不是「没配置」）。"""
    record = config.resolver.get_typed(SECTION, "window.album")

    assert isinstance(record, ShortcutRecord)
    assert record.keys == ()
    assert record.enabled is False


# ==================== 写回落点 ====================

def test_target_file_of_points_at_the_user_table(config: Configue) -> None:
    """默认表只读 → ``target_file_of`` 挑用户表（段级写回落点兜底）。"""
    target = config.target_file_of(SECTION, "app.quit")

    assert target == config.base_dir / "shortcuts.ini"


def test_bundled_table_is_marked_readonly(config: Configue) -> None:
    path = _FILES[SHORTCUT_DEFAULTS_FILE].resolved_path

    assert path is not None
    assert config.is_readonly_path(path) is True


def test_tree_write_lands_in_the_user_table(config: Configue, tmp_path: Path) -> None:
    """``tree.shortcuts.shortcuts[key] = record``：写用户表并落盘。

    注意路径是「两层」：``tree.<文件名>`` 是文件视图，``.<段名>`` 才是段视图。
    用户覆盖表的**注册名与段名都是** ``shortcuts``，所以看起来重复了一次。
    """
    bundled = _FILES[SHORTCUT_DEFAULTS_FILE].resolved_path
    assert bundled is not None
    before = bundled.read_bytes()

    config.tree.shortcuts.shortcuts["app.quit"] = ShortcutRecord(
        keys=("Ctrl+Alt+Q",),
    )
    config.flush(file=SHORTCUT_USER_FILE)

    text = (tmp_path / "shortcuts.ini").read_text(encoding="utf-8")
    assert "Ctrl+Alt+Q" in text
    assert bundled.read_bytes() == before


# ==================== 坏条目：只跳过它自己 ====================

def test_malformed_entry_does_not_block_the_startup(tmp_path: Path) -> None:
    """手滑写坏的记录：启动照常，坏的那一条退回槽的兜底键位。

    与改造前的 ``load_shortcuts(strict=False)`` 行为一致——配置文件是给人手改
    的，一条少个括号的记录不该让整个应用起不来。
    """
    previous = Configue._instance
    _reset_registry()
    write_ini(tmp_path / "config.ini", "[ui]\ntheme = default\n")
    write_ini(
        tmp_path / "shortcuts.ini",
        f'[{SECTION}]\napp.quit = ["Ctrl+Q", [1]\nview.reset = ["Ctrl+Alt+0"]\n',
    )
    instance = setup_config(Container(), base_dir=tmp_path, portable=True)
    try:
        # 坏 key 没被补类型（所以 check_all 不会去转换它）
        assert (SECTION, "app.quit") not in instance.resolver.typed_nodes()
        # 好的 key 照常工作
        assert instance.resolver.get_typed(SECTION, "view.reset").keys == (
            "Ctrl+Alt+0",
        )
    finally:
        with suppress(Exception):
            instance.close()
        Configue._instance = previous
        _reset_registry()


def test_registry_skips_the_malformed_entry(tmp_path: Path) -> None:
    """注册表读到坏值：跳过该命令（退回槽的兜底键位），其它照常。"""
    from albuswall.ui.keymap import ShortcutRegistry, ShortcutSlot

    previous = Configue._instance
    _reset_registry()
    write_ini(tmp_path / "config.ini", "[ui]\ntheme = default\n")
    write_ini(
        tmp_path / "shortcuts.ini",
        f'[{SECTION}]\nview.reset = ["Ctrl+Alt+0"]\n',
    )
    instance = setup_config(Container(), base_dir=tmp_path, portable=True)
    registry = ShortcutRegistry(config=instance)
    registry.register_slot(ShortcutSlot(
        id="view.reset", handler=lambda: None, default_keys=("Ctrl+0",),
    ))
    try:
        registry.load()
        assert [str(key) for key in registry.keys_for("view.reset")] == [
            "Ctrl+Alt+0",
        ]
    finally:
        registry.close()
        with suppress(Exception):
            instance.close()
        Configue._instance = previous
        _reset_registry()


# ==================== 类型补位（加载钩子） ====================

def test_load_hook_typed_every_shortcut_key(config: Configue) -> None:
    """``[shortcuts]`` 的 key 全部被 hook 补成 ``ShortcutRecord``。"""
    typed = config.resolver.types_of(SECTION)

    assert typed
    assert set(typed.values()) == {ShortcutRecord}


def test_shortcut_keys_are_declared_for_the_audit_switch(config: Configue) -> None:
    """``strict_undeclared`` 不应把 ``[shortcuts]`` 报成幽灵 key。"""
    assert config._undeclared_nodes() == []  # noqa: SLF001
