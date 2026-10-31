#
"""``@configclass`` 注入的 ``Config.__repr__``。

行为约定：

- 列出每个字段的当前值（``!r``，和 dataclass 一致）；
- 没值的字段显示 ``<missing>``，取不到的显示 ``<error: ...>``——**repr 永不抛**；
- ``Configue`` 未构造时显示 ``<not initialized>``；
- 用户自己写的 ``__repr__`` 不会被覆盖。
"""

from __future__ import annotations

from typing import Optional

import pytest

from albuswall.configue import (
    ConfigFile,
    Configue,
    configclass,
    field,
    reg,
)

from .conftest import write_ini

INI = "[core]\ndebug = true\nname = hello\n"


@pytest.fixture
def loaded(tmp_path):
    write_ini(tmp_path / "config.ini", INI)
    reg(ConfigFile.INI, "config.ini", name="main", autosave="off")

    @configclass("core", "main")
    class Core:
        debug: bool = field(default=False)
        name: str = field(default="x")
        port: int = field(default=8000)

    c = Configue(tmp_path)
    c.load()
    return c, Core


def q(cls) -> str:
    """repr 用的是 ``__qualname__``（本地类会带 ``<locals>`` 前缀）。"""
    return cls.__qualname__


def test_repr_lists_all_fields(loaded):
    _, Core = loaded

    text = repr(Core())

    assert text.startswith(f"{q(Core)}(")
    assert "debug=True" in text            # !r：bool 可辨认
    assert "name='hello'" in text          # 字符串带引号
    assert "port=8000" in text


def test_repr_is_stable_and_single_line(loaded):
    _, Core = loaded
    assert "\n" not in repr(Core())


def test_repr_marks_missing_fields(loaded):
    _, _ = loaded

    @configclass("core2", "main")
    class NeedsValue:
        required: str

    assert repr(NeedsValue()) == f"{q(NeedsValue)}(required=<missing>)"


def test_repr_marks_conversion_errors(loaded):
    c, Core = loaded
    # 手工塞一个转不成 int 的原始值：repr 不能因此炸掉
    c.resolver.set("core", "port", "not-an-int")

    text = repr(Core())

    assert "port=<error:" in text
    assert "not-an-int" in text or "invalid literal" in text


def test_repr_without_configue():
    @configclass("core", "main")
    class Core:
        debug: bool = True

    assert Configue._instance is None
    assert repr(Core()) == f"{q(Core)}(<not initialized>)"


def test_repr_after_unload_marks_missing(loaded):
    c, Core = loaded
    c.unload("main")

    # 文件值撤掉后，剩下的默认值里没有 port？有——port 有默认值。
    assert "port=8000" in repr(Core())


def test_repr_optional_none(loaded):
    c, _ = loaded

    @configclass("core3", "main")
    class WitOptional:
        limit: Optional[int] = None

    # 类是在 load() 之后声明的 → configclass 装饰当下就注入了默认值
    # （早注册路径由 load() 的 Step 4 注入，见 test_late_class.py）
    assert repr(WitOptional()) == f"{q(WitOptional)}(limit=None)"

    # 再 load() 一次不影响已有值（Step 4 每次全量重跑，但只在缺值时注入）
    c.load()
    assert repr(WitOptional()) == f"{q(WitOptional)}(limit=None)"


def test_user_repr_is_not_overridden():
    @configclass("core", "main")
    class Custom:
        debug: bool = True

        def __repr__(self) -> str:
            return "my-own-repr"

    Configue.__new__(Configue, "/tmp")  # 单例（未 load 也没关系）

    assert repr(Custom()) == "my-own-repr"


def test_generated_repr_is_refreshed_on_redecoration(tmp_path):
    write_ini(tmp_path / "config.ini", "[core]\na = 1\n")
    reg(ConfigFile.INI, "config.ini", name="main", autosave="off")

    class C:
        a: int = 0

    C = configclass("core", "main")(C)
    Configue(tmp_path).load()
    assert repr(C()) == f"{q(C)}(a=1)"

    # 同一个类补一个字段再装饰 → repr 跟着更新（字段来自新的 ClassInfo）
    C.__annotations__["b"] = int
    C.b = 2
    configclass("core", "main")(C)

    # 新字段的默认值要再 load() 一次才会注入
    Configue(tmp_path).load()
    assert repr(C()) == f"{q(C)}(a=1, b=2)"
