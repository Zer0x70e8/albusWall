#
"""``except ConfigClassNotRegistered`` 的护栏：编程错误不得被静默吞掉。

背景：``ConfigClassNotRegistered`` 同时继承 ``RuntimeError`` 与 ``ConfigError``
（见 ``configue/errors.py``），因此它会**新落入**三处既有的
``except ConfigError``：

=======================  ==========================================
位置                      语义
=======================  ==========================================
``check()``               文件路径解析失败 → 记 ``LoadError`` 继续
``load()`` Step 3         同上（逐文件）
``_node_owned_by()``      节点无归属文件 → ``return False`` 继续
=======================  ==========================================

这三处都是"配置缺失属正常，继续跑"，而 ``ConfigClassNotRegistered`` 是
"某个 ``@configclass`` 压根没登记上"的**编程错误** —— 必须当场炸。因此每处
都在宽捕获之前加了一条 ``except ConfigClassNotRegistered: raise``。

本文件用"让来源直接抛"的方式验证护栏存在：只要护栏去掉，异常就会被降级成
报告项 / ``False``，用例立刻失败。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from albuswall.configue import Configue, configclass, reg, ConfigFile
from albuswall.configue.errors import ConfigClassNotRegistered
from albuswall.configue.registry import _FILES

from .conftest import write_ini


def _make_configue(tmp_path: Path) -> Configue:
    """注册一个未加载的 ``main`` 文件（``load()`` 期间才会解析路径）。"""
    write_ini(tmp_path / "config.ini", "[core]\ndebug = true\n")

    @configclass("core", "main")
    class Core:
        debug: bool = False

    del Core
    reg(ConfigFile.INI, "config.ini", name="main")
    return Configue(tmp_path)


def _raiser(_finfo: object) -> Path:
    raise ConfigClassNotRegistered(
        "Core is not registered in _CLASSES (superseded)"
    )


# ==================== check() ====================

def test_check_reraises_class_not_registered(tmp_path, monkeypatch):
    """``check()`` 不得把"类被顶掉"降级成一条 ``LoadError``。"""
    config = _make_configue(tmp_path)
    assert _FILES, "用例前提：至少登记了一个文件"
    monkeypatch.setattr(config, "_resolve_file_path", _raiser)

    with pytest.raises(ConfigClassNotRegistered):
        config.check()


# ==================== load() Step 3 ====================

def test_load_reraises_class_not_registered(tmp_path, monkeypatch):
    """``load()`` 同样不得降级：启动期就该炸，而不是记一条 read_failed。"""
    config = _make_configue(tmp_path)
    monkeypatch.setattr(config, "_resolve_file_path", _raiser)

    with pytest.raises(ConfigClassNotRegistered):
        config.load()


# ==================== _node_owned_by() ====================

def test_node_owned_by_reraises_class_not_registered(tmp_path, monkeypatch):
    """归属判定里的 ``return False`` 也不得吞掉编程错误。"""
    config = _make_configue(tmp_path)

    def _raiser_target(node):
        raise ConfigClassNotRegistered("Core is not registered in _CLASSES")

    monkeypatch.setattr(config, "_target_file_for", _raiser_target)

    with pytest.raises(ConfigClassNotRegistered):
        config._node_owned_by(("core", "debug"), tmp_path / "config.ini")


def test_node_owned_by_still_returns_false_for_plain_config_error(tmp_path):
    """反面对照：**普通** ``ConfigError``（无归属文件）仍然安静地判 ``False``。"""
    config = _make_configue(tmp_path)
    config.load()

    # ("nowhere", "key") 没有 @configclass 声明 → _target_file_for 抛 ConfigError
    assert config._node_owned_by(("nowhere", "key"), tmp_path / "config.ini") is False


# ==================== 正常路径：护栏不可达（不掩盖 bug） ====================

def test_normal_path_does_not_trigger_class_not_registered_guard(tmp_path):
    """真实注册表 + 真实 ``Configue``：``check()`` / ``load()`` 正常跑完。

    证明护栏是"不可达时无害"，而不是"掩盖了一个可达的 bug"：这里的
    ``@configclass`` 类都在登记表里，``_resolve_file_path`` /
    ``_target_file_for`` 也都不读字段，所以 ``ConfigClassNotRegistered``
    一次都不该出现。
    """
    @configclass("core", "main")
    class Core:
        debug: bool = False
        name: str = "default-name"

    write_ini(tmp_path / "config.ini", "[core]\nname = hello\n")
    reg(ConfigFile.INI, "config.ini", name="main")
    config = Configue(tmp_path)

    report = config.check()
    assert not any(
        "ConfigClassNotRegistered" in str(err.message) for err in report.errors
    ), report.errors

    config.load()  # 正常路径不得抛任何 ConfigClassNotRegistered

    assert Core().name == "hello"   # 文件值生效
    assert Core().debug is False    # 默认值生效
