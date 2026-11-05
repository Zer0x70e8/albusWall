#
"""``configue.events``：变更广播（``EventHub`` + ``Configue.subscribe``）。

只测「广播这一层」的契约，不测快捷键：

* 四种来源各发一次事件，``source`` 正确；
* ``flush()`` 不发事件（它只落盘）；
* 订阅者抛异常被隔离，不影响其它订阅者、不冒泡；
* 取消函数幂等，重复 ``subscribe`` 不重复入队；
* 回调里 ``subscribe`` / ``set`` 不丢事件、不死锁、不无限递归；
* ``nodes=`` 过滤；``inject_raw`` 同值重复调用不 emit。

用例自带 ``Configue`` 隔离（登记表是进程全局的，见
``tests/test_configue/conftest.py``），不依赖其它 fixture 的执行顺序。
"""

from __future__ import annotations

from contextlib import suppress
from pathlib import Path

import pytest

from albuswall.configue import Configue, EventHub
from albuswall.configue.registry import (
    _CLASSES,
    _FIELD_OWNER,
    _FILES,
    _PATHS,
    ConfigFile,
    reg,
)

from .conftest import write_ini


@pytest.fixture
def config(tmp_path: Path):
    """一个只有 ``config.ini`` 的最小 ``Configue``。"""
    write_ini(tmp_path / "config.ini", "[core]\ndebug = true\ncount = 3\n")
    reg(ConfigFile.INI, "config.ini", name="main", autosave="manual")
    instance = Configue(base_dir=tmp_path)
    instance.load()
    yield instance
    with suppress(Exception):
        instance.close()
    Configue._instance = None
    for table in (_FILES, _CLASSES, _PATHS, _FIELD_OWNER):
        table.clear()


class Recorder:
    """收集 ``(node, value, source)`` 的订阅者。"""

    def __init__(self) -> None:
        self.events: list[tuple[tuple[str, str], object, str]] = []

    def __call__(self, node, value, source) -> None:
        self.events.append((node, value, source))

    @property
    def sources(self) -> list[str]:
        return [source for _node, _value, source in self.events]


# ==================== EventHub 本体 ====================

def test_emit_filters_by_nodes() -> None:
    hub = EventHub()
    hit = Recorder()
    other = Recorder()
    hub.subscribe(hit, nodes=[("a", "x")])
    hub.subscribe(other)

    hub.emit(("a", "x"), 1, source="set")
    hub.emit(("a", "y"), 2, source="set")

    assert [node for node, _v, _s in hit.events] == [("a", "x")]
    assert len(other.events) == 2


def test_subscribe_accepts_a_single_node_tuple() -> None:
    """``nodes=("s", "k")`` 是最常见的误写，应当按「一个节点」解释。"""
    hub = EventHub()
    recorder = Recorder()
    hub.subscribe(recorder, nodes=("a", "x"))

    hub.emit(("a", "x"), 1, source="set")
    hub.emit(("a", "y"), 2, source="set")

    assert [node for node, _v, _s in recorder.events] == [("a", "x")]


def test_subscribe_is_idempotent_and_unsubscribe_repeatable() -> None:
    hub = EventHub()
    recorder = Recorder()

    first = hub.subscribe(recorder, nodes=[("a", "x")])
    hub.subscribe(recorder, nodes=[("a", "x")])
    assert len(hub) == 1

    first()
    first()          # 幂等：第二次不抛
    assert len(hub) == 0

    hub.emit(("a", "x"), 1, source="set")
    assert recorder.events == []


def test_emit_snapshots_subscribers() -> None:
    """回调里 subscribe / unsubscribe 不能影响本次投递。"""
    hub = EventHub()
    seen: list[str] = []

    def first(node, value, source) -> None:
        seen.append("first")
        hub.subscribe(lambda *a: seen.append("late"))

    second = Recorder()
    hub.subscribe(first)
    hub.subscribe(second)

    hub.emit(("a", "x"), 1, source="set")
    assert seen == ["first"]
    assert len(second.events) == 1

    # 新一轮投递才会带上新订阅者
    hub.emit(("a", "x"), 2, source="set")
    assert seen == ["first", "first", "late"]


def test_one_failing_subscriber_does_not_stop_the_others() -> None:
    hub = EventHub()
    recorder = Recorder()

    def boom(node, value, source) -> None:
        raise RuntimeError("subscriber exploded")

    hub.subscribe(boom)
    hub.subscribe(recorder)

    hub.emit(("a", "x"), 1, source="set")   # 不抛

    assert len(recorder.events) == 1


def test_clear_drops_every_subscriber() -> None:
    hub = EventHub()
    hub.subscribe(Recorder())
    assert len(hub) == 1

    hub.clear()

    assert len(hub) == 0
    assert not hub


# ==================== 四种来源 ====================

def test_set_emits_once(config: Configue) -> None:
    recorder = Recorder()
    config.subscribe(recorder)

    config.resolver.set("core", "debug", False)

    assert recorder.events == [(("core", "debug"), False, "set")]


def test_remove_emits_once(config: Configue) -> None:
    recorder = Recorder()
    config.subscribe(recorder)

    config.resolver.remove("core", "debug")

    assert recorder.events == [(("core", "debug"), None, "remove")]


def test_remove_unknown_node_does_not_emit(config: Configue) -> None:
    """本来就不存在的节点：没有「值变了」可播。"""
    recorder = Recorder()
    config.subscribe(recorder)

    config.resolver.remove("core", "nope")

    assert recorder.events == []


def test_inject_raw_emits_reload_on_change(tmp_path: Path, config: Configue) -> None:
    recorder = Recorder()
    config.subscribe(recorder)

    config.resolver.inject_raw(
        "core", "debug", "false", origin=tmp_path / "config.ini", overwrite=True,
    )

    assert recorder.sources == ["reload"]
    assert recorder.events[0][1] == "false"


def test_inject_raw_same_value_does_not_emit(config: Configue) -> None:
    """``load()`` 重复注入同一个值时不能刷屏。"""
    recorder = Recorder()
    config.subscribe(recorder)

    for _ in range(3):
        config.resolver.inject_raw("core", "debug", "true", overwrite=True)

    assert recorder.events == []


def test_flush_does_not_emit(config: Configue) -> None:
    recorder = Recorder()
    config.subscribe(recorder)
    config.resolver.set("core", "debug", False)
    recorder.events.clear()

    config.flush()

    assert recorder.events == []


def test_repeated_load_emits_nothing_for_unchanged_values(
        tmp_path: Path, config: Configue,
) -> None:
    """重复 ``load()`` 幂等：文件已加载，不重新注入 → 不广播。"""
    recorder = Recorder()
    config.subscribe(recorder)

    config.load()

    assert recorder.events == []


# ==================== 与写路径的关系 ====================

def test_callback_can_set_another_value_without_deadlock(
        config: Configue,
) -> None:
    """回调在锁外执行：回调里再 ``set`` 不会死锁，也不会被吞掉。"""
    seen: list[tuple[str, str]] = []

    def mirror(node, value, source) -> None:
        seen.append(node)
        if node == ("core", "debug"):
            config.resolver.set("core", "mirrored", value)

    config.subscribe(mirror)

    config.resolver.set("core", "debug", False)

    assert seen == [("core", "debug"), ("core", "mirrored")]
    assert config.resolver.get_typed("core", "mirrored") is False


def test_tree_write_emits(config: Configue) -> None:
    """树视图写入也要广播（它最终走 ``resolver.set``）。"""
    recorder = Recorder()
    config.subscribe(recorder)

    config.tree.main["core"]["debug"] = False

    assert recorder.sources == ["set"]
    assert recorder.events[0][0] == ("core", "debug")


def test_unsubscribe_stops_delivery(config: Configue) -> None:
    recorder = Recorder()
    unsubscribe = config.subscribe(recorder)

    config.resolver.set("core", "debug", False)
    unsubscribe()
    config.resolver.set("core", "debug", True)

    assert len(recorder.events) == 1


def test_close_clears_subscribers(config: Configue) -> None:
    config.subscribe(Recorder())
    assert len(config.resolver.events) == 1

    config.close()

    assert len(config.resolver.events) == 0


def test_public_event_hub_is_exported() -> None:
    from albuswall.configue import EventHub as hub_from_package

    assert hub_from_package is EventHub
