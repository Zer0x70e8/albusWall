#
"""「``load()`` 之后注册的 ``@configclass``」：默认值 / 类型即时注入。

背景：插件与 UI 模块在 ``setup_config`` + ``load()`` **之后**才被 import，
它们的 ``@configclass`` 装饰晚于 ``load()`` 的 Step 4（Step 4 只遍历「当时」
的 ``_CLASSES``），若装饰器不兜底，字段默认值永远进不了 Resolver——读侧每次
访问都要抛一次 ``ConfigMissingError``。

这些用例锁住四条边界：

1. 晚注册：默认值立刻生效；
2. 文件里已写的值**不被**默认值覆盖；
3. 运行期 ``set`` 过的值**不被**默认值覆盖；
4. 注册时机早于 ``load()`` 的类，仍走 Step 4（回归保护）；
5. 晚注册兜底同时注册类型（读侧转换 + 写侧 ``isinstance`` 校验）
   ——这一半逻辑最容易在重构时被误删。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from albuswall.configue import ConfigFile, Configue, configclass, reg

from .conftest import write_ini


@pytest.fixture
def loaded(tmp_path: Path):
    """一个已 ``load()`` 的 Configue（单文件 ``main``，不写盘）。"""
    write_ini(tmp_path / "config.ini", "[core]\nname = hello\n")
    reg(ConfigFile.INI, "config.ini", name="main", autosave="off")
    c = Configue(tmp_path)
    c.load()
    return c


# ==================== 晚注册：默认值生效 ====================

def test_late_class_default_injected_after_load(loaded):
    """晚于 load() 装饰的类，默认值自动注入。"""
    @configclass("late", "main")
    class LateConf:
        count: int = 7
        label: str = "dflt"

    assert ("late", "count") in loaded.resolver
    assert ("late", "label") in loaded.resolver
    assert LateConf().count == 7
    assert LateConf().label == "dflt"


def test_late_class_does_not_override_file_value(tmp_path: Path):
    """文件里已有值时，晚注册的类不覆盖。"""
    write_ini(tmp_path / "config.ini", "[late]\ncount = 3\n")
    reg(ConfigFile.INI, "config.ini", name="main", autosave="off")
    c = Configue(tmp_path)
    c.load()                                  # 此时还没有 LateConf

    @configclass("late", "main")
    class LateConf:
        count: int = 7

    assert LateConf().count == 3              # 文件值赢，默认值不覆盖


def test_late_class_does_not_override_runtime_set(loaded):
    """运行期 set 过的值，晚注册的类不覆盖。"""
    loaded.resolver.set("late", "count", 99)

    @configclass("late", "main")
    class LateConf:
        count: int = 7

    assert LateConf().count == 99


def test_early_class_still_uses_load_step4(tmp_path: Path):
    """早于 load() 的类仍走 Step 4（回归保护）。"""
    write_ini(tmp_path / "config.ini", "[core]\nname = hello\n")
    reg(ConfigFile.INI, "config.ini", name="main", autosave="off")

    @configclass("early", "main")
    class EarlyConf:
        flag: bool = True

    # 装饰时单例还不存在 → 不走晚注册兜底，默认值留给 load() 的 Step 4
    assert Configue._instance is None

    c = Configue(tmp_path)
    c.load()

    assert ("early", "flag") in c.resolver
    assert EarlyConf().flag is True


def test_late_class_register_type_for_later_set(tmp_path: Path):
    """晚注册兜底也把类型注册了，后续 set 的类型校验生效。"""
    write_ini(tmp_path / "config.ini", "[late]\ncount = 7\n")
    reg(ConfigFile.INI, "config.ini", name="main", autosave="off")
    c = Configue(tmp_path)
    c.load()                                  # 读进来时 count 还是字符串 "7"

    @configclass("late", "main")
    class LateConf:
        count: int = 0

    # 类型注册这一半：字符串被转成 int
    assert LateConf().count == 7
    assert isinstance(LateConf().count, int)

    # 写侧（``_ConfigSlot.__set__``）的严格校验同样可用
    with pytest.raises(TypeError, match="expected"):
        LateConf().count = "not an int"
