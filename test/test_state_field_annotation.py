#
"""``StateField`` 从类型注解兜底读取类型 + 泛型 isinstance 修复。

因为模块开了 ``from __future__ import annotations``，本文件里所有类属性
注解天然是字符串，正好覆盖「注解 → 类型」的解析路径。

覆盖清单（对应任务书的 12 条）：

 1. 旧写法 ``StateField(str, default="x")`` 行为不变
 2. 新写法 ``x: str = StateField(default="x")``
 3. 自定义类型（Enum）能在 future-annotations 模块里解析出来
 4. ``Optional[int]``：None / int 通过，str 抛 TypeError，不抛 Subscripted generics
 5. ``list[int]``：元素校验
 6. ``UserPrefs``：dict 自动提升；``nested_cls`` 正确
 7. ``Optional[UserPrefs]``：dict 自动提升；None 允许
 8. 无类型无注解：不校验
 9. 显式 type_ 优先于冲突注解
10. 显式 nested=False：dict 不提升、原样存
11. 注解解析失败：type=None + warning + fail-open
12. 嵌套子树与父对象共享 _ctx（脏标记可见）

不依赖 pytest：直接 ``python test/test_state_field_annotation.py`` 运行。
"""

from __future__ import annotations

import enum
import sys
import traceback
import warnings
from pathlib import Path
from typing import Optional

try:  # 允许直接以文件路径运行
    import albuswall  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from albuswall.configue.state import (
    ObservableNamespace,
    StateField,
)
from albuswall.configue.utils.annotation import resolve_annotation


# ══════════════════════════════════════════════════════════════
# 测试用类型（模块级定义：验证 owner 模块 globals 解析）
# ══════════════════════════════════════════════════════════════
class Color(enum.Enum):
    RED = "red"
    BLUE = "blue"


class UserPrefs(ObservableNamespace):
    theme: str = StateField(default="dark")


def _raises(exc_type: type[BaseException], fn) -> BaseException:
    try:
        fn()
    except exc_type as exc:
        return exc
    raise AssertionError(f"expected {exc_type.__name__}, nothing raised")


# ══════════════════════════════════════════════════════════════
# 1. 旧写法
# ══════════════════════════════════════════════════════════════
def test_legacy_explicit_type_unchanged() -> None:
    class Legacy(ObservableNamespace):
        user_id = StateField(str, default="anon")

    field = Legacy.__dict__["user_id"]
    assert field.type is str
    assert field.nested is False
    assert field.nested_cls is None

    state = Legacy()
    assert state.user_id == "anon"
    state.user_id = "bob"
    assert state.user_id == "bob"

    exc = _raises(TypeError, lambda: setattr(state, "user_id", 1))
    assert "user_id" in str(exc)
    assert "expected str" in str(exc)


# ══════════════════════════════════════════════════════════════
# 2. 新写法：注解兜底
# ══════════════════════════════════════════════════════════════
def test_annotation_supplies_type() -> None:
    class FromAnnotation(ObservableNamespace):
        x: str = StateField(default="x")

    field = FromAnnotation.__dict__["x"]
    assert field.type is str, "注解应解析成真正的 str，而不是字符串 'str'"
    assert field.nested is False

    state = FromAnnotation()
    state.x = "ok"
    _raises(TypeError, lambda: setattr(state, "x", 1))


def test_annotation_and_explicit_type_agree() -> None:
    class Both(ObservableNamespace):
        x: str = StateField(str, default="x")

    assert Both.__dict__["x"].type is str


# ══════════════════════════════════════════════════════════════
# 3. future annotations 下的自定义类型
# ══════════════════════════════════════════════════════════════
def test_custom_enum_resolves_from_owner_module() -> None:
    class Palette(ObservableNamespace):
        color: Color = StateField()

    field = Palette.__dict__["color"]
    assert field.type is Color, "owner 模块 globals 必须参与解析"
    assert not isinstance(field.type, str)

    state = Palette()
    state.color = Color.RED
    _raises(TypeError, lambda: setattr(state, "color", "red"))
    _raises(TypeError, lambda: setattr(state, "color", Color))


# ══════════════════════════════════════════════════════════════
# 4. Optional[int]
# ══════════════════════════════════════════════════════════════
def test_optional_int_validates_without_subscript_error() -> None:
    class OptInt(ObservableNamespace):
        x: Optional[int] = StateField()

    field = OptInt.__dict__["x"]
    assert field.type == Optional[int]

    state = OptInt()
    state.x = None
    state.x = 1
    state.x = None

    exc = _raises(TypeError, lambda: setattr(state, "x", "a"))
    assert "Subscripted generics" not in str(exc), (
        f"泛型 isinstance 的 TypeError 泄漏了: {exc}"
    )


# ══════════════════════════════════════════════════════════════
# 5. list[int]
# ══════════════════════════════════════════════════════════════
def test_list_int_element_validation() -> None:
    class ListInt(ObservableNamespace):
        x: list[int] = StateField()

    field = ListInt.__dict__["x"]
    assert field.type == list[int]
    assert field.nested is False

    state = ListInt()
    state.x = [1, 2, 3]
    state.x = []
    _raises(TypeError, lambda: setattr(state, "x", "abc"))
    _raises(TypeError, lambda: setattr(state, "x", [1, "a"]))


def test_dict_and_optional_container_validation() -> None:
    class Containers(ObservableNamespace):
        mapping: dict[str, int] = StateField()
        maybe: Optional[list[str]] = StateField()
        fixed: tuple[int, str] = StateField()

    state = Containers()
    state.mapping = {"a": 1}
    _raises(TypeError, lambda: setattr(state, "mapping", {"a": "x"}))
    _raises(TypeError, lambda: setattr(state, "mapping", {1: 1}))

    state.maybe = None
    state.maybe = ["a"]
    _raises(TypeError, lambda: setattr(state, "maybe", [1]))

    state.fixed = (1, "a")
    _raises(TypeError, lambda: setattr(state, "fixed", (1, 2)))


# ══════════════════════════════════════════════════════════════
# 6 / 7. 嵌套 Namespace
# ══════════════════════════════════════════════════════════════
def test_namespace_annotation_promotes_dict() -> None:
    class WithPrefs(ObservableNamespace):
        prefs: UserPrefs = StateField()

    field = WithPrefs.__dict__["prefs"]
    assert field.nested is True
    assert field.nested_cls is UserPrefs

    state = WithPrefs()
    state.prefs = {"theme": "light"}
    assert isinstance(state.prefs, UserPrefs)
    assert state.prefs.theme == "light"


def test_optional_namespace_promotes_dict() -> None:
    class MaybePrefs(ObservableNamespace):
        prefs: Optional[UserPrefs] = StateField()

    field = MaybePrefs.__dict__["prefs"]
    assert field.nested is True
    assert field.nested_cls is UserPrefs, (
        "Optional[UserPrefs] 必须解包出真正的类，否则实例化会炸"
    )
    assert field.type == Optional[UserPrefs]

    state = MaybePrefs()
    state.prefs = {"theme": "light"}
    assert isinstance(state.prefs, UserPrefs)
    state.prefs = None  # None 允许
    assert state.prefs is None


def test_pep604_union_supported() -> None:
    """``int | None`` / ``UserPrefs | None`` 与 typing.Optional 等价。"""
    class Modern(ObservableNamespace):
        x: int | None = StateField()
        prefs: UserPrefs | None = StateField()

    x_field = Modern.__dict__["x"]
    assert x_field.type == (int | None)
    assert x_field.nested is False

    prefs_field = Modern.__dict__["prefs"]
    assert prefs_field.nested is True
    assert prefs_field.nested_cls is UserPrefs

    state = Modern()
    state.x = None
    state.x = 3
    _raises(TypeError, lambda: setattr(state, "x", "a"))

    state.prefs = {"theme": "light"}
    assert isinstance(state.prefs, UserPrefs)
    state.prefs = None
    assert state.prefs is None


def test_list_of_namespace_is_not_nested() -> None:
    class ManyPrefs(ObservableNamespace):
        prefs: list[UserPrefs] = StateField()

    field = ManyPrefs.__dict__["prefs"]
    assert field.nested is False, "list[Namespace] 不自动嵌套（文档写死）"
    assert field.nested_cls is None


# ══════════════════════════════════════════════════════════════
# 8. 无类型无注解
# ══════════════════════════════════════════════════════════════
def test_untyped_field_accepts_anything() -> None:
    class Untyped(ObservableNamespace):
        anything = StateField()

    field = Untyped.__dict__["anything"]
    assert field.type is None

    state = Untyped()
    for value in (None, 1, "s", object(), [1, 2], {"a": 1}):
        state.anything = value


# ══════════════════════════════════════════════════════════════
# 9. 显式 type_ 优先
# ══════════════════════════════════════════════════════════════
def test_explicit_type_wins_over_annotation() -> None:
    class Conflict(ObservableNamespace):
        x: int = StateField(str)

    field = Conflict.__dict__["x"]
    assert field.type is str

    state = Conflict()
    state.x = "a"  # 按显式 str 校验
    _raises(TypeError, lambda: setattr(state, "x", 1))


# ══════════════════════════════════════════════════════════════
# 10. 显式 nested=False
# ══════════════════════════════════════════════════════════════
def test_explicit_nested_false_keeps_raw_dict() -> None:
    class RawPrefs(ObservableNamespace):
        prefs: UserPrefs = StateField(nested=False)

    field = RawPrefs.__dict__["prefs"]
    assert field.nested is False
    assert field.type is UserPrefs

    state = RawPrefs()
    state.prefs = {"theme": "light"}
    assert isinstance(state.prefs, dict), "显式 nested=False 不应自动提升"
    assert state.prefs == {"theme": "light"}


# ══════════════════════════════════════════════════════════════
# 11. 解析失败 fail-open
# ══════════════════════════════════════════════════════════════
def test_unresolvable_annotation_fails_open() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")

        class Broken(ObservableNamespace):
            x: UndefinedTypeForTest = StateField()

    field = Broken.__dict__["x"]
    assert field.type is None, "解析失败应回退 None（不校验），不是 str"

    messages = [str(item.message) for item in caught]
    assert any("UndefinedTypeForTest" in m for m in messages), messages
    assert any("Broken" in m for m in messages), "warning 应带上 owner"

    state = Broken()
    for value in (None, 1, "s", object()):
        state.x = value


def test_resolve_annotation_returns_none_on_failure() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert resolve_annotation("NoSuchNameAnywhere") is None
    assert caught

    # 非字符串原样返回，不算失败、不该有 warning
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert resolve_annotation(int) is int
    assert not caught


def test_resolve_annotation_uses_owner_module() -> None:
    # Color 是本模块的名字；owner 只要 __module__ 指向本模块即可命中
    class Holder:
        pass

    Holder.__module__ = __name__
    assert resolve_annotation("Color", owner=Holder) is Color
    assert resolve_annotation("Optional[int]", owner=Holder) == Optional[int]
    assert resolve_annotation("Path", owner=Holder) is Path


# ══════════════════════════════════════════════════════════════
# 12. 嵌套子树共享 _ctx
# ══════════════════════════════════════════════════════════════
def test_nested_subtree_shares_context() -> None:
    class Root(ObservableNamespace):
        prefs: UserPrefs = StateField()

    root = Root(prefs={"theme": "light"})
    assert isinstance(root.prefs, UserPrefs)

    child_ctx = object.__getattribute__(root.prefs, "_ctx")
    parent_ctx = object.__getattribute__(root, "_ctx")
    assert child_ctx is parent_ctx, "嵌套子树必须共享同一个 _ctx（脏标记 / 观察者）"

    root.commit()
    root.prefs.theme = "solarized"
    assert "theme" in root.dirty_keys, "子字段写入必须反映到父对象的脏标记"


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
