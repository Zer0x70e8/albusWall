#
"""``SourceRuntime`` 的任务派发契约：幂等、可观测、可控制。

三条被钉住的行为：

1. **幂等派发** —— 已扫完（已结算）的任务不会被一次无关对账重新激活；
   只有 ``rebuild`` / ``added``、``rescan``、``force_scan`` 会重新派发。
   旧实现遍历整张任务表，导致"新建一个源 ⇒ 其余所有源重扫一遍"。
2. **可观测** —— ``status()`` 给出每源调度态 + 最近一次扫描结果。
3. **可控制** —— ``force_scan``（不看跟踪集）与 ``stop_scan``（撤任务、
   取消未启动的在途任务）。

用内存替身（fake repo / fake TaskService），不连 DB、不起任务线程：这里测的
是**调度决策**，不是扫描本身（后者见 ``test_source_services.py`` 的 executor
用例）。
"""

from __future__ import annotations

from concurrent.futures import Future
from typing import Any, Optional

import pytest

from albuswall.common.exceptions import BackpressureError
from albuswall.dto.source.events import SourceScanFinished
from albuswall.dto.source.query import IngestSource
from albuswall.service.source import ScanTaskSnapshot
from albuswall.service.source.runtime import SourceRuntime


# ==================== 替身 ====================

class FakeTaskService:
    """记录提交；``submit`` 返回一个由用例控制的 ``Future``。"""

    def __init__(self) -> None:
        self.submissions: list[dict[str, Any]] = []
        self.futures: list[Future] = []
        self.reject_next = 0

    def submit(self, fn, **kwargs) -> Future:
        if self.reject_next > 0:
            self.reject_next -= 1
            raise BackpressureError(reason="full", queue_size=1, max_queue_size=1)
        future: Future = Future()
        self.submissions.append({"fn": fn, **kwargs})
        self.futures.append(future)
        return future

    # ---- 用例辅助 ----
    def settle(self) -> None:
        """让最近一次提交的任务到达终态（触发 done 回调）。"""
        for future in self.futures:
            if not future.done():
                future.set_result(None)
        self.futures.clear()

    def last_seq(self) -> int:
        return self.submissions[-1]["correlation_id"]

    def last_executor(self):
        return self.submissions[-1]["fn"]


class FakeImportRepo:
    """executor 不会被真正执行，这里只需占位。"""


class FakeSourceRepo:
    def exists(self, source_id: int, **_kw) -> bool:
        return True


def _make_runtime() -> tuple[SourceRuntime, FakeTaskService, FakeSourceRepo]:
    task_service = FakeTaskService()
    repo = FakeSourceRepo()
    runtime = SourceRuntime(FakeImportRepo(), task_service, repo)
    runtime.start()
    return runtime, task_service, repo


def _source(source_id: int = 1, **overrides: Any) -> IngestSource:
    base = dict(id=source_id, title=f"src-{source_id}", source_path="/data/pics")
    base.update(overrides)
    return IngestSource(**base)


def _finished(source_id: int, seq: int, *, error: Optional[str] = None):
    return SourceScanFinished(
        source_id=source_id,
        task_seq=seq,
        scanned_file_count=3,
        new_file_count=1,
        has_new_content=True,
        inserted_count=1,
        error=error,
    )


# ==================== 1. 幂等派发 ====================

def test_added_source_is_submitted_once():
    runtime, task_service, _repo = _make_runtime()
    try:
        runtime.reconcile(added={1: _source(1)})

        assert len(task_service.submissions) == 1
        assert runtime.task_seqs == {1: task_service.last_seq()}
        assert runtime.submitted_seqs == {task_service.last_seq()}
    finally:
        runtime.shutdown()


def test_settled_task_is_not_resubmitted_by_another_sources_plan():
    """核心回归：新建源 B 不会把已扫完的源 A 再扫一遍。"""
    runtime, task_service, _repo = _make_runtime()
    try:
        runtime.reconcile(added={1: _source(1)})
        task_service.settle()                      # A 扫完

        runtime.reconcile(added={2: _source(2)})   # 只新增 B

        submitted = [s["correlation_id"] for s in task_service.submissions]
        assert len(submitted) == 2                 # A 一次 + B 一次
        assert runtime.task_seqs[1] == submitted[0]
        assert runtime.submitted_seqs == {submitted[1]}   # 只有 B 在途
    finally:
        runtime.shutdown()


def test_snapshot_only_plan_never_resubmits():
    runtime, task_service, _repo = _make_runtime()
    try:
        runtime.reconcile(added={1: _source(1)})
        task_service.settle()

        for _ in range(3):
            runtime.reconcile(snapshot={1: _source(1, title="renamed")})

        assert len(task_service.submissions) == 1
    finally:
        runtime.shutdown()


def test_rebuild_resubmits_with_a_new_seq():
    runtime, task_service, _repo = _make_runtime()
    try:
        runtime.reconcile(added={1: _source(1)})
        first_seq = runtime.task_seqs[1]
        task_service.settle()

        runtime.reconcile(rebuild={1: _source(1, source_path="/data/other")})

        assert len(task_service.submissions) == 2
        assert runtime.task_seqs[1] != first_seq
        assert runtime.submitted_seqs == {runtime.task_seqs[1]}
    finally:
        runtime.shutdown()


def test_rescan_resubmits_only_the_targeted_source():
    runtime, task_service, _repo = _make_runtime()
    try:
        runtime.reconcile(added={1: _source(1), 2: _source(2)})
        task_service.settle()

        assert runtime.rescan(2) is True

        assert len(task_service.submissions) == 3          # 只多了一次
        assert task_service.submissions[-1]["correlation_id"] == runtime.task_seqs[2]
        assert runtime.submitted_seqs == {runtime.task_seqs[2]}
    finally:
        runtime.shutdown()


def test_backpressure_keeps_task_pending_and_retries_next_round():
    runtime, task_service, _repo = _make_runtime()
    try:
        task_service.reject_next = 1
        runtime.reconcile(added={1: _source(1)})

        assert task_service.submissions == []
        assert [s.state for s in runtime.status()] == ["pending"]

        runtime.reconcile(snapshot={1: _source(1)})   # 下一轮重试
        assert len(task_service.submissions) == 1
        assert [s.state for s in runtime.status()] == ["running"]
    finally:
        runtime.shutdown()


def test_removed_source_drops_task_without_resubmitting_others():
    runtime, task_service, _repo = _make_runtime()
    try:
        runtime.reconcile(added={1: _source(1), 2: _source(2)})
        task_service.settle()

        before = len(task_service.submissions)
        runtime.reconcile(removed_ids=(1,))

        assert 1 not in runtime.task_seqs
        # 撤任务不触发任何新提交（``seq`` 是进程级计数器，只能按条数断言）
        assert len(task_service.submissions) == before
        assert runtime.task_seqs.keys() == {2}
    finally:
        runtime.shutdown()


# ==================== 2. 可观测 ====================

def test_status_reports_scheduling_state_and_last_scan():
    runtime, task_service, _repo = _make_runtime()
    try:
        runtime.reconcile(added={1: _source(1)})
        snapshots = runtime.status(1)

        assert len(snapshots) == 1
        snapshot = snapshots[0]
        assert isinstance(snapshot, ScanTaskSnapshot)
        assert (snapshot.source_id, snapshot.state) == (1, "running")
        assert snapshot.submitted is True
        assert snapshot.last_scan is None            # 还没扫出结果

        # 扫描完成：runtime 的 scan_finished 槽记一份结果
        seq = snapshot.seq
        runtime.scan_finished.emit(None, event=_finished(1, seq))
        task_service.settle()
        runtime.reconcile(snapshot={1: _source(1)})

        after = runtime.status(1)[0]
        assert after.state == "finished"
        assert after.submitted is False
        assert after.last_scan["new_file_count"] == 1
        assert after.last_scan["task_seq"] == seq
        assert after.last_scan_at is not None
    finally:
        runtime.shutdown()


def test_status_says_finished_as_soon_as_the_scan_reports_back():
    """有结果就报 finished —— 即使 seq 还没从在途集合里回收。"""
    runtime, task_service, _repo = _make_runtime()
    try:
        runtime.reconcile(added={1: _source(1)})
        seq = runtime.task_seqs[1]

        runtime.scan_finished.emit(None, event=_finished(1, seq))

        snapshot = runtime.status(1)[0]
        assert snapshot.state == "finished"
        # 原始在途标志仍为真：回收要等下一次对账（文档里写明的滞后）
        assert snapshot.submitted is True
    finally:
        runtime.shutdown()


def test_status_marks_pending_state_before_submission():
    runtime, task_service, _repo = _make_runtime()
    try:
        task_service.reject_next = 1
        runtime.reconcile(added={1: _source(1)})

        snapshot = runtime.status()[0]
        assert snapshot.state == "pending"
        assert snapshot.submitted is False
    finally:
        runtime.shutdown()


def test_status_covers_stopped_sources():
    runtime, task_service, _repo = _make_runtime()
    try:
        runtime.reconcile(added={1: _source(1)})
        runtime.scan_finished.emit(None, event=_finished(1, runtime.task_seqs[1]))
        runtime.stop_scan(1)

        snapshot = runtime.status(1)[0]
        assert snapshot.state == "stopped"
        assert snapshot.seq == task_service.last_seq()
        assert snapshot.last_scan["scanned_file_count"] == 3
    finally:
        runtime.shutdown()


def test_status_returns_all_sources_sorted():
    runtime, task_service, _repo = _make_runtime()
    try:
        runtime.reconcile(added={2: _source(2), 1: _source(1)})

        assert [s.source_id for s in runtime.status()] == [1, 2]
        assert [s.source_id for s in runtime.status(2)] == [2]
        assert runtime.status(99) == []
    finally:
        runtime.shutdown()


# ==================== 3. 运行时控制 ====================

def test_force_scan_dispatches_even_for_untracked_source():
    runtime, task_service, _repo = _make_runtime()
    try:
        runtime.reconcile(added={1: _source(1)})
        task_service.settle()
        ahead = len(task_service.submissions)

        seq = runtime.force_scan(_source(42))          # 未跟踪的源也能强制扫

        assert seq is not None
        assert len(task_service.submissions) == ahead + 1
        assert runtime.task_seqs[42] == seq
        assert task_service.last_executor().source_id == 42
    finally:
        runtime.shutdown()


def test_force_scan_ignored_when_runtime_not_started():
    task_service = FakeTaskService()
    runtime = SourceRuntime(FakeImportRepo(), task_service, FakeSourceRepo())

    assert runtime.force_scan(_source(1)) is None
    assert task_service.submissions == []


def test_stop_scan_cancels_pending_future_and_drops_task():
    runtime, task_service, _repo = _make_runtime()
    try:
        runtime.reconcile(added={1: _source(1)})
        seq = runtime.task_seqs[1]
        future = task_service.futures[-1]

        assert runtime.stop_scan(1) is True

        assert 1 not in runtime.task_seqs
        assert seq not in runtime.submitted_seqs
        assert future.cancelled() is True              # 未启动 → 真的取消了
        assert runtime.stop_scan(1) is False           # 幂等
    finally:
        runtime.shutdown()


def test_stop_scan_does_not_break_other_sources():
    runtime, task_service, _repo = _make_runtime()
    try:
        runtime.reconcile(added={1: _source(1), 2: _source(2)})
        task_service.settle()
        runtime.reconcile(snapshot={2: _source(2)})    # 2 还在途？不需要

        runtime.stop_scan(1)
        runtime.rescan(2)

        assert runtime.rescan(1) is False              # 已停 → 无法 rescan
        assert runtime.rescan(2) is True
    finally:
        runtime.shutdown()


def test_shutdown_clears_snapshot_history():
    runtime, task_service, _repo = _make_runtime()
    runtime.reconcile(added={1: _source(1)})
    runtime.scan_finished.emit(None, event=_finished(1, runtime.task_seqs[1]))

    runtime.shutdown()

    assert runtime.status() == []


@pytest.mark.parametrize("action", ["reconcile", "rescan", "force_scan", "status"])
def test_public_entries_are_noops_after_shutdown(action):
    """关闭后所有 public 入口都不该炸（装配期 / 退出期的防御性契约）。"""
    runtime, _task_service, _repo = _make_runtime()
    runtime.shutdown()

    if action == "reconcile":
        runtime.reconcile(added={1: _source(1)})
    elif action == "rescan":
        assert runtime.rescan(1) is False
    elif action == "force_scan":
        assert runtime.force_scan(_source(1)) is None
    else:
        assert runtime.status() == []
