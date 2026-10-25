#
"""类型转换的「默认回退 + custom_converters 注册」契约。

覆盖：

  1. 未注册的类只要 ``X(raw)`` 能成功，就自动可用（Enum / Path 子类 /
     str 子类 / 自定义类），不再抛 ``TypeError``
  2. ``Enum.__str__`` 不是 value → ``_deconvert`` 写 value，读回能还原
  3. 泛型内部：``Optional[X]`` / ``list[X]`` / ``tuple[X, ...]`` / ``dict[str, X]``
     都会递归到 ``X``
  4. ``_convert_dict_item`` 递归容器：``dict[str, list[X]]`` /
     ``dict[str, dict[str, X]]`` / ``dict[str, set[int]]``
  5. 元素是容器的序列用 JSON 数组表达（``list[list[int]]`` /
     ``list[dict[str, int]]``），``_deconvert`` ↔ ``_convert`` 往返一致
  6. ``Resolver.register_converter`` 增量注册：覆盖默认构造、立即生效
     （已缓存的节点失效重转）、失败抛 ``ValueError`` 而不是静默 ``None``
  7. 构造失败的报错带 raw / converter / cause，不再是裸 ``TypeError``

不依赖 pytest：直接 ``python test/test_converter_registry.py`` 运行，
装了 pytest 的话 ``pytest test/test_converter_registry.py`` 也能跑。
"""

from __future__ import annotations

import sys
import traceback
from enum import Enum
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Set, Tuple

try:  # 允许直接以文件路径运行
    import albuswall  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from albuswall.configue.resolver import Resolver
from albuswall.configue.utils.ini_parser import _convert, _deconvert


# ══════════════════════════════════════════════════════════════
# 被测类型
# ══════════════════════════════════════════════════════════════
class Theme(Enum):
    DARK = "dark"
    LIGHT = "light"


class Alias(Enum):
    A = "alpha"


class MyPath(Path):
    """``Path`` 子类：``MyPath(raw)`` 即可，无需注册。"""


class MyStr(str):
    pass


class Strict:
    """``X(raw)`` 语义正确、但会拒绝非法输入的自定义类。"""

    def __init__(self, raw: str) -> None:
        if not raw.startswith("x:"):
            raise ValueError(f"missing x: prefix in {raw!r}")
        self.raw = raw

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Strict) and other.raw == self.raw

    def __hash__(self) -> int:
        return hash(self.raw)

    def __repr__(self) -> str:
        return f"Strict({self.raw!r})"


class NeedsContext:
    """构造函数语义不对：``NeedsContext("raw")`` 不是你想要的 → 必须注册。"""

    def __init__(self, prefix: str, raw: str) -> None:  # 需要两个参数
        self.value = f"{prefix}/{raw}"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, NeedsContext) and other.value == self.value

    def __hash__(self) -> int:
        return hash(self.value)

    def __repr__(self) -> str:
        return f"NeedsContext({self.value!r})"


# ══════════════════════════════════════════════════════════════
# 1. 默认回退：不用注册
# ══════════════════════════════════════════════════════════════
def test_enum_converts_without_registration() -> None:
    assert _convert("dark", Theme) is Theme.DARK
    assert _convert(" light ", Theme) is Theme.LIGHT


def test_bad_enum_value_raises_value_error() -> None:
    """构造失败统一抛 ValueError（带 raw / converter），不再是裸 TypeError。"""
    try:
        _convert("nope", Theme)
    except ValueError as exc:
        assert "nope" in str(exc)
        assert "Theme" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")


def test_path_and_str_subclass_convert_without_registration() -> None:
    assert _convert("/tmp/x", MyPath) == MyPath("/tmp/x")
    assert isinstance(_convert("/tmp/x", MyPath), MyPath)
    assert _convert("abc", MyStr) == "abc"
    assert isinstance(_convert("abc", MyStr), MyStr)


def test_plain_class_with_one_arg_ctor_converts() -> None:
    assert _convert("x:1", Strict) == Strict("x:1")


def test_ctor_failure_carries_cause() -> None:
    try:
        _convert("y:1", Strict)
    except ValueError as exc:
        assert isinstance(exc.__cause__, ValueError)
        assert "missing x: prefix" in str(exc.__cause__)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")


def test_any_type_passes_through_unchanged() -> None:
    """``Any`` 声明是「原样保留」，不能掉进默认回退里（3.11+ 的 Any 是类）。"""
    assert _convert("dark", Any) == "dark"
    assert _convert("dark, light", List[Any]) == ["dark", "light"]


# ══════════════════════════════════════════════════════════════
# 2. Enum 往返
# ══════════════════════════════════════════════════════════════
def test_enum_deconvert_writes_value_not_repr() -> None:
    assert _deconvert(Theme.DARK, Theme) == "dark"
    assert _convert(_deconvert(Theme.DARK, Theme), Theme) is Theme.DARK


def test_enum_roundtrip_in_containers() -> None:
    value = [Theme.DARK, Theme.LIGHT]
    assert _convert(_deconvert(value, List[Theme]), List[Theme]) == value

    mapping = {"a": Theme.DARK}
    text = _deconvert(mapping, Dict[str, Theme])
    assert text == '{"a": "dark"}'
    assert _convert(text, Dict[str, Theme]) == mapping


# ══════════════════════════════════════════════════════════════
# 3. 泛型内部递归到 X
# ══════════════════════════════════════════════════════════════
def test_optional_enum() -> None:
    assert _convert("dark", Optional[Theme]) is Theme.DARK
    assert _convert("", Optional[Theme]) is None
    assert _convert("   ", Optional[Theme]) is None


def test_list_and_tuple_of_enum() -> None:
    assert _convert("dark, light", List[Theme]) == [Theme.DARK, Theme.LIGHT]
    assert _convert("dark,light", Tuple[Theme, ...]) == (
        Theme.DARK, Theme.LIGHT,
    )


def test_dict_value_enum() -> None:
    assert _convert('{"a": "alpha"}', Dict[str, Alias]) == {"a": Alias.A}


# ══════════════════════════════════════════════════════════════
# 4. _convert_dict_item 递归容器（真 bug 修复）
# ══════════════════════════════════════════════════════════════
def test_dict_of_list_of_enum_recurses() -> None:
    """JSON 里 value 是数组时，内层 Theme 必须被应用（原先原样透传）。"""
    result = _convert('{"a": ["dark", "light"]}', Dict[str, List[Theme]])
    assert result == {"a": [Theme.DARK, Theme.LIGHT]}


def test_dict_of_dict_recurses() -> None:
    """``"1"`` 必须转成 ``1``（原先原样透传成字符串）。"""
    assert _convert('{"a": {"b": 1}}', Dict[str, Dict[str, int]]) == {
        "a": {"b": 1},
    }


def test_dict_of_optional_enum() -> None:
    assert _convert(
        '{"a": "dark", "b": null}', Dict[str, Optional[Theme]],
    ) == {"a": Theme.DARK, "b": None}


def test_dict_of_set() -> None:
    assert _convert('{"a": [1, 2]}', Dict[str, Set[int]]) == {"a": {1, 2}}


# ══════════════════════════════════════════════════════════════
# 5. 元素是容器的序列 → JSON 数组，往返一致
# ══════════════════════════════════════════════════════════════
def test_nested_list_roundtrip() -> None:
    value = [[1, 2], [3, 4]]
    text = _deconvert(value, List[List[int]])
    assert text == "[[1, 2], [3, 4]]"
    assert _convert(text, List[List[int]]) == value


def test_list_of_dict_roundtrip() -> None:
    value = [{"a": 1}, {"b": 2}]
    text = _deconvert(value, List[Dict[str, int]])
    assert _convert(text, List[Dict[str, int]]) == value


def test_set_and_frozenset_roundtrip() -> None:
    """``set[X]`` / ``frozenset[X]`` 与 list 同形态，读写对称。"""
    assert _convert("1, 2, 2", Set[int]) == {1, 2}
    assert _convert("", Set[int]) == set()
    assert _deconvert({1, 2}, Set[int]) == "1, 2"
    assert _convert(_deconvert({1, 2}, Set[int]), Set[int]) == {1, 2}

    assert isinstance(_convert("a, b", FrozenSet[str]), frozenset)
    assert _convert(_deconvert(frozenset({"a"}), FrozenSet[str]), FrozenSet[str]) \
        == frozenset({"a"})


def test_plain_scalar_list_still_comma_separated() -> None:
    """不带歧义的 ``list[int]`` / ``list[str]`` 仍是逗号分隔。"""
    assert _deconvert([1, 2], List[int]) == "1, 2"
    assert _convert("1, 2", List[int]) == [1, 2]
    assert _deconvert(["a", "b"], List[str]) == "a, b"
    assert _convert("a, b", List[str]) == ["a", "b"]


def test_list_str_with_comma_roundtrips() -> None:
    """``list[str]`` 元素含逗号时改写成 JSON 数组，不再被逗号吞掉。"""
    value = ["Ctrl+S", "Ctrl+,"]
    text = _deconvert(value, List[str])

    assert text == '["Ctrl+S", "Ctrl+,"]'
    assert _convert(text, List[str]) == value


def test_list_str_ambiguous_elements_roundtrip() -> None:
    """空串 / 首尾空白 / 以 ``[`` 开头的元素同样用 JSON 保住往返。"""
    for value in ([""], ["a "], [" a"], ["[x]"], ["a,b"]):
        text = _deconvert(value, List[str])
        assert _convert(text, List[str]) == value, (value, text)


def test_list_of_keys_with_plus_comma_survives_flush_shape() -> None:
    """模拟 keymap 的 ``list[str]`` 绑定：``Ctrl+,`` 就是那个真实受害者。"""
    value = ["Ctrl+S", "Ctrl+,"]
    text = _deconvert(value, List[str])
    # 逗号分隔会读成 ['Ctrl+S', 'Ctrl+', ''] → 丢掉一项
    assert _convert("Ctrl+S, Ctrl+,", List[str]) == ["Ctrl+S", "Ctrl+"]
    assert _convert(text, List[str]) == value


# ══════════════════════════════════════════════════════════════
# 5b. 写回方向：writer 注册 / from_ini + to_ini 约定
# ══════════════════════════════════════════════════════════════
class Seq:
    """模拟 ``QKeySequence``：``str()`` 不稳定，必须显式给 writer。"""

    def __init__(self, text: str) -> None:
        if not text or text.endswith("+"):
            raise ValueError(f"bad key sequence {text!r}")
        self.text = text

    def __str__(self) -> str:  # 故意不可读回
        return "<Seq object at 0x...>"

    def portable(self) -> str:
        return self.text

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Seq) and other.text == self.text

    def __hash__(self) -> int:
        return hash(self.text)


def test_deconvert_without_writer_uses_custom_str() -> None:
    """没有 writer 但对象自带可读的 ``__str__`` 时就用它（明确的退路）。"""

    class Slug:
        def __init__(self, text: str) -> None:
            self.text = text

        def __str__(self) -> str:
            return f"#{self.text}"

    assert _deconvert(Slug("abc"), Slug) == "#abc"


def test_deconvert_without_writer_refuses_default_repr() -> None:
    """``str(value)`` 只是默认 repr 时宁可报错——写出去下次必然读不回来。"""

    class Opaque:
        pass

    try:
        _deconvert(Opaque(), Opaque)
    except ValueError as exc:
        assert "has no INI text form" in str(exc)
        assert "writer" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError for default repr")


def test_custom_serializer_is_used_for_write() -> None:
    serializers = {Seq: lambda value: value.portable()}
    assert _deconvert(Seq("Ctrl+S"), Seq, None, serializers) == "Ctrl+S"


def test_custom_serializer_failure_raises() -> None:
    def boom(value: Seq) -> str:
        raise RuntimeError("writer exploded")

    try:
        _deconvert(Seq("Ctrl+S"), Seq, None, {Seq: boom})
    except ValueError as exc:
        assert "writer exploded" in str(exc)
        assert isinstance(exc.__cause__, RuntimeError)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")


def test_custom_serializer_must_return_str() -> None:
    try:
        _deconvert(Seq("Ctrl+S"), Seq, None, {Seq: lambda value: 42})
    except ValueError as exc:
        assert "must return str" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")


def test_serializer_applies_inside_containers() -> None:
    serializers = {Seq: lambda value: value.portable()}
    value = [Seq("Ctrl+S"), Seq("Ctrl+,")]
    text = _deconvert(value, List[Seq], None, serializers)

    # 元素带逗号 → JSON 数组，避免逗号吞值
    assert text == '["Ctrl+S", "Ctrl+,"]'
    assert _convert(text, List[Seq]) == value


def test_from_ini_to_ini_convention_needs_no_registration() -> None:
    """类型自带 ``from_ini`` / ``to_ini`` 时读写全自动，不必注册。"""

    class Portable:
        def __init__(self, text: str) -> None:
            self.text = text

        @classmethod
        def from_ini(cls, raw: str) -> "Portable":
            if not raw.startswith("p:"):
                raise ValueError(f"expected p: prefix, got {raw!r}")
            return cls(raw[2:])

        def to_ini(self) -> str:
            return f"p:{self.text}"

        def __eq__(self, other: object) -> bool:
            return isinstance(other, Portable) and other.text == self.text

        def __hash__(self) -> int:
            return hash(self.text)

    assert _convert("p:abc", Portable) == Portable("abc")
    assert _deconvert(Portable("abc"), Portable) == "p:abc"
    # 容器里同样生效
    assert _convert(_deconvert([Portable("a")], List[Portable]),
                    List[Portable]) == [Portable("a")]


def test_register_converter_with_writer_roundtrips_through_flush() -> None:
    """``register_converter(..., writer=...)`` 后 ``flush`` 写出可读回的值。"""
    import tempfile

    from albuswall.configue.configue import Configue, StaticConfig

    d = Path(tempfile.mkdtemp(prefix="albuswall-conv-"))
    main = d / "main.ini"
    main.write_text("[kbd]\nseq = Ctrl+S\n", encoding="utf-8")

    config = Configue()
    config.resolver = Resolver()
    config.static = StaticConfig(config.resolver)
    config._last_flush_error = None
    config._default_write_file = main
    config.configure_persistence(autosave="manual")

    config.register_converter(
        Seq, lambda raw: Seq(raw), writer=lambda value: value.portable(),
    )
    resolver = config.resolver
    resolver.register_type("kbd", "seq", Seq)
    resolver.inject_raw("kbd", "seq", "Ctrl+S", origin=main)
    assert resolver.get_typed("kbd", "seq") == Seq("Ctrl+S")

    config.set("kbd", "seq", Seq("Ctrl+,"))
    config.flush()

    assert main.read_text(encoding="utf-8") == "[kbd]\nseq = Ctrl+,\n"
    assert resolver.get_typed("kbd", "seq") == Seq("Ctrl+,")


def test_flush_without_writer_fails_loudly_not_silently() -> None:
    """只注册 reader 且对象只有默认 repr：``flush`` 报错，**不**写坏文件。"""
    import tempfile

    from albuswall.configue.configue import Configue, StaticConfig

    class Opaque:
        def __init__(self, raw: str) -> None:
            self.raw = raw

    d = Path(tempfile.mkdtemp(prefix="albuswall-conv-"))
    main = d / "main.ini"
    main.write_text("[kbd]\nseq = Ctrl+S\n", encoding="utf-8")

    config = Configue()
    config.resolver = Resolver()
    config.static = StaticConfig(config.resolver)
    config._last_flush_error = None
    config._default_write_file = main
    config.configure_persistence(autosave="manual")

    config.register_converter(Opaque, lambda raw: Opaque(raw))
    resolver = config.resolver
    resolver.register_type("kbd", "seq", Opaque)
    resolver.inject_raw("kbd", "seq", "Ctrl+S", origin=main)

    config.set("kbd", "seq", Opaque("Ctrl+,"))
    assert config.flush() == []          # 没有写任何文件
    assert config.last_flush_error is not None
    assert main.read_text(encoding="utf-8") == "[kbd]\nseq = Ctrl+S\n"
    assert resolver.dirty_nodes() == frozenset({("kbd", "seq")})


# ══════════════════════════════════════════════════════════════
# 6. Resolver.register_converter 增量注册
# ══════════════════════════════════════════════════════════════
def test_register_converter_enables_type_needing_context() -> None:
    resolver = Resolver()
    resolver.register_converter(
        NeedsContext, lambda raw: NeedsContext("ctx", raw),
    )
    resolver.register_type("db", "dsn", NeedsContext)
    resolver.inject_raw("db", "dsn", "main")

    assert resolver.get_typed("db", "dsn") == NeedsContext("ctx", "main")


def test_register_converter_overrides_default_construction() -> None:
    class Flexible:
        def __init__(self, *args: str) -> None:
            self.args = args

    resolver = Resolver()
    resolver.register_type("ui", "theme", Flexible)

    # 默认回退：Flexible(raw)（构造能成功，但语义不一定对）
    resolver.inject_raw("ui", "theme", "dark")
    assert resolver.get_typed("ui", "theme").args == ("dark",)

    # 注册后立即生效：已缓存的节点失效重转
    resolver.register_converter(Flexible, lambda raw: Flexible(raw, "extra"))
    assert resolver.get_typed("ui", "theme").args == ("dark", "extra")


def test_register_converter_failure_raises_not_none() -> None:
    """自定义转换器抛异常 → ValueError，绝不静默变 None。"""

    def boom(raw: str) -> Strict:
        raise RuntimeError("converter exploded")

    resolver = Resolver()
    resolver.register_converter(Strict, boom)
    resolver.register_type("ui", "thing", Strict)
    resolver.inject_raw("ui", "thing", "x:1")

    try:
        resolver.get_typed("ui", "thing")
    except ValueError as exc:
        assert "converter exploded" in str(exc)
        assert isinstance(exc.__cause__, RuntimeError)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError, not a silent None")


def test_check_all_surfaces_conversion_errors() -> None:
    resolver = Resolver()
    resolver.register_type("ui", "theme", Theme)
    resolver.inject_raw("ui", "theme", "no-such-theme")

    try:
        resolver.check_all()
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("check_all 应把转换失败抛出来")


def test_register_custom_converters_bulk_replace() -> None:
    resolver = Resolver()
    resolver.register_custom_converters({Theme: lambda raw: Theme.DARK})
    resolver.register_type("ui", "theme", Theme)
    resolver.inject_raw("ui", "theme", "light")

    assert resolver.get_typed("ui", "theme") is Theme.DARK
    assert Theme in resolver.custom_converters()


# ══════════════════════════════════════════════════════════════
# 7. Configue 统一入口
# ══════════════════════════════════════════════════════════════
def test_configue_register_converter_entrypoint() -> None:
    from albuswall.configue.configue import Configue

    config = Configue()
    config.register_converter(NeedsContext, lambda raw: NeedsContext("cfg", raw))
    assert NeedsContext in config.resolver.custom_converters()


def test_typed_config_enum_through_flush() -> None:
    """``Enum`` 声明的 key 能读、能写、写完能读回（不需要注册任何东西）。"""
    import tempfile
    from textwrap import dedent

    from albuswall.configue.configue import Configue, StaticConfig

    d = Path(tempfile.mkdtemp(prefix="albuswall-conv-"))
    main = d / "main.ini"
    main.write_text("[ui]\ntheme = light\n", encoding="utf-8")

    # 重置单例，避免和其他用例互相影响
    config = Configue()
    config.resolver = Resolver()
    config.static = StaticConfig(config.resolver)
    config._last_flush_error = None
    config._default_write_file = main
    config.configure_persistence(autosave="manual")

    resolver = config.resolver
    resolver.register_type("ui", "theme", Theme)
    resolver.inject_raw("ui", "theme", "light", origin=main)

    assert resolver.get_typed("ui", "theme") is Theme.LIGHT
    resolver.check_all()

    config.set("ui", "theme", Theme.DARK)
    config.flush()

    assert main.read_text(encoding="utf-8") == dedent("""\
        [ui]
        theme = dark
    """)
    assert resolver.get_typed("ui", "theme") is Theme.DARK


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
