#
"""``handlers/_params.py``：Param 工厂 + 入参收口。

守的是「声明期就把坑堵住」：

* 可选位置参数漏写 ``default`` —— ``_bind`` 会填 ``None``，随后在 handler
  第一行 ``AttributeError``（对外是 ``CommandFailed``，不是"参数缺失"）；
* ``flag`` 的 ``type`` 只能是 ``bool``；
* ``Param.type`` 不参与运行时转换，强类型靠 ``as_*`` 收口（失败是
  ``BadRequest``，退出码 ``USAGE_ERROR``）。

另外验证工厂产出的 ``Param`` 能真的被 ``Dispatcher._bind`` 绑定 —— 否则
"声明对了但绑不上"同样会在运行时炸。
"""

from __future__ import annotations

import warnings

import pytest

from albuswall.command import Command, CommandRegistry
from albuswall.command.exceptions import BadRequest
from albuswall.service.command.dispatcher import Dispatcher
from albuswall.service.command.handlers._params import (
    as_bool, as_choice, as_int, as_uuid,
    flag, limit_params, opt, paging_params, pos,
)


# ==================== Param 工厂 ====================

def test_pos_requires_explicit_default_when_optional():
    """可选位置参数必须显式给 default（这正是历史坑的样子）。"""
    with pytest.raises(ValueError, match="必须显式给 default"):
        pos("subcmd", required=False)

    param = pos("subcmd", required=False, default="")
    assert (param.kind, param.required, param.default) == ("pos", False, "")


def test_pos_rejects_required_with_default():
    with pytest.raises(ValueError, match="不能带 default"):
        pos("name", required=True, default="x")


def test_opt_and_flag_shapes():
    option = opt("limit", type=int, default=20, short="n", help="数量")
    assert (option.name, option.kind, option.type, option.default) == (
        "limit", "opt", int, 20)
    assert option.short == "n"

    boolean = flag("force", short="f")
    assert (boolean.kind, boolean.type, boolean.default) == (
        "flag", bool, False)

    with pytest.raises(ValueError, match="required 选项不能带 default"):
        opt("x", required=True, default=1)


def test_choices_are_normalised_to_tuple():
    option = opt("mode", choices=["a", "b"])
    assert option.choices == ("a", "b")


# ==================== 与 Dispatcher._bind 的契约 ====================

def test_generated_params_bind_without_warnings_and_coerce_flags():
    """工厂产出的声明必须能绑定，且不发「positional param」警告。"""
    captured: dict = {}

    def _handler(subcmd: str, limit: int, force: bool) -> dict:
        captured.update(subcmd=subcmd, limit=limit, force=force)
        return captured

    registry = CommandRegistry()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        registry.register(Command(
            path="demo.run",
            func=_handler,
            params=(
                pos("subcmd", required=False, default=""),
                opt("limit", type=int, default=20),
                flag("force"),
            ),
        ))
    assert [str(w.message) for w in caught
            if "positional param" in str(w.message)] == []

    result = Dispatcher(registry).dispatch(
        "demo.run", [], {"limit": 5, "force": "false"},
    )
    # flag 由 _bind 归一（"false" → False），option 原样交给 handler
    assert result == {"subcmd": "", "limit": 5, "force": False}

    result = Dispatcher(registry).dispatch("demo.run", ["scan"], {"force": "1"})
    assert result == {"subcmd": "scan", "limit": 20, "force": True}


# ==================== 入参收口 ====================

def test_as_uuid_accepts_uuid_like_and_rejects_garbage():
    value = "3F2504E0-4F89-11D3-9A0C-0305E82C3301"
    assert str(as_uuid(value)) == value.lower()

    with pytest.raises(BadRequest, match="asset_uuid"):
        as_uuid("not-a-uuid", field="asset_uuid")
    with pytest.raises(BadRequest, match="missing uuid"):
        as_uuid("", field="asset_uuid")


def test_as_bool_accepts_loose_forms():
    for value in (True, 1, 1.0, "true", "TRUE", "yes", "on", "1"):
        assert as_bool(value, field="force") is True
    for value in (False, 0, 0.0, "false", "no", "off", "", None):
        assert as_bool(value, field="force") is False

    with pytest.raises(BadRequest, match="force"):
        as_bool("maybe", field="force")


def test_as_int_is_strict_and_range_checked():
    assert as_int(3, field="limit") == 3
    assert as_int(3, field="limit", minimum=1, maximum=3) == 3

    with pytest.raises(BadRequest, match="expected an int"):
        as_int("3", field="limit")          # 不做隐式转换
    with pytest.raises(BadRequest, match="expected an int"):
        as_int(True, field="limit")         # bool 不算 int
    with pytest.raises(BadRequest, match=">= 1"):
        as_int(0, field="limit", minimum=1)
    with pytest.raises(BadRequest, match="<= 10"):
        as_int(11, field="limit", maximum=10)


def test_as_choice_reports_the_allowed_values():
    assert as_choice("asc", ("asc", "desc"), field="order") == "asc"

    with pytest.raises(BadRequest) as excinfo:
        as_choice("sideways", ("asc", "desc"), field="order")
    assert "order" in str(excinfo.value)
    assert "asc" in str(excinfo.value)


def test_reuse_param_groups():
    assert [p.name for p in limit_params()] == ["limit"]
    assert [p.name for p in paging_params()] == ["limit", "offset"]
    assert all(p.kind == "opt" and p.default is not None
               for p in limit_params() + paging_params())
