#
"""``Literal[...]`` 的读写收尾：``_convert`` / ``_deconvert`` / coerce / 赋值校验。

背景：``schema._choices_for`` 早就能从 ``Literal`` 反射出 ``choices``（UI 可以
直接显示 combo），但转换层对 ``Literal`` 一律 ``TypeError("Unsupported generic
type")``——「能显示、一读就炸」。本文件把四个出口一起钉住：

* ``utils.ini_parser._convert`` / ``_deconvert``：INI 文本 ↔ 成员值；
* ``resolver`` 的 ``coerce_value``（``_coerce_native`` / ``_coerce_scalar``）：
  JSON 后端 / CLI 传来的原生值；
* ``registry._type_matches``：类视图赋值（``Config().field = value``）。
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Literal

import pytest

from albuswall.configue import ConfigFile, Configue, configclass, reg
from albuswall.configue.registry import _type_matches
from albuswall.configue.resolver import coerce_value
from albuswall.configue.utils.ini_parser import _convert, _deconvert


class Theme(Enum):
    DARK = "dark"
    LIGHT = "light"


# ══════════════════════════════════════════════════════════════
# _convert / _deconvert
# ══════════════════════════════════════════════════════════════

def test_convert_literal_str_hit():
    assert _convert("a", Literal["a", "b"]) == "a"


def test_convert_literal_str_strip():
    assert _convert("  b  ", Literal["a", "b"]) == "b"


def test_convert_literal_str_miss_raises():
    with pytest.raises(ValueError, match=r"'a'|Literal"):
        _convert("c", Literal["a", "b"])


def test_convert_literal_str_is_case_sensitive():
    with pytest.raises(ValueError):
        _convert("A", Literal["a", "b"])


def test_convert_literal_int_member():
    assert _convert("1", Literal[1, 2]) == 1
    assert isinstance(_convert("1", Literal[1, 2]), int)


def test_convert_literal_int_member_miss():
    with pytest.raises(ValueError):
        _convert("3", Literal[1, 2])


def test_convert_literal_enum_member_by_value():
    assert _convert("dark", Literal[Theme.DARK, Theme.LIGHT]) is Theme.DARK


def test_convert_literal_bool_member():
    assert _convert("true", Literal[True, False]) is True


def test_convert_literal_none_member():
    assert _convert("", Literal[None, "x"]) is None


def test_deconvert_literal_roundtrip_str():
    text = _deconvert("a", Literal["a", "b"])
    assert text == "a"
    assert _convert(text, Literal["a", "b"]) == "a"


def test_deconvert_literal_roundtrip_enum_writes_value():
    """Enum 成员写 **value**（写成员名会读不回来）。"""
    text = _deconvert(Theme.LIGHT, Literal[Theme.DARK, Theme.LIGHT])
    assert text == "light"
    assert _convert(text, Literal[Theme.DARK, Theme.LIGHT]) is Theme.LIGHT


def test_deconvert_literal_roundtrip_int():
    assert _convert(_deconvert(2, Literal[1, 2]), Literal[1, 2]) == 2


# ══════════════════════════════════════════════════════════════
# coerce_value（JSON 后端 / CLI 原生值）
# ══════════════════════════════════════════════════════════════

def test_coerce_value_literal_native_hit():
    assert coerce_value("a", Literal["a", "b"]) == "a"
    assert coerce_value("b", Literal["a", "b"]) == "b"


def test_coerce_value_literal_native_miss():
    with pytest.raises(ValueError):
        coerce_value("c", Literal["a", "b"])


def test_coerce_value_literal_native_non_string():
    # JSON 后端给的是原生值：合法成员原样通过，非法成员必须报错（不 fail-open）
    assert coerce_value(2, Literal[1, 2]) == 2
    with pytest.raises(ValueError):
        coerce_value(3, Literal[1, 2])


def test_coerce_value_list_of_literal_checks_elements():
    assert coerce_value(["a", "b"], list[Literal["a", "b"]]) == ["a", "b"]
    assert coerce_value(["a"], list[Literal["a", "b"]]) == ["a"]
    with pytest.raises(ValueError):
        coerce_value(["a", "c"], list[Literal["a", "b"]])


# ══════════════════════════════════════════════════════════════
# _type_matches（类视图赋值）
# ══════════════════════════════════════════════════════════════

def test_type_matches_literal():
    assert _type_matches("a", Literal["a", "b"]) is True
    assert _type_matches("c", Literal["a", "b"]) is False
    assert _type_matches(1, Literal[1, 2]) is True
    assert _type_matches(3, Literal[1, 2]) is False
    assert _type_matches(Theme.DARK, Literal[Theme.DARK]) is True


# ══════════════════════════════════════════════════════════════
# 端到端：INI 读写 + 类视图赋值
# ══════════════════════════════════════════════════════════════

@pytest.fixture
def literal_cfg(tmp_path: Path):
    (tmp_path / "a.ini").write_text(
        "[demo]\nmode = fast\n",
        encoding="utf-8",
    )
    reg(ConfigFile.INI, "a.ini", name="a", order=10)

    @configclass("demo", "a")
    class Demo:
        mode: Literal["fast", "slow"] = "fast"

    cfg = Configue(base_dir=tmp_path)
    cfg.load()
    return cfg, Demo


def test_ini_literal_reads(literal_cfg):
    _cfg, Demo = literal_cfg
    assert Demo().mode == "fast"


def test_class_view_accepts_member(literal_cfg):
    _cfg, Demo = literal_cfg
    Demo().mode = "slow"
    assert Demo().mode == "slow"


def test_class_view_rejects_non_member(literal_cfg):
    _cfg, Demo = literal_cfg
    with pytest.raises(TypeError):
        Demo().mode = "c"
    assert Demo().mode == "fast"
