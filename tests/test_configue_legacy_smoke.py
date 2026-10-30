#
"""旧包 ``albuswall.configue`` 的最小 smoke —— 迁移期间的唯一防线。

旧包在仓库里**没有任何测试**，所以 ``_convert`` 的三个 bug 从未被发现：

1. 容器分支（``dict`` / ``list`` / ``tuple`` / ``set``）被缩进在
   ``if origin in _UNION_ORIGINS`` 里面——非 Union 的泛型永远走不到，
   ``list[str]`` 会退化成 ``list(raw)`` 逐字符。
2. ``if origin is Union`` 只认 ``typing.Union``，不认 ``types.UnionType``：
   Python 3.10–3.13 上 ``int | None`` 会抛 ``TypeError``（3.14 里两者同一个对象，
   所以本机测不出来——用 ``typing.get_origin`` 的等价性断言兜住）。
3. ``dict[str, Path]`` 是 bug 1 的连带受害者（``dict[str, Path]("...")`` 抛
   ``ValueError: dictionary update sequence...``）。

这些用例是「旧包 bug 已修」的唯一证据；旧包删除时本文件一起删。
"""

from __future__ import annotations

import importlib
import sys
import types
import typing
from pathlib import Path

import pytest

# 导入旧包会触发 DeprecationWarning（模块级、每进程一次）；测试关心的是
# 转换行为，警告本身单独用 test_legacy_package_is_deprecated 覆盖。
pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

from albuswall.configue.utils.ini_parser import _convert  # noqa: E402

# ==================== bug 1：非 Union 泛型 ====================

@pytest.mark.parametrize(
    ("raw", "converter", "expected"),
    [
        ("a,b,c", list[str], ["a", "b", "c"]),
        ("Ctrl+O", list[str], ["Ctrl+O"]),
        ("1, 2", list[int], [1, 2]),
        ("", list[str], []),
        ("x, y", tuple[str, ...], ("x", "y")),
        ("a, b", set[str], {"a", "b"}),
        ('{"small": 128}', dict[str, int], {"small": 128}),
    ],
)
def test_convert_container_types(raw, converter, expected):
    assert _convert(raw, converter, {}) == expected


def test_convert_container_is_not_char_split():
    """回归点：bug 1 时 ``list[str]`` 会返回 ``['C', 't', 'r', 'l']``。"""
    assert _convert("Ctrl+S", list[str], {}) == ["Ctrl+S"]


# ==================== bug 2：PEP 604 union ====================

def test_convert_pep604_union():
    assert _convert("42", int | None, {}) == 42
    assert _convert("", int | None, {}) is None
    assert _convert("4", int | str, {}) == 4


def test_convert_typing_union_still_works():
    assert _convert("42", typing.Optional[int], {}) == 42
    assert _convert("", typing.Optional[int], {}) is None


def test_both_union_spellings_are_recognised():
    """3.10–3.13：``get_origin(int | None)`` 是 ``types.UnionType``；
    3.14：``typing.Union is types.UnionType``。两种都要落进 union 分支。"""
    origin = typing.get_origin(int | None)
    assert origin is typing.Union or origin is types.UnionType
    assert typing.get_origin(typing.Optional[int]) is typing.Union


# ==================== bug 3：dict[str, Path] ====================

def test_convert_dict_of_paths():
    assert _convert('{"a": "b"}', dict[str, Path], {}) == {"a": Path("b")}


# ==================== 标量不回归 ====================

@pytest.mark.parametrize(
    ("raw", "converter", "expected"),
    [
        ("true", bool, True),
        ("no", bool, False),
        ("42", int, 42),
        ("1.5", float, 1.5),
        ("plain", str, "plain"),
        ("/tmp/x", Path, Path("/tmp/x")),
    ],
)
def test_convert_scalars(raw, converter, expected):
    assert _convert(raw, converter, {}) == expected


# ==================== 弃用标记 ====================

def test_legacy_package_is_deprecated(monkeypatch):
    """导入旧包发出 DeprecationWarning，且 sys 标记保证每进程只警告一次。"""
    import albuswall.configue

    flag = "_albuswall_configue_deprecation_warned"
    monkeypatch.delattr(sys, flag, raising=False)

    with pytest.warns(DeprecationWarning, match="albuswall.configue1"):
        importlib.reload(albuswall.configue)

    assert getattr(sys, flag) is True

    # 再 reload 一次：标记已置位 → 不再警告
    import warnings

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        importlib.reload(albuswall.configue)

    assert [w for w in caught if issubclass(w.category, DeprecationWarning)] == []
