#
"""源域**线程边界**：写入口自己回主线程，命令层保持不变。

背景（见 ``service/main_loop_service.py`` 模块 docstring）：``Signal`` 是同步
派发，``TrackingState`` 只允许属主线程写。IPC handler 跑在 ``ipc-conn`` 上，
所以线程边界必须画在**服务的写方法**里 —— 否则"DB 已提交、state 才炸"。

本文件钉住三件事：

1. 跨线程调写方法 → 写库与 ``emit`` 都发生在主循环线程；
2. 读方法**不**平移（读命令不该陪写命令排队）；
3. ``SourceStateService.refresh_source`` 从任意线程调用都安全
   （触发器调度线程就是这条路径，回归既存的跨线程写 state 隐患）。
"""

from __future__ import annotations

import threading
from concurrent.futures import Future
from typing import Any, Optional

import pytest

from albuswall.command import Command, CommandRegistry
from albuswall.core.main_loop import HeadlessMainLoop
from albuswall.dto.sentinel import UNSET as _UNSET_SENTINEL
from albuswall.dto.source.command import IngestSourceCreate, IngestSourceUpdate
from albuswall.dto.source.events import SourcePurgeReport
from albuswall.dto.source.query import IngestSource
from albuswall.service.command.dispatch_errors import (
    SourceNotFoundMapped,
    translate_service_error,
)
from albuswall.service.command.handlers._params import pos
from albuswall.service.command.service import CommandService
from albuswall.service.main_loop_service import MainLoopService
from albuswall.service.source import (
    SourceNotFound,
    SourceService,
    SourceStateService,
)
from albuswall.utils.signals import Signal

_LOOP_NAME = "albuswall-test-loop"
_WORKER_NAME = "albuswall-test-worker"


# ==================== 替身 ====================

class FakeRepo:
    """内存版仓储：每次调用都记下"谁调的"，用于断言线程归属。"""

    def __init__(self) -> None:
        self.rows: dict[int, IngestSource] = {}
        self.threads: dict[str, str] = {}
        self._next_id = 1
        self.asset_count = 0

    # ---- 记录 ----
    def _note(self, name: str) -> None:
        self.threads[name] = threading.current_thread().name

    # ---- 读 ----
    def exists(self, source_id: int, *, include_deleted: bool = False) -> bool:
        self._note("exists")
        return source_id in self.rows

    def count_assets(self, source_id: int) -> int:
        self._note("count_assets")
        return self.asset_count

    def get_view_dto(self, source_id: int) -> Optional[IngestSource]:
        self._note("get_view_dto")
        return self.rows.get(source_id)

    def list_view_dtos(self) -> list[IngestSource]:
        self._note("list_view_dtos")
        return list(self.rows.values())

    def get_sync_candidate(self, source_id: int) -> Optional[IngestSource]:
        self._note("get_sync_candidate")
        return self.rows.get(source_id)

    def get_source_candidates(self) -> list[IngestSource]:
        self._note("get_source_candidates")
        return list(self.rows.values())

    # ---- 写 ----
    def create(self, source: IngestSourceCreate) -> int:
        self._note("create")
        new_id = self._next_id
        self._next_id += 1
        self.rows[new_id] = IngestSource(
            id=new_id,
            title=source.title,
            source_path=source.source_path,
            target_path=source.target_path,
            mount_point=source.mount_point,
            auto_mount=source.auto_mount,
        )
        return new_id

    def update(self, source_id: int, patch: IngestSourceUpdate) -> bool:
        self._note("update")
        row = self.rows.get(source_id)
        if row is None:
            return False
        new_title = getattr(patch, "title", None)
        if new_title is not None and new_title is not _UNSET_SENTINEL:
            row.title = new_title
        return True

    def soft_delete(self, source_id: int) -> bool:
        self._note("soft_delete")
        return source_id in self.rows

    def purge(self, source_id: int) -> SourcePurgeReport:
        self._note("purge")
        existed = self.rows.pop(source_id, None) is not None
        return SourcePurgeReport(
            source_id=source_id, purged=existed,
            asset_ids=[], asset_uuids=[],
        )


class FakeRuntime:
    """只记录 ``reconcile`` / ``rescan``，供 state 层用例使用。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any, str]] = []
        self.futures: dict[int, Future] = {}
        self.settled_seqs: set[int] = set()
        self.submitted_seqs: set[int] = set()
        self.task_seqs: dict[int, int] = {}

    def reconcile(self, **kwargs: Any) -> None:
        self.calls.append(("reconcile", kwargs, threading.current_thread().name))

    def rescan(self, source_id: int) -> bool:
        self.calls.append(("rescan", source_id, threading.current_thread().name))
        return True

    def has_task(self, source_id: int) -> bool:
        return False


# ==================== fixture ====================

@pytest.fixture
def running_loop():
    """真正在跑的 headless 主循环（跨线程语义只能靠真循环证明）。"""
    loop = HeadlessMainLoop()
    thread = threading.Thread(target=loop.run, name=_LOOP_NAME, daemon=True)
    thread.start()
    try:
        yield loop, thread
    finally:
        loop.quit(0)
        thread.join(timeout=2.0)


@pytest.fixture
def main_thread_service(running_loop):
    """已 ``start()`` 的调度器（属主 = 循环线程）。"""
    loop, thread = running_loop
    service = MainLoopService(loop, timeout=5.0)
    done = threading.Event()
    loop.call_soon(lambda: (service.start(), done.set()))
    assert done.wait(2.0)
    assert service.owner_thread() is thread
    return service


def _call_from_worker(fn):
    """在独立线程执行 ``fn``，返回 (结果, 异常, 该线程名)。"""
    box: dict = {}

    def _run() -> None:
        box["thread"] = threading.current_thread().name
        try:
            box["result"] = fn()
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc

    worker = threading.Thread(target=_run, name=_WORKER_NAME)
    worker.start()
    worker.join(timeout=5.0)
    assert not worker.is_alive(), "worker 未退出：疑似死锁"
    return box


# ==================== 1. SourceService：写入口回主线程 ====================

def test_create_from_worker_writes_and_emits_on_loop_thread(
        main_thread_service,
):
    """跨线程 ``create`` → 仓储写入与 ``source_created`` 都在主循环线程。"""
    repo = FakeRepo()
    service = SourceService(repo, main_loop=main_thread_service)

    seen: list[tuple[int, str]] = []
    service.source_created.connect(
        lambda _sender, *, source_id: seen.append(
            (source_id, threading.current_thread().name))
    )

    box = _call_from_worker(
        lambda: service.create(IngestSourceCreate(title="pics", source_path="/p"))
    )

    assert "error" not in box, box.get("error")
    assert box["thread"] == _WORKER_NAME
    assert box["result"] == 1
    assert repo.threads["create"] == _LOOP_NAME
    # 关键断言：订阅者（= 状态层）在主循环线程上被调用
    assert seen == [(1, _LOOP_NAME)]


def test_update_and_delete_from_worker_emit_on_loop_thread(
        main_thread_service,
):
    repo = FakeRepo()
    repo.rows[1] = IngestSource(id=1, title="old", source_path="/p")
    service = SourceService(repo, main_loop=main_thread_service)

    seen: list[tuple[str, str]] = []
    service.source_changed.connect(
        lambda _sender, **_: seen.append(
            ("changed", threading.current_thread().name))
    )
    service.source_deleted.connect(
        lambda _sender, **_: seen.append(
            ("deleted", threading.current_thread().name))
    )

    update = _call_from_worker(
        lambda: service.update(1, IngestSourceUpdate(title="new"))
    )
    assert "error" not in update, update.get("error")

    delete = _call_from_worker(lambda: service.delete(1))
    assert "error" not in delete, delete.get("error")

    assert repo.threads["update"] == _LOOP_NAME
    assert repo.threads["soft_delete"] == _LOOP_NAME
    assert repo.threads["purge"] == _LOOP_NAME
    assert seen == [("changed", _LOOP_NAME), ("deleted", _LOOP_NAME)]


def test_write_from_owner_thread_runs_inline(main_thread_service):
    """主线程自己调用（UI 写路径）→ 内联；不需要推进事件循环即可完成。"""
    repo = FakeRepo()
    # 故意的：不 start() 一个新调度器，直接用属主线程 = 本线程的实例
    inline = MainLoopService(None)
    service = SourceService(repo, main_loop=inline)

    assert service.create(
        IngestSourceCreate(title="pics", source_path="/p")
    ) == 1
    assert repo.threads["create"] == threading.current_thread().name


# ==================== 2. 读路径不平移 ====================

def test_read_paths_do_not_hop_to_main_thread(main_thread_service):
    """读方法留在调用线程（写入口才回主线程），否则读命令要陪写命令排队。"""
    repo = FakeRepo()
    service = SourceService(repo, main_loop=main_thread_service)

    box = _call_from_worker(lambda: service.list_sources())
    assert "error" not in box, box.get("error")
    assert repo.threads["list_view_dtos"] == _WORKER_NAME


# ==================== 3. SourceStateService.refresh_source ====================

def test_refresh_source_from_worker_runs_on_loop_thread(
        running_loop, main_thread_service,
):
    """触发器调度线程的路径：``refresh_source`` 不得跨线程写 state。

    在循环线程里构造 state 服务，让 ``TrackingState`` 的属主线程与主循环
    一致（生产环境两者都是进程主线程）；随后从 worker 调用，跟踪集写入
    必须落在属主线程，否则 ``TrackingState`` 会抛 ``RuntimeError``。
    """
    loop, _ = running_loop
    repo = FakeRepo()
    runtime = FakeRuntime()
    signals = [Signal(f"sig-{i}") for i in range(4)]

    # 在循环线程里构造：TrackingState 的属主线程 = 主循环线程
    done = threading.Event()
    box: dict = {}

    def _build() -> None:
        box["state"] = SourceStateService(
            repo, runtime,
            source_created=signals[0], source_changed=signals[1],
            source_deleted=signals[2], trash_changed=signals[3],
            main_loop=main_thread_service,
        )
        done.set()

    loop.call_soon(_build)
    assert done.wait(2.0)
    state = box["state"]

    # 源不存在 → _reconcile_one 走 remove_source（是一次 TrackingState 写）
    result = _call_from_worker(lambda: state.refresh_source(7))

    assert "error" not in result, result.get("error")
    assert repo.threads["get_sync_candidate"] == _LOOP_NAME
    assert runtime.calls == [("reconcile", {"removed_ids": (7,)}, _LOOP_NAME)]


def test_mount_source_from_worker_raises_inside_loop_thread(
        main_thread_service,
):
    """``mount_source`` 的错误也从主循环线程回到调用方（跨线程不影响异常类型）。"""
    repo = FakeRepo()
    runtime = FakeRuntime()
    signals = [Signal(f"sig-{i}") for i in range(4)]
    state = SourceStateService(
        repo, runtime,
        source_created=signals[0], source_changed=signals[1],
        source_deleted=signals[2], trash_changed=signals[3],
        main_loop=main_thread_service,
    )

    box = _call_from_worker(lambda: state.mount_source(99))

    assert isinstance(box.get("error"), SourceNotFound)
    assert repo.threads["get_sync_candidate"] == _LOOP_NAME


# ==================== 4. 端到端：命令（子线程）→ 服务（回主线程） ====================

def test_command_runs_on_worker_but_service_work_lands_on_main_loop(
        main_thread_service,
):
    """完整链路：handler 留在子线程，服务把真正的工作搬回主循环线程。

    这正是本次改动的分工 —— ``Dispatcher`` 不加线程逻辑，命令层照旧同步；
    线程边界由服务的写入口负责。
    """
    repo = FakeRepo()
    service = SourceService(repo, main_loop=main_thread_service)

    registry = CommandRegistry()
    handler_thread: list[str] = []

    def _source_create(title: str) -> dict:
        handler_thread.append(threading.current_thread().name)
        new_id = service.create(
            IngestSourceCreate(title=title, source_path="/p")
        )
        return {"id": new_id}

    registry.register(Command(
        path="source.create",
        func=_source_create,
        params=(pos("title"),),
    ))

    command_service = CommandService(
        registry, exception_translator=translate_service_error,
    )

    box = _call_from_worker(
        lambda: command_service.execute("source.create", ["pics"], {})
    )

    assert "error" not in box, box.get("error")
    assert box["result"] == {"id": 1}
    assert handler_thread == [_WORKER_NAME]     # handler 仍在子线程
    assert repo.threads["create"] == _LOOP_NAME  # 服务的工作在主循环线程


def test_command_sees_typed_dispatch_error_from_service_layer(
        main_thread_service,
):
    """服务层异常穿过命令层后带正确的 code / 退出码，而不是 CommandFailed。"""
    repo = FakeRepo()
    service = SourceService(repo, main_loop=main_thread_service)

    registry = CommandRegistry()

    def _source_delete(source_id: int) -> dict:
        service.delete(source_id)
        return {"deleted": source_id}

    registry.register(Command(path="source.delete", func=_source_delete))
    command_service = CommandService(
        registry, exception_translator=translate_service_error,
    )

    box = _call_from_worker(
        lambda: command_service.execute("source.delete", [123], {})
    )

    error = box.get("error")
    assert isinstance(error, SourceNotFoundMapped)
    assert error.code == "source_not_found"
    assert error.source_id == 123
    assert repo.threads["exists"] == _LOOP_NAME
