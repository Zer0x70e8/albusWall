#
"""多文件加载 + 回写的补丁用例（``test_bootstrap_include`` / ``test_flush`` 的补充）。

覆盖本轮的修复（任务一 4 项正确性 + 任务二 2 项健壮性）：

  1. ``_write_file`` 缺父目录创建 → 新文件 + 新目录也能 flush
  2. ``DynamicConfig.__getattr__`` 工厂失败后 lazy 项保留 → 可重试
  3. ``_expand_include_spec`` glob 展开结果过滤目录
  4. ``bootstrap._resolve_path`` 抛带 base_key 的 ``ConfigError``
     （``@dynamic_state`` 的 ``DynamicMountSpec.resolve_path`` 复用它）
  5. ``load_config_tree_ex`` 的 include 列表按 resolve() 去重
  6. ``Configue.flush(only=...)`` 校验 node 存在

外加端到端行为：origin 归属、只读回退、glob、CRLF、target 指定等。

用 pytest 的 ``tmp_path`` fixture；没装 pytest 时
``python test/test_bootstrap_multifile.py`` 也能跑（自己造 tmp_path）。
"""

from __future__ import annotations

import contextlib
import inspect
import sys
import tempfile
import traceback
from pathlib import Path
from textwrap import dedent

try:  # 允许直接以文件路径运行
    import albuswall  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from albuswall.configue.bootstrap import load_config_tree, load_config_tree_ex
from albuswall.configue.exceptions import ConfigError, ConfigueErrorSource

# ══════════════════════════════════════════════════════════════
# 工具
# ══════════════════════════════════════════════════════════════
def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dedent(text).lstrip("\n"), encoding="utf-8")
    return path


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _singleton():
    from albuswall.configue.configue import Configue

    return Configue()


#: Configue 是进程级单例（``Configue._instance``），``_reset_config`` 会把它的
#: resolver / static 换掉。别的测试文件（如 test_window_state_persistence）在
#: import 期就把单例抓成模块级 CONFIG，所以这里必须「用完即还原」，
#: 否则本文件会把后续用例的静态配置污染掉。
_STATE_ATTRS = (
    "resolver", "static", "_default_write_file", "_persistence",
    "_debounce_interval", "_last_flush_error",
)


@contextlib.contextmanager
def _isolated_config():
    """快照单例的可变状态，退出时原样还原。"""
    config = _singleton()
    snapshot = {name: getattr(config, name) for name in _STATE_ATTRS}
    try:
        yield config
    finally:
        config._cancel_debounce()
        for name, value in snapshot.items():
            setattr(config, name, value)


def _reset_config():
    """把 Configue 单例重置成干净状态（isolated resolver + 默认策略）。"""
    from albuswall.configue.configue import StaticConfig
    from albuswall.configue.resolver import Resolver

    config = _singleton()
    config.resolver = Resolver()
    config.static = StaticConfig(config.resolver)
    config._last_flush_error = None
    config._default_write_file = None
    config.configure_persistence(autosave="atexit", debounce=0.5)
    return config


def _load(main: Path):
    """重置单例并跑 setup_static_config（default_write_file 会被设成 main）。"""
    from albuswall.configue import setup_static_config
    from albuswall.core import Container

    config = _reset_config()
    setup_static_config(Container(), main)
    return config


def _raises_config_error(fn):
    """断言 fn() 抛 ConfigError(source=FILE) 并返回该异常。"""
    try:
        fn()
    except ConfigError as exc:
        assert exc.source is ConfigueErrorSource.FILE
        return exc
    raise AssertionError("expected ConfigError")


try:
    import pytest
except ImportError:  # pragma: no cover
    pytest = None

if pytest is not None:
    @pytest.fixture(autouse=True)
    def _isolate_config_state():
        """每个用例前后还原单例状态，避免污染同进程的其它测试文件。"""
        with _isolated_config():
            yield


# ══════════════════════════════════════════════════════════════
# 1. 主文件不存在 / include 目标不存在
# ══════════════════════════════════════════════════════════════
def test_missing_main_file_returns_empty(tmp_path: Path) -> None:
    assert load_config_tree(tmp_path / "absent.ini") == {}


def test_include_target_missing_raises(tmp_path: Path) -> None:
    main = _write(tmp_path / "main.ini", """
        [configue]
        include = ./nowhere.ini
    """)

    exc = _raises_config_error(lambda: load_config_tree(main))
    assert exc.key == "include"
    assert "not found" in str(exc)


# ══════════════════════════════════════════════════════════════
# 2. origin 归属
# ══════════════════════════════════════════════════════════════
def test_include_overrides_include_ro_origin(tmp_path: Path) -> None:
    """include.ro 与 include 都定义同一 node → origin 是 include 文件。"""
    _write(tmp_path / "defaults.ini", "[ui]\nwidth = 1\n")
    rw = _write(tmp_path / "machine.ini", "[ui]\nwidth = 1280\n")
    main = _write(tmp_path / "main.ini", """
        [configue]
        include    = ./machine.ini
        include.ro = ./defaults.ini
    """)

    merged = load_config_tree(main)

    assert merged[("ui", "width")][1] == rw


def test_readonly_node_flush_goes_to_default_write_file(tmp_path: Path) -> None:
    """include.ro 定义 + set + flush → 写主文件，ro 文件一个字节都不变。"""
    ro = _write(tmp_path / "defaults.ini", "[ui]\nwidth = 1\n")
    main = _write(tmp_path / "main.ini", """
        [configue]
        include.ro = ./defaults.ini
    """)
    config = _load(main)
    before = _text(ro)

    config.set("ui", "width", 999)
    written = config.flush()

    assert written == [main.resolve()]
    assert _text(ro) == before, "include.ro 文件不许被改写"
    assert "width = 999" in _text(main)


def test_writable_include_over_readonly_is_flushed(tmp_path: Path) -> None:
    """ro + 可写 include 都定义 → flush 写 include 文件（不是 default）。"""
    ro = _write(tmp_path / "defaults.ini", "[ui]\nwidth = 1\n")
    rw = _write(tmp_path / "machine.ini", "[ui]\nwidth = 2\n")
    main = _write(tmp_path / "main.ini", """
        [configue]
        include    = ./machine.ini
        include.ro = ./defaults.ini
    """)
    config = _load(main)
    ro_before = _text(ro)

    config.set("ui", "width", 42)
    written = config.flush()

    assert written == [rw]
    assert "width = 42" in _text(rw)
    assert _text(ro) == ro_before
    assert "width" not in _text(main)


# ══════════════════════════════════════════════════════════════
# 3. glob
# ══════════════════════════════════════════════════════════════
def test_glob_without_match_is_not_an_error(tmp_path: Path) -> None:
    main = _write(tmp_path / "main.ini", """
        [configue]
        include = ./profile.d/*.ini

        [ui]
        width = 1
    """)
    (tmp_path / "profile.d").mkdir()

    merged = load_config_tree(main)

    assert merged[("ui", "width")] == ("1", main.resolve())


def test_glob_skips_matched_directories(tmp_path: Path) -> None:
    """glob 把目录也匹配进来时只合并文件（旧行为会 RuntimeError: suffix）。"""
    _write(tmp_path / "profile.d" / "a.ini", "[ui]\nwidth = 10\n")
    (tmp_path / "profile.d" / "sub").mkdir(parents=True)

    main = _write(tmp_path / "main.ini", """
        [configue]
        include = ./profile.d/*
    """)

    merged = load_config_tree(main)

    assert merged[("ui", "width")] == ("10", tmp_path / "profile.d" / "a.ini")


# ══════════════════════════════════════════════════════════════
# 4. 循环 / 去重
# ══════════════════════════════════════════════════════════════
def test_main_including_itself_raises(tmp_path: Path) -> None:
    main = _write(tmp_path / "main.ini", """
        [configue]
        include = ./main.ini
    """)

    exc = _raises_config_error(lambda: load_config_tree(main))
    assert "cycle" in str(exc)


def test_duplicate_include_entries_are_deduped(tmp_path: Path) -> None:
    """``a.ini`` 与 ``./a.ini`` 是同一文件 → 只合并一次。"""
    child = _write(tmp_path / "a.ini", "[ui]\nwidth = 10\n")
    main = _write(tmp_path / "main.ini", """
        [configue]
        include = a.ini, ./a.ini
    """)

    merged, readonly = load_config_tree_ex(main)

    assert merged[("ui", "width")] == ("10", child)
    assert readonly == []


def test_duplicate_include_ro_entries_are_deduped(tmp_path: Path) -> None:
    child = _write(tmp_path / "defaults.ini", "[ui]\nwidth = 1\n")
    main = _write(tmp_path / "main.ini", """
        [configue]
        include.ro = ./defaults.ini, defaults.ini
    """)

    config = _load(main)

    assert config.resolver.definitions_of("ui", "width") == (child,)


# ══════════════════════════════════════════════════════════════
# 5. 换行符 / 新目录 / target
# ══════════════════════════════════════════════════════════════
def test_crlf_preserved_through_flush(tmp_path: Path) -> None:
    main = tmp_path / "main.ini"
    main.write_bytes(b"[ui]\r\nwidth = 1\r\n")
    config = _load(main)

    config.set("ui", "height", 2)
    config.flush()

    assert main.read_bytes() == b"[ui]\r\nwidth = 1\r\nheight = 2\r\n"


def test_new_key_without_origin_goes_to_default_write_file(
        tmp_path: Path,
) -> None:
    main = _write(tmp_path / "main.ini", "[ui]\nwidth = 1\n")
    config = _load(main)

    config.set("ui", "height", 2)  # 运行期新 key，origin 依旧 None
    assert config.resolver.origin_of("ui", "height") is None

    written = config.flush()

    assert written == [main.resolve()]
    assert "height = 2" in _text(main)


def test_set_with_explicit_target_is_flushed(tmp_path: Path) -> None:
    main = _write(tmp_path / "main.ini", "[ui]\nwidth = 1\n")
    target = tmp_path / "elsewhere.ini"
    config = _load(main)

    config.set("ui", "width", 42, target=target)
    written = config.flush()

    assert written == [target]
    assert "width = 42" in _text(target)
    assert "width = 1\n" in _text(main)


def test_flush_creates_missing_parent_directories(tmp_path: Path) -> None:
    """任务一-1：default_write_file 指向不存在的新目录 → flush 建目录再写。"""
    main = _write(tmp_path / "main.ini", "[ui]\nwidth = 1\n")
    target = tmp_path / "deep" / "nested" / "extra.ini"
    config = _load(main)
    config.configure_persistence(default_write_file=target)

    config.set("ui", "brand_new", "v")
    written = config.flush()

    assert written == [target]
    assert target.exists()
    assert "brand_new = v" in _text(target)


# ══════════════════════════════════════════════════════════════
# 6. flush(only=...) 校验
# ══════════════════════════════════════════════════════════════
def test_flush_only_unknown_node_raises(tmp_path: Path) -> None:
    main = _write(tmp_path / "main.ini", "[ui]\nwidth = 1\n")
    config = _load(main)

    exc = _raises_config_error(lambda: config.flush(only=[("nope", "x")]))
    assert exc.key == "[nope]x"
    assert "unknown nodes" in str(exc)


def test_flush_only_known_node_still_works(tmp_path: Path) -> None:
    main = _write(tmp_path / "main.ini", "[ui]\nwidth = 1\nheight = 1\n")
    config = _load(main)

    config.set("ui", "width", 7)
    config.set("ui", "height", 8)
    written = config.flush(only=[("ui", "width")])

    assert written == [main.resolve()]
    assert "width = 7" in _text(main)
    assert "height = 1" in _text(main)


# ══════════════════════════════════════════════════════════════
# 7. dynamic：base_key 报错 / lazy 工厂重试
# ══════════════════════════════════════════════════════════════
def test_dynamic_state_bad_base_key_raises_config_error(
        tmp_path: Path, monkeypatch,
) -> None:
    """任务一-4：base_key 打错 → ConfigError 且消息里有完整 base_key。"""
    import albuswall.configue.declaration as declaration
    from albuswall.configue.dynamic_declaration import (
        DYNAMIC_REGISTRY, dynamic_state,
    )
    from albuswall.configue.state import StatefulNamespace, StateField

    @dynamic_state("bad_base_key_probe", ("nope", "x"), "probe.json",
                   replace=True)
    class _Probe(StatefulNamespace):
        flag = StateField(bool, default=False)

    # 只有默认内置 key 的 StaticConfig（没有 path.cache 之外的 nope.x）
    static = _reset_config().static
    monkeypatch.setattr(
        declaration, "get_config", lambda: type("C", (), {"static": static})()
    )

    config = _reset_config()
    try:
        exc = _raises_config_error(
            lambda: getattr(config.dynamic, "bad_base_key_probe")
        )
        message = str(exc)
        assert "base_key" in message
        assert "nope" in message and "x" in message
    finally:
        DYNAMIC_REGISTRY.pop("bad_base_key_probe", None)


def test_mount_lazy_factory_failure_is_retryable(tmp_path: Path) -> None:
    """任务一-2：工厂抛错后 lazy 项仍在 _lazy 里，下次访问重新调用。"""
    from albuswall.configue.configue import DynamicConfig
    from albuswall.configue.utils.namespace import Namespace

    config = _reset_config()
    dynamic = config.dynamic
    assert isinstance(dynamic, DynamicConfig)

    calls: list[int] = []

    def factory():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return Namespace(ok=True)

    dynamic.mount_lazy("flaky_probe", factory)

    try:
        getattr(dynamic, "flaky_probe")
    except RuntimeError as exc:
        assert "boom" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected the factory error to propagate")

    # 关键断言：工厂仍在 _lazy 里，没有被 pop 掉
    assert "flaky_probe" in dynamic.__dict__["_lazy"]
    assert len(calls) == 1

    node = getattr(dynamic, "flaky_probe")  # 重试 → 成功
    assert node.ok is True
    assert len(calls) == 2
    assert "flaky_probe" not in dynamic.__dict__["_lazy"]
    assert dynamic.__dict__["flaky_probe"] is node
    dynamic.unmount("flaky_probe")


# ══════════════════════════════════════════════════════════════
# 入口（无 pytest 时直接跑）
# ══════════════════════════════════════════════════════════════
class _MonkeyPatch:
    """极简 monkeypatch（无 pytest 时用）。"""

    def __init__(self) -> None:
        self._undo: list[tuple[object, str, object]] = []

    def setattr(self, target, name, value) -> None:
        self._undo.append((target, name, getattr(target, name)))
        setattr(target, name, value)

    def undo(self) -> None:
        for target, name, old in reversed(self._undo):
            setattr(target, name, old)
        self._undo.clear()


def main() -> int:
    tests = [
        (name, obj)
        for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    failures: list[tuple[str, BaseException]] = []
    for name, fn in tests:
        kwargs: dict[str, object] = {}
        params = inspect.signature(fn).parameters
        if "tmp_path" in params:
            kwargs["tmp_path"] = Path(tempfile.mkdtemp(prefix="albuswall-mf-"))
        patch = None
        if "monkeypatch" in params:
            patch = _MonkeyPatch()
            kwargs["monkeypatch"] = patch
        try:
            with _isolated_config():
                fn(**kwargs)
        except BaseException as exc:  # noqa: BLE001
            failures.append((name, exc))
            print(f"FAIL {name}: {exc}")
            traceback.print_exc()
        else:
            print(f"ok   {name}")
        finally:
            if patch is not None:
                patch.undo()

    print()
    if failures:
        print(f"{len(failures)}/{len(tests)} 个用例失败")
        return 1
    print(f"全部 {len(tests)} 个用例通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
