#
"""字段级校验层骨架：``field(validators=[...])`` + ``ConfigValidationError``。

设计固定（不要重新设计）：

* 校验器签名 ``Callable[[Any], None]``——**抛异常 = 非法，不抛 = 合法**；
* 校验发生在 **coerce 之后**：类型不对仍抛 ``ValueError``，类型对了但语义
  不合法抛 ``ConfigValidationError``（两类错误刻意分开，UI 才能分头提示）；
* 默认（``collect_errors=False`` 且非 ``dry_run``）：第一个失败即整体失败，
  内存与磁盘都不动；
* ``collect_errors=True``：收集所有字段的错误，整体仍不写；
* ``dry_run=True``：校验器照跑，错误只收集不抛。

本层**不内置**任何具体校验器（Range / Pattern / Length 都没有）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from albuswall.configue import (
    ConfigError,
    ConfigFile,
    Configue,
    configclass,
    field,
    reg,
)
from albuswall.configue.errors import ConfigValidationError
from albuswall.configue.schema import (
    _MANUAL_SCHEMA,
    invalidate_schema,
    register_setting_item,
    save_group,
    save_values,
    setting_item,
)


def _fail(message: str):
    """构造一个必定抛 ``ValueError(message)`` 的校验器。"""

    def _validator(_value: Any) -> None:
        raise ValueError(message)

    return _validator


def _port_range(value: int) -> None:
    if not 1 <= value <= 65535:
        raise ValueError("端口必须在 1-65535 之间")


def _verbose(value: bool) -> None:
    if value:
        raise ValueError("调试开关目前不允许打开")


@pytest.fixture(autouse=True)
def _isolated_manual_schema():
    _MANUAL_SCHEMA.clear()
    invalidate_schema()
    try:
        yield
    finally:
        _MANUAL_SCHEMA.clear()
        invalidate_schema()


@pytest.fixture
def demo(tmp_path: Path):
    (tmp_path / "a.ini").write_text(
        "[demo]\nport = 8000\nverbose = false\n",
        encoding="utf-8",
    )
    reg(ConfigFile.INI, "a.ini", name="a", order=10)

    @configclass("demo", "a")
    class Demo:
        port: int = field(default=8000, validators=[_port_range])
        verbose: bool = field(default=False, validators=[_verbose])

    cfg = Configue(base_dir=tmp_path)
    cfg.load()
    return cfg


def _read(cfg: Configue) -> str:
    return (cfg.base_dir / "a.ini").read_text(encoding="utf-8")


# ══════════════════════════════════════════════════════════════
# 错误类型：与 ValueError 分开
# ══════════════════════════════════════════════════════════════

def test_validation_error_is_config_error_but_not_value_error():
    assert issubclass(ConfigValidationError, ConfigError)
    assert not issubclass(ConfigValidationError, ValueError)


def test_validation_error_accepts_callable_message():
    exc = ConfigValidationError(lambda: "端口不合法", path="[demo]port")
    assert str(exc) == "端口不合法"
    assert exc.path == "[demo]port"


def test_validation_error_callable_failure_does_not_raise():
    def boom() -> str:
        raise RuntimeError("i18n 没准备好")

    exc = ConfigValidationError(boom)
    assert str(exc) == "validation failed"


# ══════════════════════════════════════════════════════════════
# 默认语义：第一个失败即整体失败
# ══════════════════════════════════════════════════════════════

def test_validator_failure_raises_and_touches_nothing(demo):
    before_text = _read(demo)
    before_dirty = demo.dirty_nodes()

    with pytest.raises(ConfigValidationError, match=r"\[demo\]port"):
        save_values({"demo.port": 99999}, config=demo)

    assert _read(demo) == before_text
    assert demo.resolver.get_typed("demo", "port") == 8000
    assert demo.dirty_nodes() == before_dirty


def test_validator_message_is_included(demo):
    with pytest.raises(ConfigValidationError) as info:
        save_values({"demo.port": 0}, config=demo)
    message = str(info.value)
    assert "[demo]port" in message
    assert "1-65535" in message


def test_valid_value_passes(demo):
    result = save_values({"demo.port": 8080}, config=demo)
    assert result.applied == ("demo.port",)
    assert result.validation_errors == ()
    assert demo.resolver.get_typed("demo", "port") == 8080
    assert "port = 8080" in _read(demo)


def test_changed_value_equal_to_old_still_validated(demo):
    """值没变也要校验——UI 提交整张表单时不能因为「没改」就放过。"""
    demo.resolver.set("demo", "port", 99999, persist=False)
    demo.resolver.mark_clean([("demo", "port")])
    with pytest.raises(ConfigValidationError):
        save_values({"demo.port": 99999}, config=demo)


def test_coerce_failure_still_raises_value_error(demo):
    """类型转不过去 → ``ValueError``（不是 ``ConfigValidationError``）。"""
    with pytest.raises(ValueError) as info:
        save_values({"demo.port": "abc"}, config=demo)
    assert not isinstance(info.value, ConfigValidationError)


def test_validator_receives_coerced_value(demo):
    """校验器拿到的是 coerce **之后**的值（``"8080"`` → ``8080``）。"""
    seen: list[Any] = []

    def _record(value: Any) -> None:
        seen.append(value)

    register_setting_item("demo", setting_item("port", validators=[_record]))
    save_values({"demo.port": "8080"}, config=demo)
    assert seen == [8080]
    assert demo.resolver.get_typed("demo", "port") == 8080


# ══════════════════════════════════════════════════════════════
# collect_errors=True：收集全部错误，整体不写
# ══════════════════════════════════════════════════════════════

def test_collect_errors_gathers_all_and_writes_nothing(demo):
    before_text = _read(demo)
    before_dirty = demo.dirty_nodes()

    result = save_values(
        {"demo.port": 99999, "demo.verbose": True},
        config=demo,
        collect_errors=True,
    )

    assert result.written == ()
    assert result.flushed == ()
    assert result.applied == ()
    assert len(result.validation_errors) == 2
    paths = {error["path"] for error in result.validation_errors}
    assert paths == {"[demo]port", "[demo]verbose"}
    assert {error["key"] for error in result.validation_errors} == {
        "demo.port", "demo.verbose",
    }
    assert all(isinstance(error["message"], str) for error in result.validation_errors)

    assert _read(demo) == before_text
    assert demo.resolver.get_typed("demo", "port") == 8000
    assert demo.resolver.get_typed("demo", "verbose") is False
    assert demo.dirty_nodes() == before_dirty


def test_collect_errors_partial_failure_blocks_all(demo):
    """一个合法、一个非法：合法那个也不写（不留半截修改）。"""
    before_text = _read(demo)
    result = save_values(
        {"demo.port": 8080, "demo.verbose": True},
        config=demo,
        collect_errors=True,
    )
    assert result.applied == ()
    assert [error["path"] for error in result.validation_errors] == [
        "[demo]verbose",
    ]
    assert _read(demo) == before_text
    assert demo.resolver.get_typed("demo", "port") == 8000


def test_collect_errors_no_error_writes_normally(demo):
    result = save_values(
        {"demo.port": 8080},
        config=demo,
        collect_errors=True,
    )
    assert result.validation_errors == ()
    assert result.applied == ("demo.port",)
    assert "port = 8080" in _read(demo)


def test_to_dict_exposes_validation_errors(demo):
    result = save_values(
        {"demo.port": 99999}, config=demo, collect_errors=True,
    )
    payload = result.to_dict()
    assert payload["validation_errors"][0]["path"] == "[demo]port"
    assert payload["written"] == []


# ══════════════════════════════════════════════════════════════
# dry_run：跑校验，但不抛
# ══════════════════════════════════════════════════════════════

def test_dry_run_collects_validation_errors_without_raising(demo):
    result = save_values(
        {"demo.port": 99999}, config=demo, dry_run=True,
    )
    assert result.dry_run is True
    assert result.written == ()
    assert result.applied == ()
    assert [error["path"] for error in result.validation_errors] == ["[demo]port"]
    assert demo.resolver.get_typed("demo", "port") == 8000
    assert demo.dirty_nodes() == frozenset()


def test_dry_run_valid_value_has_no_errors(demo):
    result = save_values({"demo.port": 8080}, config=demo, dry_run=True)
    assert result.validation_errors == ()
    assert result.changes[0]["new"] == 8080
    assert demo.resolver.get_typed("demo", "port") == 8000


# ══════════════════════════════════════════════════════════════
# save_group / 手写覆盖 / 声明期
# ══════════════════════════════════════════════════════════════

def test_save_group_collect_errors(demo):
    result = save_group(
        "demo", {"port": 0}, config=demo, collect_errors=True,
    )
    assert result.validation_errors
    assert result.applied == ()


def test_manual_override_supplies_validators(demo):
    """手写 ``setting_item(validators=...)`` 非空即覆盖声明期校验器。"""
    register_setting_item(
        "demo",
        setting_item("port", validators=[_fail("手写校验：不许改端口")]),
    )
    with pytest.raises(ConfigValidationError, match="手写校验"):
        save_values({"demo.port": 8080}, config=demo)


def test_manual_override_keeps_declared_validators_when_empty(demo):
    register_setting_item("demo", setting_item("port", label="端口"))
    with pytest.raises(ConfigValidationError, match="1-65535"):
        save_values({"demo.port": 0}, config=demo)


def test_field_without_validators_is_untouched(tmp_path: Path):
    (tmp_path / "b.ini").write_text("[plain]\nlevel = 1\n", encoding="utf-8")
    reg(ConfigFile.INI, "b.ini", name="b", order=10)

    @configclass("plain", "b")
    class Plain:
        level: int = 1

    cfg = Configue(base_dir=tmp_path)
    cfg.load()
    result = save_values({"plain.level": 5}, config=cfg)
    assert result.validation_errors == ()
    assert result.applied == ("plain.level",)


def test_validators_are_stored_on_declaration():
    from albuswall.configue.registry import declarations

    @configclass("v", "v")
    class V:
        port: int = field(default=1, validators=[_port_range])

    declared = [
        f for info in declarations() for f in info.fields if f.path[-1] == "port"
    ]
    assert declared[0].validators == (_port_range,)
    assert declared[0].default == 1


def test_configclass_rejects_non_callable_validator():
    with pytest.raises(TypeError, match="callable"):

        @configclass("bad", "bad")
        class Bad:
            port: int = field(default=1, validators=[42])


def test_lambda_validator_from_field_declaration(tmp_path: Path):
    """prompt 里的最小写法：``field(validators=[lambda ...])`` 一路走通。"""
    (tmp_path / "l.ini").write_text("[l]\nlevel = 1\n", encoding="utf-8")
    reg(ConfigFile.INI, "l.ini", name="l", order=10)

    @configclass("l", "l")
    class L:
        level: int = field(
            default=1,
            validators=[lambda v: (_ for _ in ()).throw(ValueError("bad"))],
        )

    cfg = Configue(base_dir=tmp_path)
    cfg.load()
    with pytest.raises(ConfigValidationError, match=r"\[l\]level"):
        save_values({"l.level": 2}, config=cfg)


def test_validator_accumulates_callable_message(tmp_path: Path):
    (tmp_path / "c.ini").write_text("[c]\nlevel = 1\n", encoding="utf-8")
    reg(ConfigFile.INI, "c.ini", name="c", order=10)

    def _translated() -> str:
        return "会被求值的消息"

    @configclass("c", "c")
    class C:
        level: int = field(
            default=1,
            validators=[lambda _v: (_ for _ in ()).throw(ValueError(_translated))],
        )

    cfg = Configue(base_dir=tmp_path)
    cfg.load()
    with pytest.raises(ConfigValidationError, match="会被求值的消息"):
        save_values({"c.level": 2}, config=cfg)
