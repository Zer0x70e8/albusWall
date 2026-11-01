#
"""``MainLoopService``：服务层写入口的线程边界。

守住四条语义（顺序即 ``call()`` 里的优先级）：

1. 已在该线程 → 直接执行（UI 写路径零开销，且不会排队等自己 → 不自锁）；
2. 没有可用主循环（``None`` / 未实现 ``call_soon`` 的替身循环）→ 同步执行；
3. 其余情况投递到主循环线程并阻塞等待，异常原样带回调用线程；
4. 超时 / 关闭 → 抛 ``MainThreadTimeout`` / ``MainLoopUnavailable``，且
   排队中的回调因 Future 已取消而放弃执行。

这里用**真的** ``HeadlessMainLoop`` 跑在独立线程里，而不是替身：本模块的
全部价值就在"跨线程"三个字上，替身证明不了任何事。
"""

from __future__ import annotations

import threading
import time

import pytest

from albuswall.core.main_loop import HeadlessMainLoop, MainLoop
from albuswall.service.errors import MainLoopUnavailable, MainThreadTimeout
from albuswall.service.main_loop_service import DEFAULT_TIMEOUT, MainLoopService
from tests.conftest import FakeSourceTrashService

_LOOP_NAME = "albuswall-test-loop"


# ==================== 工具 ====================

class _NeverRunsLoop:
    """有 ``call_soon`` 但从不执行：模拟"主循环被阻塞"。

    ``timeout`` 必须小于用例的超时期望，否则用例自己会挂死。
    """

    def __init__(self) -> None:
        self.posted: list = []

    def call_soon(self, fn) -> None:
        self.posted.append(fn)


class _NoCallSoonLoop(MainLoop):
    """只实现 run/quit 的替身（继承到的 ``call_soon`` 抛 NotImplementedError）。"""

    def run(self) -> int:  # pragma: no cover - 本用例不 run
        return 0

    def quit(self, code=None) -> None:  # pragma: no cover
        pass


def _start_loop():
    """起一个真正在跑的 ``HeadlessMainLoop``，返回 (loop, thread)。"""
    loop = HeadlessMainLoop()
    thread = threading.Thread(
        target=loop.run, name=_LOOP_NAME, daemon=True,
    )
    thread.start()
    return loop, thread


def _stop_loop(loop: HeadlessMainLoop, thread: threading.Thread) -> None:
    loop.quit(0)
    thread.join(timeout=2.0)


def _in_thread(fn):
    """在独立线程里执行 ``fn``，返回 (结果/异常, 线程名)。"""
    box: dict = {}

    def _run() -> None:
        box["thread"] = threading.current_thread().name
        try:
            box["result"] = fn()
        except BaseException as exc:  # noqa: BLE001 —— 用例要检查异常
            box["error"] = exc

    thread = threading.Thread(target=_run, name="albuswall-test-worker")
    thread.start()
    thread.join(timeout=5.0)
    assert not thread.is_alive(), "worker 未退出：疑似死锁"
    return box


def _run_on_loop(loop: MainLoop, fn):
    """把 ``fn`` 排到主循环线程执行并等它完成（用例自用，不是被测代码）。"""
    done = threading.Event()
    box: dict = {}

    def _cb() -> None:
        try:
            box["result"] = fn()
        finally:
            done.set()

    loop.call_soon(_cb)
    assert done.wait(2.0), "主循环没有执行回调"
    return box.get("result")


# ==================== 1. 属主线程识别 ====================

def test_start_captures_the_thread_that_runs_the_loop():
    """``start()`` 在 ``on_loop`` 里被调用，捕获的就是循环线程。"""
    loop, thread = _start_loop()
    try:
        service = MainLoopService(loop)
        # 默认属主是进程主线程（本用例的线程）
        assert service.in_main_thread() is True

        _run_on_loop(loop, service.start)

        assert service.owner_thread() is thread
        assert service.in_main_thread() is False
    finally:
        _stop_loop(loop, thread)


def test_call_from_owner_thread_runs_inline_without_deadlock():
    """已在主循环线程 → 内联执行；排队会等自己，这里必须不死锁。"""
    loop, thread = _start_loop()
    try:
        service = MainLoopService(loop)
        _run_on_loop(loop, service.start)

        box = _run_on_loop(loop, lambda: service.call(lambda: "inline"))
        assert box == "inline"
    finally:
        _stop_loop(loop, thread)


# ==================== 2. 退化路径 ====================

def test_no_loop_runs_inline():
    """``loop is None``（单测 / 装配早期）→ 同步执行。"""
    service = MainLoopService(None)
    assert service.call(lambda: 42) == 42


def test_loop_without_call_soon_runs_inline():
    """只实现 run/quit 的循环 → 同步执行，而不是启动期就炸。"""
    service = MainLoopService(_NoCallSoonLoop())
    assert service.call(lambda: "inline") == "inline"


# ==================== 3. 跨线程投递 ====================

def test_call_from_worker_runs_on_loop_thread_and_returns_result():
    """worker 线程的调用在主循环线程执行，结果原样返回。"""
    loop, thread = _start_loop()
    try:
        service = MainLoopService(loop, timeout=5.0)
        _run_on_loop(loop, service.start)

        box = _in_thread(
            lambda: service.call(lambda: threading.current_thread().name)
        )
        assert "error" not in box, box.get("error")
        assert box["result"] == _LOOP_NAME
        assert box["thread"] == "albuswall-test-worker"
    finally:
        _stop_loop(loop, thread)


def test_exception_propagates_to_caller_thread_unchanged():
    """handler 异常原样带回调用线程（不换类型、不吞栈）。"""
    loop, thread = _start_loop()
    try:
        service = MainLoopService(loop, timeout=5.0)
        _run_on_loop(loop, service.start)

        raised = ValueError("boom on the main thread")
        box = _in_thread(lambda: service.call(_raise(raised)))
        assert box["error"] is raised
    finally:
        _stop_loop(loop, thread)


def _raise(exc: BaseException):
    def _fn():
        raise exc

    return _fn


# ==================== 4. 超时 ====================

def test_timeout_raises_and_late_callback_is_dropped():
    """超时抛 ``MainThreadTimeout``；迟到执行的回调因 Future 已取消而放弃。"""
    loop = _NeverRunsLoop()
    service = MainLoopService(
        loop, timeout=0.05, owner=threading.Thread(target=lambda: None),
    )

    calls: list[str] = []
    with pytest.raises(MainThreadTimeout) as excinfo:
        service.call(lambda: calls.append("ran"))

    assert excinfo.value.timeout == 0.05
    assert service.loop is loop
    assert len(loop.posted) == 1

    # 模拟"主循环后来终于跑了这个回调"：必须什么都不做。
    loop.posted[0]()
    assert calls == []


# ==================== 5. 关闭 ====================

def test_close_is_idempotent_and_rejects_new_calls():
    service = MainLoopService(None)
    service.close()
    service.close()
    assert service.closed is True
    with pytest.raises(MainLoopUnavailable):
        service.call(lambda: 1)


def test_close_cancels_pending_calls_instead_of_waiting_for_timeout():
    """关闭时在途调用立刻失败，而不是干等 ``timeout``。"""
    loop = _NeverRunsLoop()
    service = MainLoopService(
        loop, timeout=30.0, owner=threading.Thread(target=lambda: None),
    )

    started = threading.Event()
    box: dict = {}

    def _caller() -> None:
        started.set()
        try:
            service.call(lambda: "never")
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc

    worker = threading.Thread(target=_caller, name="albuswall-test-pending")
    worker.start()
    assert started.wait(2.0)
    time.sleep(0.05)          # 让 worker 真的进入 future.result()

    began = time.monotonic()
    service.close()
    worker.join(timeout=2.0)

    assert not worker.is_alive(), "close() 之后调用方仍在等待"
    assert isinstance(box.get("error"), MainLoopUnavailable)
    assert time.monotonic() - began < 2.0   # 远小于 timeout=30

    # 排队的回调无论如何不该再执行
    loop.posted[0]()


def test_default_timeout_stays_below_ipc_client_timeout():
    """默认超时必须小于 IPC 客户端超时（5s），否则客户端先报无信息量的错。"""
    from albuswall.infrastructure.ipc import IpcClient

    default_client_timeout = IpcClient.__init__.__defaults__[-1]
    assert DEFAULT_TIMEOUT < default_client_timeout


# ==================== 6. 装配接线 ====================

def test_register_service_injects_the_scheduler_into_write_entries():
    """``register_service`` 把调度器接进两个写入口 + 给命令层装翻译器。"""
    from albuswall.command import CommandRegistry
    from albuswall.core import Container
    from albuswall.service import register_service
    from albuswall.service.command.dispatch_errors import (
        translate_service_error,
    )

    loop, thread = _start_loop()
    try:
        container = Container()
        container.register(
            "command_registry", CommandRegistry, returns=CommandRegistry,
        )
        container.set("ipc_service", object())
        container.set("main_loop", loop)

        register_service(container)

        # 必须**在 register_service 之后**覆盖：它会把 Services 表里的每个
        # key 都重新登记成工厂，先 set 的实例会被覆盖掉。
        # 状态层的构造参数要 ``source_trash_service.sources_changed``；本用例
        # 只关心调度器注入，回收站给个最小替身即可（真服务要 reconciler）。
        container.set("source_trash_service", FakeSourceTrashService())

        scheduler = container.require("main_loop_service")
        assert scheduler.loop is loop
        assert container.require("source_service")._main_loop is scheduler
        assert container.require("source_state_service")._main_loop is scheduler
        assert (
            container.require("command_service").dispatcher._translate
            is translate_service_error
        )
    finally:
        _stop_loop(loop, thread)


def test_register_service_degrades_gracefully_without_a_main_loop():
    """容器里没有 ``main_loop`` 时不炸：调度器退化为同步执行。

    单测与装配早期（``main.py`` phase 4 之前）就是这种状态，见
    ``tests/test_source_wiring.py`` 里直接调 ``register_service`` 的用例。
    """
    from albuswall.command import CommandRegistry
    from albuswall.core import Container
    from albuswall.service import register_service

    container = Container()
    container.register(
        "command_registry", CommandRegistry, returns=CommandRegistry,
    )
    container.set("ipc_service", object())

    register_service(container)

    scheduler = container.require("main_loop_service")
    assert scheduler.loop is None
    assert scheduler.call(lambda: "inline") == "inline"
