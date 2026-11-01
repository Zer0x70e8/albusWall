#
"""``_ConfigSlot`` 运行时：``Config().field`` 的读取与严格 isinstance 赋值。"""

from __future__ import annotations

from typing import Any, Optional

import pytest

from albuswall.configue import (
    ConfigFile,
    ConfigMissingError,
    Configue,
    configclass,
    field,
    reg,
)
from albuswall.configue.registry import _CLASSES

from .conftest import write_ini

INI = "[core]\ndebug = true\nname = hello\n"


@pytest.fixture
def env(tmp_path):
    write_ini(tmp_path / "config.ini", INI)
    reg(ConfigFile.INI, "config.ini", name="main", autosave="off")

    @configclass("core", "main")
    class Core:
        debug: bool = field(default=False)
        name: str = field(default="x")
        nothing: Optional[int] = None
        untyped: Any = None

    c = Configue(tmp_path)
    c.load()
    return c, Core


# ==================== __get__ ====================

def test_get_returns_typed_value(env):
    _, Core = env
    assert Core().debug is True
    assert Core().name == "hello"
    assert Core().nothing is None


def test_get_on_unregistered_class_raises(env):
    _, Core = env

    class Stranger:
        debug = Core.__dict__["debug"]  # 借用描述符，但类本身没登记

    with pytest.raises(RuntimeError, match="not registered in _CLASSES"):
        _ = Stranger().debug


def test_get_on_class_removed_from_registry_raises(env):
    """登记表里没有这个类（不是"被顶掉"——同名声明现在是共享）→ 当场炸。"""
    _, Core = env

    _CLASSES.pop(Core)
    with pytest.raises(RuntimeError, match="not registered in _CLASSES"):
        _ = Core().debug


def test_get_without_configue_raises_config_missing():
    @configclass("core", "main")
    class Core:
        debug: bool = True

    with pytest.raises(ConfigMissingError, match="Configue not initialized"):
        _ = Core().debug


def test_get_before_load_raises_config_missing(tmp_path):
    reg(ConfigFile.INI, "config.ini", name="main")

    @configclass("core", "main")
    class Core:
        debug: bool = True

    Configue(tmp_path)

    with pytest.raises(ConfigMissingError, match="Configue.load"):
        _ = Core().debug


# ==================== __set__：严格 isinstance ====================

def test_set_accepts_matching_type(env):
    c, Core = env
    Core().debug = False

    assert Core().debug is False
    assert ("core", "debug") in c.resolver.dirty_nodes()


def test_set_rejects_wrong_type(env):
    _, Core = env

    with pytest.raises(TypeError, match="expected <class 'bool'>, got str"):
        Core().debug = "yes"


def test_set_rejects_none_for_non_optional(env):
    _, Core = env

    with pytest.raises(TypeError, match="got None"):
        Core().debug = None


def test_set_allows_none_for_optional(env):
    _, Core = env
    Core().nothing = None
    assert Core().nothing is None

    Core().nothing = 3
    assert Core().nothing == 3


def test_set_rejects_wrong_type_inside_optional(env):
    _, Core = env

    with pytest.raises(TypeError, match="got str"):
        Core().nothing = "nope"


def test_set_any_accepts_everything(env):
    _, Core = env
    Core().untyped = {"anything": [1, 2]}
    assert Core().untyped == {"anything": [1, 2]}


def test_set_on_unregistered_class_raises(env):
    _, Core = env

    class Stranger:
        debug = Core.__dict__["debug"]

    with pytest.raises(RuntimeError, match="not registered in _CLASSES"):
        Stranger().debug = True


def test_set_without_configue_raises_config_missing():
    @configclass("core", "main")
    class Core:
        debug: bool = True

    with pytest.raises(ConfigMissingError, match="Configue not initialized"):
        Core().debug = False


def test_set_schedules_persist_for_now_policy(tmp_path):
    write_ini(tmp_path / "config.ini", INI)
    reg(ConfigFile.INI, "config.ini", name="main", autosave="now")

    @configclass("core", "main")
    class Core:
        debug: bool = False

    Configue(tmp_path).load()
    Core().debug = False

    text = (tmp_path / "config.ini").read_text(encoding="utf-8")
    assert "debug = false" in text
