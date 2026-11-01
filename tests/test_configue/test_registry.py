#
"""``configue.registry``：数据结构、reg()、@configclass 与前置修复回归。

对应上一轮 review 的 5 项修复，逐条有用例：

1. 重装饰刷新 slot 快照（不再 ``continue``）
2. ``_ConfigSlot`` 只有 ``(path, type, default)``，没有 label / description
3. ``classes_in()`` 按 ``ClassInfo.order`` **降序**
4. ``exists_check=True`` 且无活动 ``Configue`` → ``ConfigError``（不回退 cwd）
5. 契约写进 docstring（这里只做行为回归：``FieldInfo.default`` 是声明值，
   ``_ConfigSlot.type/default`` 只是装饰时快照）
"""

# 注意：本文件**不用** ``from __future__ import annotations``。
# 那会把注解变成字符串，让「注解解析失败 → Any」「撞方法名」这类用例
# 测到完全不同的代码路径。
from dataclasses import replace
from typing import Any, ClassVar, Optional

import pytest

from albuswall.configue import (
    ConfigError,
    ConfigFile,
    ConfigFileNotFound,
    Configue,
    classes_in,
    configclass,
    field,
    reg,
)
from albuswall.configue.registry import (
    _CLASSES,
    _FILES,
    MISSING,
    ClassInfo,
    FieldInfo,
    _ConfigSlot,
)

from .conftest import write_ini

# ==================== _ConfigSlot 形态（#2） ====================

def test_config_slot_has_exactly_three_fields():
    assert _ConfigSlot.__slots__ == ("path", "type", "default")

    slot = _ConfigSlot(("a", "b"), type=int, default=1)
    assert slot.path == ("a", "b")
    assert slot.type is int
    assert slot.default == 1

    # label / description 不再挂在描述符上
    assert not hasattr(slot, "label")
    assert not hasattr(slot, "description")


def test_config_slot_default_sentinel_is_missing():
    slot = _ConfigSlot(("a", "b"))
    assert slot.default is MISSING
    assert slot.type is None


# ==================== reg() ====================

def test_reg_stores_file_info():
    reg(ConfigFile.INI, "config.ini", name="main", order=7, autosave="off",
        debounce=0.5)

    finfo = _FILES["main"]
    assert finfo.name == "main"
    assert finfo.backend is ConfigFile.INI
    assert finfo.file_path == "config.ini"
    assert finfo.order == 7
    assert finfo.autosave == "off"
    assert finfo.debounce == 0.5
    assert finfo.loaded is False
    assert finfo.resolved_path is None


def test_reg_overwrites_same_name_and_resets_loaded():
    reg(ConfigFile.INI, "a.ini", name="main")
    _FILES["main"] = replace(_FILES["main"], loaded=True)

    reg(ConfigFile.INI, "b.ini", name="main")
    assert _FILES["main"].file_path == "b.ini"
    assert _FILES["main"].loaded is False


@pytest.mark.parametrize("bad_name", ["", "_internal"])
def test_reg_rejects_bad_name(bad_name: str):
    with pytest.raises(ValueError):
        reg(ConfigFile.INI, "a.ini", name=bad_name)


def test_reg_rejects_invalid_autosave():
    with pytest.raises(ValueError):
        reg(ConfigFile.INI, "a.ini", name="main", autosave="sometimes")  # type: ignore[arg-type]


def test_reg_rejects_negative_debounce():
    with pytest.raises(ValueError):
        reg(ConfigFile.INI, "a.ini", name="main", debounce=-1)


# ==================== exists_check（#4） ====================

def test_exists_check_without_configue_raises(tmp_path):
    """没有活动单例 → 明确报错，**不再**静默回退 cwd。"""
    write_ini(tmp_path / "config.ini", "[core]\n")

    with pytest.raises(ConfigError) as ei:
        reg(ConfigFile.INI, "config.ini", name="main", exists_check=True)
    assert "Configue not initialized" in str(ei.value)
    assert "exists_check" in str(ei.value)


def test_exists_check_uses_base_dir(tmp_path):
    write_ini(tmp_path / "config.ini", "[core]\n")
    Configue(tmp_path)

    reg(ConfigFile.INI, "config.ini", name="main", exists_check=True)
    assert "main" in _FILES


def test_exists_check_missing_file_raises(tmp_path):
    Configue(tmp_path)
    with pytest.raises(ConfigFileNotFound):
        reg(ConfigFile.INI, "nope.ini", name="main", exists_check=True)


def test_exists_check_does_not_resolve_relative_to_cwd(tmp_path, monkeypatch):
    """cwd 里有一个同名文件，但 base_dir 里没有 → 仍然报错。"""
    other = tmp_path / "cwd"
    other.mkdir()
    write_ini(other / "config.ini", "[core]\n")
    monkeypatch.chdir(other)

    base = tmp_path / "base"
    base.mkdir()
    Configue(base)

    with pytest.raises(ConfigFileNotFound):
        reg(ConfigFile.INI, "config.ini", name="main", exists_check=True)


# ==================== @configclass ====================

def test_configclass_collects_fields_and_installs_slots():
    @configclass("core", "main")
    class Core:
        debug: bool = field(default=False, label="调试", description="开日志")
        port: int = 8000
        title: str = field(default="x")
        _private: int = 1
        table: ClassVar[dict] = {}

    info = _CLASSES[Core]
    assert info.file == "main"
    assert info.path == ("core",)
    paths = [f.path for f in info.fields]
    assert paths == [("core", "debug"), ("core", "port"), ("core", "title")]
    assert info.fields[0].label == "调试"
    assert info.fields[0].default is False
    assert info.fields[1].default == 8000
    assert all(isinstance(Core.__dict__[f.path[-1]], _ConfigSlot) for f in info.fields)
    # ClassVar 不进 fields（类属性本身保留，只是不参与配置）
    assert "table" not in [f.path[-1] for f in info.fields]
    assert Core.table == {}


def test_field_info_has_default_property():
    f_default = FieldInfo(("a", "b"), int, 1)
    f_missing = FieldInfo(("a", "b"), int, MISSING)
    assert f_default.has_default is True
    assert f_missing.has_default is False


def test_configclass_rejects_field_shadowing_method():
    with pytest.raises(ValueError, match="shadows an existing method"):

        @configclass("core", "main")
        class Bad:
            debug: bool  # 只有注解，没有赋值

            def debug(self):  # noqa: ANN001, F811
                return 1


def test_configclass_failed_annotation_becomes_any():
    @configclass("core", "main")
    class Weird:
        value: "NoSuchType" = 1  # noqa: F821

    assert _CLASSES[Weird].fields[0].type is Any


def test_configclass_shares_same_file_and_path():
    """同名 section 多份声明：字段不冲突 → 并存共享，不互相顶掉。"""
    @configclass("core", "main")
    class First:
        a: int = 1

    @configclass("core", "main")
    class Second:
        b: int = 2

    assert First in _CLASSES
    assert Second in _CLASSES
    assert _CLASSES[Second].path == ("core",)
    assert classes_in("main") == (First, Second)
    from albuswall.configue.registry import _PATHS

    assert [ci.cls for ci in _PATHS[("main", ("core",))]] == [First, Second]


# ==================== 重装饰（#1） ====================

def test_redecoration_refreshes_slot_snapshot_in_place():
    class C:
        x: int = 1

    C = configclass("a", "f")(C)
    slot = C.__dict__["x"]
    assert isinstance(slot, _ConfigSlot)

    # 人为把快照弄脏（模拟「类被换了 namespace / 注解变了」）
    slot.path = ("stale",)
    slot.type = str

    configclass("a", "f")(C)

    assert C.__dict__["x"] is slot, "重装饰应原地更新，不新建描述符"
    assert slot.path == ("a", "x")
    assert slot.type is int


def test_redecoration_adopts_new_field_spec_default():
    class C:
        x: int = 1

    C = configclass("a", "f")(C)
    first_slot = C.__dict__["x"]
    assert first_slot.default == 1

    # 类级覆盖：把描述符换成新的 field() 声明（Python 允许，见 registry 契约）
    C.x = field(default=5)  # type: ignore[assignment]

    configclass("a", "f")(C)

    # 此时类属性已不是 slot（被 field() 覆盖），重装饰新建一个描述符——
    # 默认值必须是新声明的 5，而不是旧 slot 里的 1。
    new_slot = C.__dict__["x"]
    assert isinstance(new_slot, _ConfigSlot)
    assert new_slot.default == 5
    assert new_slot.type is int


def test_redecoration_keeps_slot_default_when_unavailable():
    """已在 slot 里的 default 是唯一来源，重装饰时原样保留（幂等）。"""
    class C:
        x: int = 1

    C = configclass("a", "f")(C)
    slot = C.__dict__["x"]
    slot.default = "adopted"

    configclass("a", "f")(C)
    assert slot.default == "adopted"


# ==================== classes_in（#3） ====================

def test_classes_in_sorted_by_order_descending():
    @configclass("low", "main", order=1)
    class Low:
        a: int = 1

    @configclass("high", "main", order=5)
    class High:
        b: int = 2

    @configclass("mid", "main", order=3)
    class Mid:
        c: int = 3

    @configclass("other", "other", order=99)
    class Other:
        d: int = 4

    assert classes_in("main") == (High, Mid, Low)
    assert classes_in("other") == (Other,)
    assert classes_in("nope") == ()


def test_classes_in_is_stable_for_equal_orders():
    @configclass("a", "main", order=0)
    class A:
        a: int = 1

    @configclass("b", "main", order=0)
    class B:
        b: int = 2

    assert classes_in("main") == (A, B)


def test_enforced_int_order_contract_is_passthrough():
    """``order`` 由调用方保证为 int：本层只透传，不做校验。"""

    @configclass("a", "main", order=2.5)  # type: ignore[arg-type]
    class A:
        a: int = 1

    assert isinstance(_CLASSES[A], ClassInfo)
    assert _CLASSES[A].order == 2.5


# ==================== optional 类型识别 ====================

def test_optional_annotation_is_recorded_verbatim():
    @configclass("a", "main")
    class A:
        x: Optional[int] = None

    assert _CLASSES[A].fields[0].type == Optional[int]
    assert _CLASSES[A].fields[0].default is None
