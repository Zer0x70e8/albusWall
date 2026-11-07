#
"""同名 section 的多份 ``@configclass`` 声明：等价就**共享**，不等价就**报错**。

设计目标（见 ``configue/registry.py`` 模块 docstring 的「声明共享」）：

- 多个类可以声明同一个 ``(section, key)``，它们读写的是**同一个 Resolver 节点**；
- 字段集合可以不同（各声明各的，合并成一个 section 的字段集）；
- 声明不等价（类型 / 默认值不同）→ **装饰期**抛 ``ConfigDeclarationConflict``，
  而不是让后写者静默赢；
- 重复装饰同一个类是幂等的（刷新自己，不与自己的旧声明冲突）。
"""

from __future__ import annotations

import pytest

from albuswall.configue import (
    ConfigDeclarationConflict,
    ConfigFile,
    Configue,
    configclass,
    field,
    reg,
)
from albuswall.configue.registry import _CLASSES, _FIELD_OWNER, _PATHS, classes_in

from .conftest import write_ini


# ==================== 共享：不同字段 → 合并 ====================

def test_split_field_declarations_merge_into_one_section(tmp_path):
    """同一 section 拆成两个类各声明部分字段：两份都生效，都能读写。"""
    reg(ConfigFile.INI, "config.ini", name="main", autosave="off", optional=True)

    @configclass("core", "main")
    class AlphaPart:
        alpha: int = field(default=1)

    @configclass("core", "main")
    class BetaPart:
        beta: str = field(default="x")

    c = Configue(tmp_path)
    c.load()

    assert AlphaPart in _CLASSES and BetaPart in _CLASSES
    assert classes_in("main") == (AlphaPart, BetaPart)

    # 两边的默认值都注入了
    assert AlphaPart().alpha == 1
    assert BetaPart().beta == "x"
    assert set(c.resolver.nodes()) == {("core", "alpha"), ("core", "beta")}

    # 一个类只看到自己声明的字段（字段集合不互相污染）
    assert [f.path for f in _CLASSES[AlphaPart].fields] == [("core", "alpha")]
    assert [f.path for f in _CLASSES[BetaPart].fields] == [("core", "beta")]


def test_identical_declaration_shares_one_value(tmp_path):
    """同一个配置项被两处声明（声明等价）：读到同一份值，写入互相可见。"""
    write_ini(tmp_path / "config.ini", "[core]\ndebug = true\n")
    reg(ConfigFile.INI, "config.ini", name="main", autosave="off")

    @configclass("core", "main")
    class Writer:
        debug: bool = False

    @configclass("core", "main")
    class ReaderView:
        debug: bool = False

    c = Configue(tmp_path)
    c.load()

    assert Writer().debug is True
    assert ReaderView().debug is True          # 同一个节点，不是两份拷贝

    Writer().debug = False
    assert ReaderView().debug is False         # 写入立刻对另一处可见
    assert ("core", "debug") in c.resolver.dirty_nodes()


def test_shared_declaration_across_files(tmp_path):
    """跨文件声明同一节点（声明等价）：值共享，写回归生效 origin。"""
    write_ini(tmp_path / "config.ini", "[core]\ndebug = true\n")
    reg(ConfigFile.INI, "config.ini", name="main", order=0, autosave="off")
    reg(ConfigFile.INI, "machine.ini", name="machine", order=1, autosave="off",
        optional=True)

    @configclass("core", "main")
    class FromMain:
        debug: bool = False

    @configclass("core", "machine")
    class FromMachine:
        debug: bool = False

    c = Configue(tmp_path)
    c.load()

    assert FromMain().debug is True
    assert FromMachine().debug is True
    assert c.resolver.origin_of("core", "debug") == (
        tmp_path / "config.ini"
    ).resolve()


def test_label_and_description_do_not_conflict(tmp_path):
    """``label`` / ``description`` 是展示信息，不参与等价判定。"""
    @configclass("core", "main")
    class A:
        debug: bool = field(default=False, label="调试", description="打开调试日志")

    @configclass("core", "main")
    class B:
        debug: bool = False

    assert _CLASSES[A].fields[0].label == "调试"
    assert _CLASSES[B].fields[0].label == ""


# ==================== 冲突：类型 / 默认值不同 → 装饰期报错 ====================

def test_conflicting_type_raises_at_decoration():
    @configclass("core", "main")
    class IntPort:
        port: int = 8000

    with pytest.raises(ConfigDeclarationConflict, match=r"type .* != .*"):

        @configclass("core", "main")
        class StrPort:
            port: str = "8000"


def test_conflicting_default_raises_at_decoration():
    @configclass("core", "main")
    class Off:
        debug: bool = False

    with pytest.raises(ConfigDeclarationConflict, match="default False != True"):

        @configclass("core", "main")
        class On:
            debug: bool = True


def test_conflict_message_names_both_declarers():
    @configclass("core", "main")
    class First:
        port: int = 8000

    with pytest.raises(ConfigDeclarationConflict) as info:
        @configclass("core", "main")
        class Second:
            port: str = "8000"

    message = str(info.value)
    assert "core/port" in message
    assert "First (main): int = 8000" in message
    assert "Second (main): str = '8000'" in message


def test_conflict_leaves_registry_untouched():
    """冲突时不登记半个类：登记表保持调用前的样子。"""
    @configclass("core", "main")
    class Good:
        port: int = 8000

    before = dict(_CLASSES)
    with pytest.raises(ConfigDeclarationConflict):
        @configclass("core", "main")
        class Bad:
            port: str = "8000"

    assert _CLASSES == before
    assert _FIELD_OWNER[("core", "port")] == "main"
    assert [ci.cls for ci in _PATHS[("main", ("core",))]] == [Good]


def test_default_versus_no_default_is_a_conflict():
    """一处写了默认值、一处没写 → 不等价（同一个值两种来源，选择是隐式的）。"""
    @configclass("core", "main")
    class WithDefault:
        port: int = 8000

    with pytest.raises(ConfigDeclarationConflict, match="default"):

        @configclass("core", "main")
        class WithoutDefault:
            port: int


# ==================== 重复装饰同一个类：幂等，不与自己冲突 ====================

def test_redecorating_same_class_updates_in_place():
    class C:
        x: int = 1

    C = configclass("a", "f")(C)
    assert _CLASSES[C].fields[0].default == 1

    # 改默认值后重装饰：自己的旧声明不参与冲突比较
    C.x = field(default=5)  # type: ignore[assignment]
    configclass("a", "f")(C)

    assert _CLASSES[C].fields[0].default == 5
    assert [ci.cls for ci in _PATHS[("f", ("a",))]] == [C]   # 只有一条，不重复


def test_redecorating_same_class_to_another_file_moves_it():
    class C:
        x: int = 1

    C = configclass("a", "first")(C)
    assert _PATHS[("first", ("a",))][0].cls is C

    configclass("a", "second")(C)

    assert ("first", ("a",)) not in _PATHS          # 旧 bucket 已清理
    assert [ci.cls for ci in _PATHS[("second", ("a",))]] == [C]
    assert _CLASSES[C].file == "second"
    assert _FIELD_OWNER[("a", "x")] == "second"


# ==================== 与 load()/flush() 的协同 ====================

def test_shared_sections_survive_load_and_flush(tmp_path):
    """两份共享声明在真实 load/flush 流程里都正常工作。"""
    write_ini(tmp_path / "config.ini", "[core]\ndebug = true\n")
    reg(ConfigFile.INI, "config.ini", name="main", autosave="off")

    @configclass("core", "main")
    class Top:
        debug: bool = False

    @configclass("core", "main")
    class Extra:
        debug: bool = False
        recent: int = 0

    c = Configue(tmp_path)
    report = c.load()
    assert report.ok

    assert Top().debug is True                  # 文件值
    assert Extra().debug is True
    assert Extra().recent == 0                  # 另一份声明的默认值

    Top().debug = False
    written = c.flush()
    assert written == [(tmp_path / "config.ini").resolve()]
    assert "debug = false" in (tmp_path / "config.ini").read_text(encoding="utf-8")


# ==================== 冲突：validators 不同 → 装饰期报错 ====================

def _range_1_100(value: int) -> None:
    if not 1 <= value <= 100:
        raise ValueError("1..100")


def _range_1_10(value: int) -> None:
    if not 1 <= value <= 10:
        raise ValueError("1..10")


def test_conflicting_validators_raise_at_decoration():
    """两个类对同一节点给出不同校验器 → 装饰期冲突（不静默丢一份）。"""
    @configclass("core", "main")
    class Wide:
        port: int = field(default=50, validators=[_range_1_100])

    with pytest.raises(ConfigDeclarationConflict, match="validators"):

        @configclass("core", "main")
        class Narrow:
            port: int = field(default=50, validators=[_range_1_10])


def test_conflicting_validators_message_lists_both():
    @configclass("core", "main")
    class Wide:
        port: int = field(default=50, validators=[_range_1_100])

    with pytest.raises(ConfigDeclarationConflict) as info:
        @configclass("core", "main")
        class Narrow:
            port: int = field(default=50, validators=[_range_1_10])

    message = str(info.value)
    assert "core/port" in message
    assert "validators" in message
    assert "_range_1_10" in message and "_range_1_100" in message
    # 与 type / default 冲突一样，两个声明者都要点名
    assert "Wide (main)" in message and "Narrow (main)" in message


def test_same_validator_object_does_not_conflict():
    """同一校验器对象 / ``==`` 相等的校验器 → 视为等价，可以共享。"""
    @configclass("core", "main")
    class First:
        port: int = field(default=50, validators=[_range_1_100])

    @configclass("core", "main")
    class Second:
        port: int = field(default=50, validators=[_range_1_100])

    assert _CLASSES[First].fields[0].validators == (
        _CLASSES[Second].fields[0].validators
    )


class _Range:
    """可比较的校验器对象（``==`` 相等即等价）——共享声明的推荐写法。"""

    def __init__(self, low: int, high: int) -> None:
        self.low = low
        self.high = high

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, _Range):
            return NotImplemented
        return (self.low, self.high) == (other.low, other.high)

    def __hash__(self) -> int:
        return hash((self.low, self.high))

    def __call__(self, value: int) -> None:
        if not self.low <= value <= self.high:
            raise ValueError(f"{self.low}..{self.high}")

    def __repr__(self) -> str:
        return f"Range({self.low}, {self.high})"


def test_equal_but_distinct_validator_objects_do_not_conflict():
    """``==`` 相等的两个校验器实例可以共享声明（不比 identity）。"""
    @configclass("core", "main")
    class A:
        port: int = field(default=50, validators=[_Range(1, 100)])

    @configclass("core", "main")
    class B:
        port: int = field(default=50, validators=[_Range(1, 100)])

    assert _CLASSES[A].fields[0].validators[0] is not \
        _CLASSES[B].fields[0].validators[0]


def test_lambda_validators_never_compare_equal():
    """两个 lambda 永不相等是**预期行为**：同一节点用两份 lambda 声明即冲突。"""
    @configclass("core", "main")
    class A:
        port: int = field(
            default=50, validators=[lambda v: v > 0],
        )

    with pytest.raises(ConfigDeclarationConflict, match="validators"):

        @configclass("core", "main")
        class B:
            port: int = field(
                default=50, validators=[lambda v: v > 0],
            )


def test_both_empty_validators_do_not_conflict():
    @configclass("core", "main")
    class Plain:
        port: int = 50

    @configclass("core", "main")
    class AlsoPlain:
        port: int = 50


def test_empty_versus_present_validators_is_a_conflict():
    """一处声明了校验器、一处没有 → 不等价（同一个格子两种规则）。"""
    @configclass("core", "main")
    class Checked:
        port: int = field(default=50, validators=[_range_1_100])

    with pytest.raises(ConfigDeclarationConflict, match="validators"):

        @configclass("core", "main")
        class Unchecked:
            port: int = 50


def test_validator_count_mismatch_is_a_conflict():
    @configclass("core", "main")
    class One:
        port: int = field(default=50, validators=[_range_1_100])

    with pytest.raises(ConfigDeclarationConflict, match="validators"):

        @configclass("core", "main")
        class Two:
            port: int = field(
                default=50, validators=[_range_1_100, _range_1_10],
            )


def test_conflicting_validators_leave_registry_untouched():
    @configclass("core", "main")
    class Wide:
        port: int = field(default=50, validators=[_range_1_100])

    before = dict(_CLASSES)
    with pytest.raises(ConfigDeclarationConflict):
        @configclass("core", "main")
        class Narrow:
            port: int = field(default=50, validators=[_range_1_10])

    assert _CLASSES == before
    assert _FIELD_OWNER[("core", "port")] == "main"


def test_redecorating_same_class_may_change_validators():
    """重复装饰同一个类允许改 validators（刷新自己，不与自己冲突）。"""
    class C:
        port: int = field(default=50, validators=[_range_1_100])

    configclass("core", "main")(C)
    assert _CLASSES[C].fields[0].validators == (_range_1_100,)

    C.port = field(default=50, validators=[_range_1_10])
    configclass("core", "main")(C)
    assert _CLASSES[C].fields[0].validators == (_range_1_10,)
