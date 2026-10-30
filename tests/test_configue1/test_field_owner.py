#
"""``_FIELD_OWNER``：字段 → 声明文件的反查表，以及无 origin 时的写回兜底。"""

from __future__ import annotations

from pathlib import Path

import pytest

from albuswall.configue1 import (
    ConfigError,
    ConfigFile,
    Configue,
    configclass,
    field,
    get_backend,
    reg,
)
from albuswall.configue1.backends.ini import INIBackend
from albuswall.configue1.backends.json import JSONBackend
from albuswall.configue1.registry import _FIELD_OWNER, _FILES

from .conftest import write_ini, write_json


@pytest.fixture
def two_files(tmp_path: Path):
    write_ini(tmp_path / "config.ini", "[core]\nname = hello\n")
    write_json(tmp_path / "state.json", {"core": {"port": 1}})
    reg(ConfigFile.INI, "config.ini", name="main", order=10, autosave="off")
    reg(ConfigFile.JSON, "state.json", name="state", order=20, autosave="off")

    @configclass("core", "main")
    class Core:
        name: str = "x"
        extra: str = field(default="dflt")   # 文件里没有 → 只有默认值

    @configclass("core", "state")
    class State:
        port: int = 0

    c = Configue(tmp_path)
    c.load()
    return c, Core, State


# ==================== 表本身 ====================

def test_field_owner_filled_by_configclass():
    @configclass("core", "main")
    class Core:
        a: int = 1
        b: str = "x"

    assert _FIELD_OWNER[("core", "a")] == "main"
    assert _FIELD_OWNER[("core", "b")] == "main"


def test_field_owner_last_registration_wins():
    @configclass("core", "first")
    class First:
        a: int = 1

    @configclass("core", "second")
    class Second:
        a: int = 2

    assert _FIELD_OWNER[("core", "a")] == "second"


def test_field_owner_ignores_private_and_classvar():
    from typing import ClassVar

    @configclass("core", "main")
    class Core:
        _hidden: int = 1
        registry: ClassVar[dict] = {}

    assert ("core", "_hidden") not in _FIELD_OWNER
    assert ("core", "registry") not in _FIELD_OWNER


# ==================== 兜底写回 ====================

def test_target_file_for_uses_owner_when_no_origin(two_files):
    c, Core, _ = two_files
    # extra 只有默认值，没有 origin
    assert c.resolver.origin_of("core", "extra") is None

    target, finfo = c._target_file_for(("core", "extra"))

    assert target == (c.base_dir / "config.ini").resolve()
    assert finfo.name == "main"
    assert isinstance(get_backend(finfo.backend), INIBackend)


def test_target_file_for_picks_backend_of_owning_file(two_files):
    c, _, _ = two_files

    _target, finfo = c._target_file_for(("core", "port"))

    assert finfo.name == "state"
    assert isinstance(get_backend(finfo.backend), JSONBackend)


def test_flush_of_default_only_field_uses_owner_file(two_files):
    c, Core, _ = two_files

    Core().extra = "written"
    written = c.flush()

    assert written == [(c.base_dir / "config.ini").resolve()]
    assert "written" in (c.base_dir / "config.ini").read_text(encoding="utf-8")


def test_target_file_for_raises_for_undeclared_key(two_files):
    c, _, _ = two_files

    with pytest.raises(ConfigError, match="no owning file"):
        c._target_file_for(("core", "never_declared"))


def test_flush_of_undeclared_key_is_recorded_not_raised(two_files):
    c, _, _ = two_files
    c.resolver.set("core", "never_declared", 1, persist=True)

    assert c.flush() == []
    assert c.last_flush_error is not None
    assert "no owning file" in str(c.last_flush_error)
    # 写失败要保留 dirty，下次还能重试
    assert ("core", "never_declared") in c.resolver.dirty_nodes()


def test_flush_strict_raises_for_undeclared_key(two_files):
    c, _, _ = two_files
    c.resolver.set("core", "never_declared", 1, persist=True)

    with pytest.raises(ConfigError, match="no owning file"):
        c.flush(strict=True)


def test_target_file_for_raises_when_owning_file_unloaded(tmp_path):
    write_ini(tmp_path / "config.ini", "[core]\n")

    @configclass("core", "main")
    class Core:
        extra: str = "dflt"

    reg(ConfigFile.INI, "config.ini", name="main", autosave="off")
    c = Configue(tmp_path)
    c.load()
    c.unload("main")

    with pytest.raises(ConfigError, match="not loaded"):
        c._target_file_for(("core", "extra"))


def test_target_file_for_prefers_writable_origin_over_owner(tmp_path):
    """origin 可写时永远写 origin，轮不到 _FIELD_OWNER 兜底。"""
    write_ini(tmp_path / "config.ini", "[core]\nextra = from-file\n")
    write_json(tmp_path / "state.json", {})

    @configclass("core", "state")   # 声明在 state，实际值来自 main
    class Core:
        extra: str = "dflt"

    reg(ConfigFile.INI, "config.ini", name="main", order=10, autosave="off")
    reg(ConfigFile.JSON, "state.json", name="state", order=20, autosave="off")
    c = Configue(tmp_path)
    c.load()

    target, finfo = c._target_file_for(("core", "extra"))

    assert finfo.name == "main"
    assert target == (tmp_path / "config.ini").resolve()
    assert _FILES["state"].loaded is True
