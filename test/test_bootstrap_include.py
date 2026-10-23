#
"""``load_config_tree``：多文件加载 + 合并顺序 + 严格失败语义。

覆盖阶段 2：

  1. 单 include：子文件的值被主文件覆盖 / 不覆盖（两种方向）
  2. glob：``profile.d/*.ini`` 按字典序展开，靠后的赢
  3. ``include.ro`` 优先级低于 ``include``
  4. 主文件 include 自己 → ConfigError（循环）
  5. include 指向的文件不存在 → ConfigError
  6. origin 正确：每个生效值的 origin 指向最后写入它的文件
  7. 子文件自己的 include 被忽略（扁平结构）
  8. [configue] include 系不会进入合并结果（Resolver 看不到）
  9. pkg:// 前缀 → ConfigError（未实现）
 10. setup_static_config 集成：多文件生效 + include.ro 标记为只读

不依赖 pytest：直接 ``python test/test_bootstrap_include.py`` 运行，
装了 pytest 的话 ``pytest test/test_bootstrap_include.py`` 也能跑。
"""

from __future__ import annotations

import sys
import tempfile
import traceback
from pathlib import Path
from textwrap import dedent

try:  # 允许直接以文件路径运行
    import albuswall  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from albuswall.configue.bootstrap import load_config_tree
from albuswall.configue.exceptions import ConfigError, ConfigueErrorSource

# ══════════════════════════════════════════════════════════════
# 工具
# ══════════════════════════════════════════════════════════════
def _tmpdir() -> Path:
    return Path(tempfile.mkdtemp(prefix="albuswall-cfg-"))


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dedent(text).lstrip("\n"), encoding="utf-8")
    return path


# ══════════════════════════════════════════════════════════════
# 合并顺序
# ══════════════════════════════════════════════════════════════
def test_child_value_survives_when_main_is_silent() -> None:
    d = _tmpdir()
    _write(d / "machine.ini", """
        [ui]
        width = 1280
    """)
    main = _write(d / "main.ini", """
        [configue]
        include = ./machine.ini

        [ui]
        height = 720
    """)

    merged = load_config_tree(main)

    assert merged[("ui", "width")] == ("1280", d / "machine.ini")
    assert merged[("ui", "height")] == ("720", main.resolve())


def test_main_overrides_include() -> None:
    d = _tmpdir()
    _write(d / "machine.ini", """
        [ui]
        width = 1280
    """)
    main = _write(d / "main.ini", """
        [configue]
        include = ./machine.ini

        [ui]
        width = 1920
    """)

    merged = load_config_tree(main)

    assert merged[("ui", "width")] == ("1920", main.resolve())


def test_glob_expands_in_sorted_order_later_wins() -> None:
    d = _tmpdir()
    _write(d / "profile.d" / "b.ini", "[ui]\nwidth = 20\n")
    _write(d / "profile.d" / "a.ini", "[ui]\nwidth = 10\n")
    _write(d / "profile.d" / "ignored.txt", "[ui]\nwidth = 999\n")
    main = _write(d / "main.ini", """
        [configue]
        include = ./profile.d/*.ini
    """)

    merged = load_config_tree(main)

    # 字典序 a.ini -> b.ini，靠后的赢；非 *.ini 不参与
    assert merged[("ui", "width")] == ("20", d / "profile.d" / "b.ini")


def test_include_ro_has_lower_priority_than_include() -> None:
    d = _tmpdir()
    ro = _write(d / "defaults.ini", """
        [ui]
        width = 1
        height = 1
    """)
    rw = _write(d / "machine.ini", """
        [ui]
        width = 1280
    """)
    main = _write(d / "main.ini", """
        [configue]
        include    = ./machine.ini
        include.ro = ./defaults.ini
    """)

    merged = load_config_tree(main)

    assert merged[("ui", "width")] == ("1280", rw)  # include 赢
    assert merged[("ui", "height")] == ("1", ro)  # 只在 ro 里定义


def test_origin_tracks_last_writer() -> None:
    d = _tmpdir()
    ro = _write(d / "defaults.ini", "[ui]\nwidth = 1\n")
    _write(d / "profile.d" / "a.ini", "[ui]\nwidth = 10\n")
    main = _write(d / "main.ini", """
        [configue]
        include    = ./profile.d/*.ini, ./machine.ini
        include.ro = ./defaults.ini

        [ui]
        height = 720
    """)
    _write(d / "machine.ini", "[ui]\nwidth = 5000\n")

    merged = load_config_tree(main)

    assert merged[("ui", "width")][1] == d / "machine.ini"
    assert merged[("ui", "height")][1] == main.resolve()


# ══════════════════════════════════════════════════════════════
# 忽略 / 剔除
# ══════════════════════════════════════════════════════════════
def test_child_own_include_is_ignored() -> None:
    """扁平结构：子文件的 include 不递归。"""
    d = _tmpdir()
    _write(d / "nested.ini", "[ui]\nwidth = 7777\n")
    _write(d / "machine.ini", """
        [configue]
        include = ./nested.ini

        [ui]
        width = 1280
    """)
    main = _write(d / "main.ini", """
        [configue]
        include = ./machine.ini
    """)

    merged = load_config_tree(main)

    assert merged[("ui", "width")] == ("1280", d / "machine.ini")
    assert ("ui", "width") not in {
        node for node, (value, _) in merged.items() if value == "7777"
    }


def test_include_keys_are_stripped_from_result() -> None:
    d = _tmpdir()
    _write(d / "machine.ini", """
        [configue]
        include = ./ghost.ini

        [ui]
        width = 1
    """)
    main = _write(d / "main.ini", """
        [configue]
        include    = ./machine.ini
        include.ro = ./defaults.ini

        [ui]
        height = 2
    """)
    _write(d / "defaults.ini", "[ui]\nwidth = 0\n")

    merged = load_config_tree(main)

    assert ("configue", "include") not in merged
    assert ("configue", "include.ro") not in merged


def test_other_configue_keys_are_kept() -> None:
    d = _tmpdir()
    main = _write(d / "main.ini", """
        [configue]
        disable_type_check = true
        include = ./machine.ini
    """)
    _write(d / "machine.ini", "[ui]\nwidth = 1\n")

    merged = load_config_tree(main)

    assert merged[("configue", "disable_type_check")] == ("true", main.resolve())


# ══════════════════════════════════════════════════════════════
# 失败语义
# ══════════════════════════════════════════════════════════════
def test_include_cycle_detected() -> None:
    d = _tmpdir()
    main = _write(d / "main.ini", """
        [configue]
        include = ./main.ini
    """)

    try:
        load_config_tree(main)
    except ConfigError as exc:
        assert exc.source is ConfigueErrorSource.FILE
        assert exc.key == "include"
        assert "cycle" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ConfigError for include cycle")


def test_missing_include_file_raises() -> None:
    d = _tmpdir()
    main = _write(d / "main.ini", """
        [configue]
        include = ./nope.ini
    """)

    try:
        load_config_tree(main)
    except ConfigError as exc:
        assert exc.source is ConfigueErrorSource.FILE
        assert "not found" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ConfigError for missing include")


def test_missing_include_ro_file_raises() -> None:
    d = _tmpdir()
    main = _write(d / "main.ini", """
        [configue]
        include.ro = ./nope.ini
    """)

    try:
        load_config_tree(main)
    except ConfigError as exc:
        assert exc.key == "include.ro"
    else:  # pragma: no cover
        raise AssertionError("expected ConfigError for missing include.ro")


def test_glob_without_match_is_not_an_error() -> None:
    d = _tmpdir()
    main = _write(d / "main.ini", """
        [configue]
        include = ./profile.d/*.ini

        [ui]
        width = 1
    """)
    (d / "profile.d").mkdir()

    merged = load_config_tree(main)

    assert merged[("ui", "width")] == ("1", main.resolve())


def test_pkg_scheme_is_rejected() -> None:
    d = _tmpdir()
    main = _write(d / "main.ini", """
        [configue]
        include.ro = pkg://albuswall/defaults.ini
    """)

    try:
        load_config_tree(main)
    except ConfigError as exc:
        assert "pkg://" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ConfigError for pkg:// include")


def test_missing_main_file_returns_empty() -> None:
    assert load_config_tree(Path(tempfile.mkdtemp()) / "absent.ini") == {}


# ══════════════════════════════════════════════════════════════
# setup_static_config 集成
# ══════════════════════════════════════════════════════════════
def _reset_singleton():
    from albuswall.configue.configue import Configue, StaticConfig
    from albuswall.configue.resolver import Resolver

    config = Configue()
    config.resolver = Resolver()
    config.static = StaticConfig(config.resolver)
    config._last_flush_error = None
    config._default_write_file = None
    config.configure_persistence(autosave="atexit", debounce=0.5)
    return config


def test_setup_static_config_loads_tree() -> None:
    from albuswall.configue import setup_static_config
    from albuswall.core import Container

    d = _tmpdir()
    ro = _write(d / "defaults.ini", "[ui]\nwidth = 1\nheight = 1\n")
    _write(d / "machine.ini", "[ui]\nwidth = 1280\n")
    main = _write(d / "main.ini", """
        [configue]
        include    = ./machine.ini
        include.ro = ./defaults.ini
    """)

    config = _reset_singleton()
    setup_static_config(Container(), main)

    # 没有 ConfigField 声明的 key 无转换器 → 保持 raw 字符串
    assert config.static.ui.width == "1280"
    assert config.static.ui.height == "1"
    assert config.resolver.origin_of("ui", "width") == d / "machine.ini"
    assert config.resolver.origin_of("ui", "height") == ro
    # include.ro 来源被标记为只读
    assert config.resolver.is_readonly_file(ro)
    # [configue] include 系不进 Resolver
    assert ("configue", "include") not in config.resolver
    assert ("configue", "include.ro") not in config.resolver
    # 默认写回落点 = 主文件
    assert config._default_write_file == main


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
