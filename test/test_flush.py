#
"""``Configue.set`` / ``flush`` / ``configure_persistence``：运行期回写。

覆盖阶段 4（PR4 里程碑）：

  1. 单文件：set 两个 key → flush → 内容更新，注释 / 前缀格式保留
  2. 多文件：属于两个 origin 的 key → 各自写回自己的文件
  3. 只读 origin → 回退 default_write_file
  4. 新 key（无 origin）→ 进 default_write_file；[section] 不存在则新建
  5. 失败注入：atomic_write 抛 OSError → 非 strict 部分成功 + last_flush_error
     + dirty 保留；strict 抛 ConfigError
  6. debounce：连续 10 次 set → 只写一次文件
  7. atexit：_atexit_flush 落盘；manual 策略下不落盘
  8. save="now" 立即写；save="defer" 只标脏
  9. CRLF 换行符保留

不依赖 pytest：直接 ``python test/test_flush.py`` 运行，
装了 pytest 的话 ``pytest test/test_flush.py`` 也能跑。
"""

from __future__ import annotations

import sys
import tempfile
import time
import traceback
from pathlib import Path
from textwrap import dedent

try:  # 允许直接以文件路径运行
    import albuswall  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from albuswall.configue.exceptions import ConfigError, ConfigueErrorSource

# ══════════════════════════════════════════════════════════════
# 工具
# ══════════════════════════════════════════════════════════════
def _tmpdir() -> Path:
    return Path(tempfile.mkdtemp(prefix="albuswall-flush-"))


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dedent(text).lstrip("\n"), encoding="utf-8")
    return path


def _reset_config():
    """把 Configue 单例重置成干净状态（isolated resolver + 策略）。"""
    from albuswall.configue.configue import Configue, StaticConfig
    from albuswall.configue.resolver import Resolver

    config = Configue()
    config.resolver = Resolver()
    config.static = StaticConfig(config.resolver)
    config._last_flush_error = None
    config._default_write_file = None
    config.configure_persistence(autosave="atexit", debounce=0.5)
    return config


def _load(main: Path):
    """重置单例并跑 setup_static_config（同时设定 default_write_file=main）。"""
    from albuswall.configue import setup_static_config
    from albuswall.core import Container

    config = _reset_config()
    setup_static_config(Container(), main)
    return config


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ══════════════════════════════════════════════════════════════
# 单文件
# ══════════════════════════════════════════════════════════════
def test_single_file_update_keeps_comments_and_format() -> None:
    d = _tmpdir()
    main = _write(d / "main.ini", """
        # header comment
        [ui]
        width = 100
        height : 200

        ; tail comment
        [other]
        a = 1
    """)
    config = _load(main)

    config.set("ui", "width", 255)
    config.set("ui", "height", 300)
    written = config.flush()

    assert written == [main]
    assert _text(main) == dedent("""
        # header comment
        [ui]
        width = 255
        height : 300

        ; tail comment
        [other]
        a = 1
    """).lstrip("\n")
    assert config.resolver.dirty_nodes() == frozenset()


def test_crlf_is_preserved() -> None:
    d = _tmpdir()
    main = d / "main.ini"
    main.write_bytes(b"[ui]\r\na = 1\r\n")
    config = _load(main)

    config.set("ui", "b", 2)
    config.flush()

    assert main.read_bytes() == b"[ui]\r\na = 1\r\nb = 2\r\n"


# ══════════════════════════════════════════════════════════════
# 多文件
# ══════════════════════════════════════════════════════════════
def test_multi_file_writes_each_origin() -> None:
    d = _tmpdir()
    machine = _write(d / "machine.ini", "[ui]\nwidth = 100\n")
    main = _write(d / "main.ini", """
        [configue]
        include = ./machine.ini

        [ui]
        height = 200
    """)
    config = _load(main)

    config.set("ui", "width", 1111)  # origin = machine.ini
    config.set("ui", "height", 2222)  # origin = main.ini
    written = config.flush()

    assert set(written) == {machine, main}
    assert _text(machine) == "[ui]\nwidth = 1111\n"
    assert "height = 2222" in _text(main)
    assert "width" not in _text(main).split("[ui]")[-1]


def test_readonly_origin_falls_back_to_default_write_file() -> None:
    d = _tmpdir()
    ro = _write(d / "defaults.ini", "[ui]\nwidth = 1\n")
    main = _write(d / "main.ini", """
        [configue]
        include.ro = ./defaults.ini
    """)
    config = _load(main)

    assert config.resolver.is_readonly_file(ro)

    config.set("ui", "width", 999)
    written = config.flush()

    assert written == [main]
    assert _text(ro) == "[ui]\nwidth = 1\n", "只读来源不许被改写"
    assert "width = 999" in _text(main)


def test_new_key_and_new_section_go_to_default_file() -> None:
    d = _tmpdir()
    main = _write(d / "main.ini", "[ui]\nwidth = 1\n")
    config = _load(main)

    config.set("ui", "height", 2)  # 已在 [ui] 里的新 key
    config.set("brand", "new", "v")  # 全新 section
    written = config.flush()

    assert written == [main]
    text = _text(main)
    assert "height = 2" in text
    assert "[brand]" in text
    assert "new = v" in text


# ══════════════════════════════════════════════════════════════
# 失败注入
# ══════════════════════════════════════════════════════════════
class _FailingAtomicWrite:
    """替换 ``albuswall.infrastructure.fs.atomic_write``：指定 dest 抛 OSError。"""

    def __init__(self, fail_for: Path) -> None:
        import albuswall.infrastructure.fs as fs_mod

        self._fs_mod = fs_mod
        self._original = fs_mod.atomic_write
        self._fail_for = fail_for

    def __enter__(self):
        original = self._original
        fail_for = self._fail_for

        def patched(dest, writer):
            if Path(dest) == fail_for:
                raise OSError("disk full (injected)")
            return original(dest, writer)

        self._fs_mod.atomic_write = patched
        return self

    def __exit__(self, *exc_info):
        self._fs_mod.atomic_write = self._original
        return False


def test_failure_injection_non_strict() -> None:
    d = _tmpdir()
    machine = _write(d / "machine.ini", "[ui]\nwidth = 1\n")
    main = _write(d / "main.ini", """
        [configue]
        include = ./machine.ini

        [ui]
        height = 2
    """)
    config = _load(main)

    config.set("ui", "width", 11)
    config.set("ui", "height", 22)
    assert config.resolver.dirty_nodes() == frozenset(
        {("ui", "width"), ("ui", "height")}
    )

    with _FailingAtomicWrite(fail_for=main):
        written = config.flush()

    # 部分成功：machine.ini 写成功，main.ini 失败
    assert written == [machine]
    error = config.last_flush_error
    assert isinstance(error, ConfigError)
    assert error.source is ConfigueErrorSource.FILE
    assert isinstance(error.cause, OSError)
    # 失败节点保持 dirty，可供下次重试
    assert config.resolver.dirty_nodes() == frozenset({("ui", "height")})

    # 恢复后重试即可成功
    assert config.flush() == [main]
    assert config.resolver.dirty_nodes() == frozenset()


def test_failure_injection_strict_raises() -> None:
    d = _tmpdir()
    main = _write(d / "main.ini", "[ui]\nheight = 2\n")
    config = _load(main)
    config.set("ui", "height", 22)

    with _FailingAtomicWrite(fail_for=main):
        try:
            config.flush(strict=True)
        except ConfigError as exc:
            assert exc.source is ConfigueErrorSource.FILE
            assert exc.key
        else:  # pragma: no cover
            raise AssertionError("expected ConfigError in strict mode")

    assert config.resolver.dirty_nodes() == frozenset({("ui", "height")})


# ══════════════════════════════════════════════════════════════
# autosave 策略
# ══════════════════════════════════════════════════════════════
def test_debounce_coalesces_high_frequency_sets() -> None:
    d = _tmpdir()
    main = _write(d / "main.ini", "[ui]\nwidth = 0\n")
    config = _load(main)

    calls: list[Path] = []
    original_write = type(config)._write_file

    def counting_write(self, file, edits):
        calls.append(Path(file))
        return original_write(self, file, edits)

    type(config)._write_file = counting_write
    try:
        config.configure_persistence(autosave="debounce", debounce=0.05)
        for i in range(10):
            config.set("ui", "width", i)

        deadline = time.monotonic() + 5.0
        while not calls and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.2)  # 给可能的第二个定时器机会暴露
    finally:
        config.configure_persistence(autosave="atexit")
        type(config)._write_file = original_write

    assert len(calls) == 1, f"expected a single debounced write, got {len(calls)}"
    assert "width = 9" in _text(main)


def test_atexit_flush_writes_pending() -> None:
    d = _tmpdir()
    main = _write(d / "main.ini", "[ui]\nwidth = 1\n")
    config = _load(main)

    config.configure_persistence(autosave="atexit")
    config.set("ui", "width", 42)
    assert config.resolver.dirty_nodes() == frozenset({("ui", "width")})

    config._atexit_flush()

    assert "width = 42" in _text(main)
    assert config.resolver.dirty_nodes() == frozenset()


def test_manual_policy_never_writes_implicitly() -> None:
    d = _tmpdir()
    main = _write(d / "main.ini", "[ui]\nwidth = 1\n")
    config = _load(main)

    config.configure_persistence(autosave="manual")
    config.set("ui", "width", 42)
    config._atexit_flush()  # manual 下退出也不写

    assert "width = 1" in _text(main)
    assert config.resolver.dirty_nodes() == frozenset({("ui", "width")})

    config.flush()  # 显式 flush 才写
    assert "width = 42" in _text(main)


def test_save_now_and_defer() -> None:
    d = _tmpdir()
    main = _write(d / "main.ini", "[ui]\nwidth = 1\nheight = 1\n")
    config = _load(main)
    config.configure_persistence(autosave="manual")

    config.set("ui", "width", 7, save="now")
    assert "width = 7" in _text(main)
    assert config.resolver.dirty_nodes() == frozenset()

    config.set("ui", "height", 8, save="defer")
    assert "height = 1" in _text(main)
    assert config.resolver.dirty_nodes() == frozenset({("ui", "height")})


# ══════════════════════════════════════════════════════════════
# 其它
# ══════════════════════════════════════════════════════════════
def test_flush_only_subset() -> None:
    d = _tmpdir()
    main = _write(d / "main.ini", "[ui]\na = 1\nb = 2\n")
    config = _load(main)

    config.set("ui", "a", 10)
    config.set("ui", "b", 20)

    written = config.flush(only=[("ui", "a")])

    assert written == [main]
    text = _text(main)
    assert "a = 10" in text
    assert "b = 2" in text
    assert config.resolver.dirty_nodes() == frozenset({("ui", "b")})


def test_flush_with_no_dirty_nodes_is_noop() -> None:
    d = _tmpdir()
    main = _write(d / "main.ini", "[ui]\na = 1\n")
    config = _load(main)

    assert config.flush() == []
    assert config.last_flush_error is None


def test_typed_value_roundtrip_through_file() -> None:
    d = _tmpdir()
    main = _write(d / "main.ini", "[ui]\nwidth = 1\n")
    config = _load(main)
    config.resolver.register_type("ui", "width", int)
    config.resolver.set("ui", "width", 1)  # 重新以 raw 字符串类型化

    config.set("ui", "width", 255)
    config.flush()

    assert "width = 255" in _text(main)
    assert config.resolver.get_typed("ui", "width") == 255


def test_unknown_save_mode_raises() -> None:
    d = _tmpdir()
    config = _load(_write(d / "main.ini", "[ui]\na = 1\n"))

    try:
        config.set("ui", "a", 2, save="eventually")  # type: ignore[arg-type]
    except ValueError as exc:
        assert "save mode" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")


def test_configure_persistence_validates() -> None:
    d = _tmpdir()
    config = _load(_write(d / "main.ini", "[ui]\na = 1\n"))

    for kwargs in ({"autosave": "always"}, {"debounce": -1}):
        try:
            config.configure_persistence(**kwargs)  # type: ignore[arg-type]
        except ValueError:
            pass
        else:  # pragma: no cover
            raise AssertionError(f"expected ValueError for {kwargs}")


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
