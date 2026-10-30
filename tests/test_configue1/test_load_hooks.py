#
"""``register_load_hook`` / ``unregister_load_hook``。

覆盖：幂等登记、注销、hook 失败收集成 ``hook_failed``、hook 能覆盖
``@configclass`` 类型、hook 在 ``check_all`` 之前运行、每次 ``load()`` 都跑。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from albuswall.configue1 import (
    _LOAD_HOOKS,
    ConfigError,
    ConfigFile,
    Configue,
    configclass,
    reg,
    register_load_hook,
    unregister_load_hook,
)
from albuswall.configue1.registry import _FILES

from .conftest import write_ini

SHORTCUTS_INI = "[shortcuts]\nopen = Ctrl+O\nquit = Ctrl+Q\n"


@pytest.fixture
def ini(tmp_path: Path) -> Path:
    write_ini(tmp_path / "config.ini", SHORTCUTS_INI)
    reg(ConfigFile.INI, "config.ini", name="main", autosave="off")
    return tmp_path


# ==================== 登记 / 注销 ====================

def test_register_is_idempotent():
    def hook(_resolver):
        pass

    register_load_hook(hook)
    register_load_hook(hook)
    register_load_hook(hook)

    assert [hook] == _LOAD_HOOKS


def test_unregister_removes_hook():
    def hook(_resolver):
        pass

    register_load_hook(hook)
    unregister_load_hook(hook)

    assert _LOAD_HOOKS == []


def test_unregister_unknown_hook_is_noop():
    unregister_load_hook(lambda _r: None)  # 不抛
    assert _LOAD_HOOKS == []


def test_load_hooks_are_not_cleared_by_load(ini):
    def hook(_resolver):
        pass

    register_load_hook(hook)
    c = Configue(ini)
    c.load()

    assert [hook] == _LOAD_HOOKS, "hook 是长期注册，load() 不能清空"


# ==================== 调用时机 / 效果 ====================

def test_hook_registers_types_for_runtime_keys(ini):
    """典型场景：``[shortcuts].<action_id>`` 的 key 到运行期才确定。"""
    def hook(resolver):
        for key in resolver.keys_of("shortcuts"):
            resolver.register_type("shortcuts", key, list[str])

    register_load_hook(hook)
    c = Configue(ini)
    c.load()

    assert c.resolver.get_typed("shortcuts", "open") == ["Ctrl+O"]
    assert c.resolver.get_typed("shortcuts", "quit") == ["Ctrl+Q"]


def test_hook_runs_every_load(ini):
    calls: list[int] = []

    def hook(resolver):
        calls.append(1)

    register_load_hook(hook)
    c = Configue(ini)
    c.load()
    c.load()

    assert len(calls) == 2


def test_hook_can_override_configclass_type(ini):
    """hook 在 Step 4 之后 → 可以覆盖 ``@configclass`` 声明的类型。"""

    @configclass("shortcuts", "main")
    class Shortcuts:
        open: str = ""

    def hook(resolver):
        for key in resolver.keys_of("shortcuts"):
            resolver.register_type("shortcuts", key, list[str])

    register_load_hook(hook)
    c = Configue(ini)
    c.load()

    # 泛型别名的 ``is`` 不可靠（每次 ``list[str]`` 都是新对象），用 ``==``
    assert c.resolver.converter_of("shortcuts", "open") == list[str]
    assert Shortcuts().open == ["Ctrl+O"]


def test_hook_runs_before_type_check(ini):
    """hook 补的类型必须被 ``check_all`` 采用（否则校验会拿旧类型失败）。"""
    write_ini(ini / "config.ini", "[nums]\nvalues = 1, 2, 3\n")
    reg(ConfigFile.INI, "config.ini", name="nums-file", order=5)

    def hook(resolver):
        resolver.register_type("nums", "values", list[int])

    register_load_hook(hook)

    @configclass("nums", "nums-file")
    class Nums:
        values: str = ""       # 声明成 str，会被 hook 覆盖成 list[int]

    c = Configue(ini)
    report = c.load()

    assert report.ok is True
    assert Nums().values == [1, 2, 3]


# ==================== 失败语义 ====================

def test_hook_failure_collected_as_load_error(ini):
    def boom(_resolver):
        raise RuntimeError("hook exploded")

    register_load_hook(boom)
    c = Configue(ini)

    report = c.load(partial=True)

    assert report.ok is False
    assert report.errors[0].kind == "hook_failed"
    assert "hook exploded" in report.errors[0].message
    assert "boom" in report.errors[0].message


def test_hook_failure_raises_unless_partial(ini):
    register_load_hook(lambda _r: 1 / 0)
    c = Configue(ini)

    with pytest.raises(ConfigError, match="hook_failed|load failed"):
        c.load()


def test_one_bad_hook_does_not_stop_others_or_file_loading(ini):
    def bad(_resolver):
        raise RuntimeError("bad")

    seen: list[str] = []

    def good(resolver):
        seen.append("good")
        resolver.register_type("shortcuts", "open", list[str])

    register_load_hook(bad)
    register_load_hook(good)
    c = Configue(ini)

    report = c.load(partial=True)

    assert seen == ["good"]
    assert report.loaded_files == ("main",)      # 文件照常加载
    assert {e.kind for e in report.errors} == {"hook_failed"}
    assert c.resolver.get_typed("shortcuts", "open") == ["Ctrl+O"]


def test_hook_failure_keeps_file_loaded(ini):
    register_load_hook(lambda _r: 1 / 0)
    c = Configue(ini)

    with pytest.raises(ConfigError):
        c.load()

    assert _FILES["main"].loaded is True
    assert c.tree.main.keys() == ["shortcuts"]
