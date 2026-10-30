#
"""装配层烟雾测试：证明 fixture 隔离 + MODULES 装配 + freeze 语义。

这里只断言「注册表长什么样」，不执行业务命令 —— 后者在
``test_handlers_commands.py``。
"""

from __future__ import annotations

import pytest

from albuswall.command import CommandRegistry, get_default_registry
from albuswall.command.exceptions import RegistryFrozen
from albuswall.service.command.handlers import setup as setup_handlers


def test_setup_registers_builtin_commands(assembled):
    """``setup_handlers`` 之后，builtin 的 4 条诊断命令都在。"""
    registry, _ = assembled
    names = set(registry.names(include_hidden=True, subject=frozenset({"admin"})))
    assert {"ping", "help", "parse", "version"} <= names


def test_two_registries_are_isolated(fresh_registry, fake_container):
    """两个用例各用一张表：互相看不见对方注册的命令。"""
    other = CommandRegistry()
    setup_handlers(fresh_registry, fake_container)
    setup_handlers(other, fake_container)

    assert fresh_registry is not other
    assert fresh_registry.names() == other.names()          # 初始一致

    fresh_registry.register(_dummy("only.in.first"))
    assert "only.in.first" in fresh_registry.names()
    assert "only.in.first" not in other.names()             # 未串味


def test_register_after_setup_raises_registry_frozen(fresh_registry, fake_container):
    """freeze 是「装配已结束」的断言：之后任何 register 都拒绝。"""
    setup_handlers(fresh_registry, fake_container)
    fresh_registry.freeze()
    assert fresh_registry.frozen is True

    with pytest.raises(RegistryFrozen) as excinfo:
        fresh_registry.register(_dummy("late.command"))
    assert "cannot register" in str(excinfo.value)

    with pytest.raises(RegistryFrozen):
        setup_handlers(fresh_registry, fake_container)      # 重复装配同样被拒


def test_fresh_registry_is_not_process_global(fresh_registry):
    """显式断言隔离：fixture 给的绝不是进程全局单例。"""
    assert fresh_registry is not get_default_registry()


def _dummy(path: str):
    from albuswall.command import Command

    return Command(path=path, func=lambda: None)
