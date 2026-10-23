#
"""``_deconvert``（typed -> raw）与 ``_convert``（raw -> typed）的对称性。

约定（见 ``albuswall.configue.utils.ini_parser._deconvert``）：

- None -> ""
- bool -> "true" / "false"
- Path -> str(value)
- list / tuple -> ", ".join(...)
- int / float / str -> str(value)
- 未知类型 -> str(value)（fail-safe，不抛）

每个 case 都按 round-trip 断言：``_convert(_deconvert(x, t), t) == x``。

不依赖 pytest：直接 ``python test/test_deconvert.py`` 运行，
装了 pytest 的话 ``pytest test/test_deconvert.py`` 也能跑。
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path
from typing import List, Optional, Tuple, Union

try:  # 允许直接以文件路径运行
    import albuswall  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from albuswall.configue.utils.ini_parser import _convert, _deconvert


def _assert_roundtrip(value, converter) -> str:
    raw = _deconvert(value, converter)
    assert isinstance(raw, str), f"_deconvert must return str, got {type(raw)}"
    back = _convert(raw, converter)
    assert back == value, (
        f"round-trip mismatch: {value!r} -> {raw!r} -> {back!r} "
        f"(converter={converter})"
    )
    return raw


# ══════════════════════════════════════════════════════════════
# 基本类型
# ══════════════════════════════════════════════════════════════
def test_str_roundtrip() -> None:
    assert _deconvert("hello", str) == "hello"
    _assert_roundtrip("hello", str)
    _assert_roundtrip("", str)


def test_int_roundtrip() -> None:
    assert _deconvert(123, int) == "123"
    _assert_roundtrip(0, int)
    _assert_roundtrip(-42, int)


def test_float_roundtrip() -> None:
    assert _deconvert(3.14, float) == "3.14"
    _assert_roundtrip(3.14, float)
    _assert_roundtrip(1e-7, float)


def test_bool_roundtrip() -> None:
    assert _deconvert(True, bool) == "true"
    assert _deconvert(False, bool) == "false"
    _assert_roundtrip(True, bool)
    _assert_roundtrip(False, bool)


def test_path_roundtrip() -> None:
    value = Path("/tmp/test")
    assert _deconvert(value, Path) == "/tmp/test"
    _assert_roundtrip(value, Path)


def test_none_is_empty_string() -> None:
    assert _deconvert(None, int) == ""
    assert _deconvert(None, str) == ""
    assert _deconvert(None, Optional[int]) == ""
    assert _deconvert(None, Path) == ""


# ══════════════════════════════════════════════════════════════
# Optional / Union
# ══════════════════════════════════════════════════════════════
def test_optional_none_roundtrip() -> None:
    assert _deconvert(None, Optional[int]) == ""
    assert _convert("", Optional[int]) is None


def test_optional_value_roundtrip() -> None:
    _assert_roundtrip(42, Optional[int])
    _assert_roundtrip("abc", Optional[str])
    _assert_roundtrip(True, Optional[bool])
    _assert_roundtrip(Path("/a/b"), Optional[Path])


def test_union_roundtrip() -> None:
    _assert_roundtrip(42, Union[int, str])
    _assert_roundtrip("hello", Union[int, str])
    _assert_roundtrip(3.14, Union[int, float])


# ══════════════════════════════════════════════════════════════
# 容器
# ══════════════════════════════════════════════════════════════
def test_list_roundtrip() -> None:
    assert _deconvert([1, 2, 3], List[int]) == "1, 2, 3"
    _assert_roundtrip([1, 2, 3], List[int])
    _assert_roundtrip(["a", "b", "c"], List[str])
    _assert_roundtrip([], List[int])


def test_list_of_path_roundtrip() -> None:
    _assert_roundtrip([Path("/a"), Path("/b")], List[Path])


def test_tuple_variadic_roundtrip() -> None:
    assert _deconvert((1, 2, 3), Tuple[int, ...]) == "1, 2, 3"
    _assert_roundtrip((1, 2, 3), Tuple[int, ...])
    _assert_roundtrip((), Tuple[int, ...])


def test_tuple_fixed_roundtrip() -> None:
    assert _deconvert((42, "hello"), Tuple[int, str]) == "42, hello"
    _assert_roundtrip((42, "hello"), Tuple[int, str])


def test_optional_list_roundtrip() -> None:
    _assert_roundtrip([1, 2], Optional[List[int]])
    assert _deconvert(None, Optional[List[int]]) == ""


# ══════════════════════════════════════════════════════════════
# fail-safe / 自定义转换器
# ══════════════════════════════════════════════════════════════
def test_unknown_converter_falls_back_to_str() -> None:
    assert _deconvert(123, dict) == "123"
    assert _deconvert(object, type)  # 不抛即可
    assert _deconvert([1, 2], dict) == "[1, 2]"


def test_custom_converter_is_not_inverted() -> None:
    """自定义转换器无法反解，直接 str(value)（保持 fail-safe）。"""
    custom = {"upper": lambda s: s.upper()}

    assert _deconvert("abc", "upper", custom) == "abc"
    assert _deconvert(5, "double", {"double": lambda s: int(s) * 2}) == "5"


# ══════════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════════
def main() -> int:
    tests = [
        (name, obj)
        for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    failures: list[tuple[str, BaseException]] = []
    for name, fn in tests:
        try:
            fn()
        except BaseException as exc:  # noqa: BLE001
            failures.append((name, exc))
            print(f"FAIL {name}: {exc}")
            traceback.print_exc()
        else:
            print(f"ok   {name}")

    print()
    if failures:
        print(f"{len(failures)}/{len(tests)} 个用例失败")
        return 1
    print(f"全部 {len(tests)} 个用例通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
