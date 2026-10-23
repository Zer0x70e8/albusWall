#
"""Resolver 的 origin / dirty / 只读标记 / 锁契约。

覆盖阶段 1（不改任何公开 API 的增量改造）：

- ``inject_raw(..., origin=p)`` 记录 origin，可反查 ``nodes_of_file``
- ``set`` 标脏、``mark_clean`` 清脏
- ``set(persist=False)`` 不标脏
- 同名 key 多次注入：生效 origin 归最高优先级文件，``definitions_of`` 全保留
- 运行期 ``set_origin`` 迁移文件归属
- 只读标记
- ``get_typed`` / ``invalidate`` 在锁下并发调用不崩

不依赖 pytest：直接 ``python test/test_resolver_origin.py`` 运行，
装了 pytest 的话 ``pytest test/test_resolver_origin.py`` 也能跑。
"""

from __future__ import annotations

import sys
import threading
import traceback
from pathlib import Path

try:  # 允许直接以文件路径运行
    import albuswall  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from albuswall.configue.resolver import Resolver

# ══════════════════════════════════════════════════════════════
# origin 追踪
# ══════════════════════════════════════════════════════════════
def test_inject_raw_records_origin() -> None:
    resolver = Resolver()
    origin = Path("/tmp/configue/a.ini")

    resolver.inject_raw("ui", "width", "1", origin=origin)

    assert resolver.origin_of("ui", "width") == origin
    assert resolver.nodes_of_file(origin) == {("ui", "width")}
    assert resolver.definitions_of("ui", "width") == (origin,)


def test_inject_raw_without_origin_is_compatible() -> None:
    """老调用方式（3 个位置参数）必须继续工作，且不产生 origin。"""
    resolver = Resolver()
    resolver.inject_raw("ui", "width", "1")

    assert resolver.get_str("ui", "width") == "1"
    assert resolver.origin_of("ui", "width") is None
    assert resolver.definitions_of("ui", "width") == ()


def test_origin_follows_effective_value() -> None:
    """低 → 高优先级注入：生效 origin 归最后一次（未 overwrite 时的先到者）。"""
    low = Path("/tmp/configue/defaults.ini")
    high = Path("/tmp/configue/main.ini")

    resolver = Resolver()
    resolver.inject_raw("ui", "width", "1", origin=low)
    resolver.inject_raw("ui", "width", "2", origin=high)  # 不覆盖

    assert resolver.get_str("ui", "width") == "1"
    assert resolver.origin_of("ui", "width") == low
    assert resolver.definitions_of("ui", "width") == (low, high), (
        "fallback 需要知道所有定义过该 key 的文件"
    )
    # 反查表只反映生效 origin
    assert resolver.nodes_of_file(high) == set()


def test_inject_raw_overwrite_moves_origin() -> None:
    low = Path("/tmp/configue/defaults.ini")
    high = Path("/tmp/configue/main.ini")

    resolver = Resolver()
    resolver.inject_raw("ui", "width", "1", origin=low)
    resolver.inject_raw("ui", "width", "2", origin=high, overwrite=True)

    assert resolver.get_str("ui", "width") == "2"
    assert resolver.origin_of("ui", "width") == high
    assert resolver.nodes_of_file(low) == set()
    assert resolver.nodes_of_file(high) == {("ui", "width")}


def test_set_origin_rebinds_file() -> None:
    first = Path("/tmp/configue/main.ini")
    second = Path("/tmp/configue/extra.ini")

    resolver = Resolver()
    resolver.inject_raw("ui", "width", "1", origin=first)
    resolver.set_origin("ui", "width", second)

    assert resolver.origin_of("ui", "width") == second
    assert resolver.nodes_of_file(first) == set()
    assert resolver.nodes_of_file(second) == {("ui", "width")}


# ══════════════════════════════════════════════════════════════
# dirty
# ══════════════════════════════════════════════════════════════
def test_set_marks_dirty_and_mark_clean_clears() -> None:
    resolver = Resolver()
    resolver.inject_raw("ui", "width", "1")

    assert resolver.dirty_nodes() == frozenset()

    resolver.set("ui", "width", 2)
    assert resolver.dirty_nodes() == frozenset({("ui", "width")})
    assert resolver.get_typed("ui", "width") == 2

    resolver.mark_clean([("ui", "width")])
    assert resolver.dirty_nodes() == frozenset()


def test_set_persist_false_does_not_mark_dirty() -> None:
    resolver = Resolver()
    resolver.inject_raw("ui", "width", "1")

    resolver.set("ui", "width", 2, persist=False)

    assert resolver.dirty_nodes() == frozenset()
    assert resolver.get_typed("ui", "width") == 2


def test_dirty_nodes_is_a_snapshot() -> None:
    resolver = Resolver()
    resolver.set("ui", "width", 1)
    snapshot = resolver.dirty_nodes()

    resolver.set("ui", "height", 2)

    assert snapshot == frozenset({("ui", "width")})
    assert isinstance(snapshot, frozenset)


def test_mark_clean_accepts_lists_and_tuples() -> None:
    resolver = Resolver()
    resolver.set("ui", "width", 1)
    resolver.set("ui", "height", 2)

    resolver.mark_clean([["ui", "width"], ("ui", "height")])

    assert resolver.dirty_nodes() == frozenset()


# ══════════════════════════════════════════════════════════════
# 只读标记
# ══════════════════════════════════════════════════════════════
def test_readonly_flag_roundtrip() -> None:
    resolver = Resolver()
    path = Path("/tmp/configue/defaults.ini")

    assert not resolver.is_readonly_file(path)
    resolver.mark_readonly_file(path)
    assert resolver.is_readonly_file(path)


def test_converter_of_returns_registered_type() -> None:
    resolver = Resolver()
    resolver.register_type("ui", "width", int)

    assert resolver.converter_of("ui", "width") is int
    assert resolver.converter_of("ui", "missing") is None


# ══════════════════════════════════════════════════════════════
# 并发
# ══════════════════════════════════════════════════════════════
def test_concurrent_set_and_get_typed() -> None:
    resolver = Resolver()
    resolver.inject_raw("ui", "width", "0")
    resolver.inject_raw("ui", "height", "0")
    resolver.register_type("ui", "width", int)
    resolver.register_type("ui", "height", int)

    errors: list[BaseException] = []
    barrier = threading.Barrier(8)

    def worker(i: int) -> None:
        try:
            barrier.wait()
            for n in range(200):
                resolver.set("ui", "width", i * 1000 + n)
                resolver.get_typed("ui", "width")
                resolver.set("ui", "height", n)
                resolver.get_typed("ui", "height")
                resolver.mark_clean([("ui", "height")])
                resolver.inject_raw("ui", f"k{n}", str(n), overwrite=True)
                _ = resolver.sections()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, errors
    assert not any(t.is_alive() for t in threads)


def test_interpolation_still_works_under_lock() -> None:
    """RLock 是必需的：get_str 展开 ${...} 时会递归调用自身。"""
    resolver = Resolver()
    resolver.inject_raw("path", "base", "/data")
    resolver.inject_raw("path", "cache", "${base}/cache")

    assert resolver.get_str("path", "cache") == "/data/cache"


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
