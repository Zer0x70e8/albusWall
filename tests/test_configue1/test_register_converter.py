#
"""``register_converter``：模块级 API + 构造前登记的待应用队列。

``register_converter`` 的两种时机：

- **构造后**：立刻登记到 ``Configue._instance.resolver``；
- **构造前**（插件导入期）：入队 ``_PENDING_CONVERTERS``，``load()`` 时一次性
  应用并清空——否则导入期的登记只能丢。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from albuswall.configue1 import (
    _PENDING_CONVERTERS,
    ConfigFile,
    Configue,
    configclass,
    reg,
    register_converter,
)
from albuswall.configue1.registry import _FILES

from .conftest import write_ini


class Colour:
    """构造函数需要两个参数 → ``_convert`` 的默认回退（``Colour(raw)``）会失败。"""

    def __init__(self, name: str, code: int) -> None:
        self.name = name
        self.code = code

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, Colour)
            and other.name == self.name
            and other.code == self.code
        )

    def __repr__(self) -> str:
        return f"Colour({self.name!r}, {self.code})"


def _reader(raw: str) -> Colour:
    return Colour(raw, len(raw))


def _writer(value: Colour) -> str:
    return value.name


@pytest.fixture
def ini(tmp_path: Path) -> Path:
    write_ini(tmp_path / "config.ini", "[ui]\naccent = red\n")
    reg(ConfigFile.INI, "config.ini", name="main", autosave="off")
    return tmp_path


# ==================== 构造后 ====================

def test_register_after_construction_applies_immediately(ini):
    @configclass("ui", "main")
    class UI:
        accent: Colour = Colour("default", 0)

    c = Configue(ini)
    register_converter(Colour, _reader, writer=_writer)

    assert c.resolver.custom_converters()[Colour] is _reader
    assert c.resolver.custom_serializers()[Colour] is _writer
    assert _PENDING_CONVERTERS == []

    c.load()
    assert UI().accent == Colour("red", 3)


def test_register_writer_optional(ini):
    @configclass("ui", "main")
    class UI:
        accent: Colour = Colour("default", 0)

    c = Configue(ini)
    register_converter(Colour, _reader)

    assert Colour in c.resolver.custom_converters()
    assert Colour not in c.resolver.custom_serializers()
    c.load()
    assert UI().accent == Colour("red", 3)


# ==================== 构造前（待应用队列） ====================

def test_register_before_construction_is_queued(ini):
    register_converter(Colour, _reader, writer=_writer)

    assert Configue._instance is None
    assert len(_PENDING_CONVERTERS) == 1


def test_load_applies_and_clears_pending_queue(ini):
    @configclass("ui", "main")
    class UI:
        accent: Colour = Colour("default", 0)

    register_converter(Colour, _reader, writer=_writer)
    c = Configue(ini)

    c.load()

    assert _PENDING_CONVERTERS == []
    assert c.resolver.custom_converters()[Colour] is _reader
    assert UI().accent == Colour("red", 3)


def test_pending_queue_applied_before_type_check(ini):
    """``check_all`` 在 Step 5，队列必须在那之前生效，否则 red 会转换失败。"""
    @configclass("ui", "main")
    class UI:
        accent: Colour = Colour("default", 0)

    register_converter(Colour, _reader)
    c = Configue(ini)

    report = c.load()   # 不抛就说明转换器已生效

    assert report.ok is True
    assert UI().accent == Colour("red", 3)


def test_pending_queue_cleared_after_first_load(ini):
    register_converter(Colour, _reader)
    c = Configue(ini)
    c.load()
    assert _PENDING_CONVERTERS == []

    # 第二次 load 不会重复应用（幂等）
    c.load()
    assert _PENDING_CONVERTERS == []


# ==================== 写回 ====================

def test_custom_writer_used_by_flush(ini):
    @configclass("ui", "main")
    class UI:
        accent: Colour = Colour("default", 0)

    register_converter(Colour, _reader, writer=_writer)
    c = Configue(ini)
    c.load()

    UI().accent = Colour("blue", 4)
    c.flush()

    text = (ini / "config.ini").read_text(encoding="utf-8")
    assert "accent = blue" in text
    assert "4" not in text.split("accent = ")[1].splitlines()[0]


def test_reader_failure_becomes_load_error(ini):
    @configclass("ui", "main")
    class UI:
        accent: Colour = Colour("default", 0)

    register_converter(Colour, lambda raw: (_ for _ in ()).throw(ValueError("nope")))
    c = Configue(ini)

    with pytest.raises(Exception) as ei:
        c.load()
    assert "type check failed" in str(ei.value)


# ==================== 同名类型覆盖 ====================

def test_register_twice_last_wins(ini):
    @configclass("ui", "main")
    class UI:
        accent: Colour = Colour("default", 0)

    c = Configue(ini)
    register_converter(Colour, _reader)
    register_converter(Colour, lambda raw: Colour(raw.upper(), 0))

    c.load()

    assert UI().accent == Colour("RED", 0)
    assert _FILES["main"].loaded is True
