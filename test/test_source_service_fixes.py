#
"""SourceService 状态一致性 / 线程安全修复的最小验证脚本。

覆盖约定里列出的 7 个问题：

1. 就地改容器缺少 mark_dirty（→ observe 收不到通知）
2. update() 丢弃纯展示字段变更（→ 与 update_source() 行为不一致）
3. _on_task_done 在工作线程写 state 并广播 observe
4. SourceExecutor.__call__ 的 signal.emit 裸调用
5. tasks 属性注解指向已删除的 Task 类型
6. shutdown() 不清 _futures
7. update_source() 的日志文案

不依赖 pytest：直接 ``python test/test_source_service_fixes.py`` 运行。
（装了 pytest 的话 ``pytest test/test_source_service_fixes.py`` 也能跑。）
"""

from __future__ import annotations

import logging
import sys
import tempfile
import threading
import traceback
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

try:  # 允许直接以文件路径运行
    import albuswall  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from albuswall.dto.import_ import AssetCandidateCacheDTO
from albuswall.dto.source import IngestSource
from albuswall.services.source import (
    SourceExecutor,
    SourceService,
    SourceServiceState,
)
from albuswall.utils.signals import Signal

_LOGGER_NAME = "albuswall.services.source"


# ─────────────────────────────────────────────────────────────
# 测试替身
# ─────────────────────────────────────────────────────────────
class FakeSourceRepo:
    """只实现 SourceService 用到的 repo 方法。"""

    def __init__(self) -> None:
        self.sources: dict[int, IngestSource] = {}

    # 写
    def put(self, source: IngestSource) -> None:
        self.sources[source.id] = source

    def update(self, source_id: int, dto) -> bool:  # noqa: ANN001
        raise AssertionError("unexpected update()")

    def create(self, dto) -> int:  # noqa: ANN001
        raise AssertionError("unexpected create()")

    # 读
    def get_source_candidates(self) -> list[IngestSource]:
        return list(self.sources.values())

    def get_sync_candidate(self, source_id: int) -> Optional[IngestSource]:
        return self.sources.get(source_id)

    def list_view_dtos(self) -> list[IngestSource]:
        return list(self.sources.values())

    def get_view_dto(self, source_id: int) -> Optional[IngestSource]:
        return self.sources.get(source_id)

    def exists(self, source_id: int) -> bool:
        return source_id in self.sources


class FakeImportRepo:
    """记录候选缓存，sufficient 支撑 SourceExecutor 的真实扫描逻辑。"""

    def __init__(self) -> None:
        self.cached: set[tuple[int, str]] = set()

    def find_missing_paths(self, source_id: int, *paths: str) -> list[str]:
        return [p for p in paths if (source_id, p) not in self.cached]

    def create_candidate_cache_batch(
            self, batch: list[AssetCandidateCacheDTO]
    ) -> int:
        for candidate in batch:
            self.cached.add((candidate.source_id, candidate.path))
        return len(batch)


class _Correlation:
    """correlation_id 占位（SourceService 传的是 seq）。"""


class FakeTaskService:
    """模拟 TaskService.submit；可选在**工作线程**里执行 fn（含 done 回调）。"""

    def __init__(self, *, run_in_thread: bool = False) -> None:
        self.run_in_thread = run_in_thread
        self.calls: list[SimpleNamespace] = []
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()

    def submit(
            self,
            fn,
            *args,
            executor=None,  # noqa: ANN001
            priority: int = 0,
            correlation_id=_Correlation,
            **kwargs,
    ) -> Future:
        future: Future = Future()
        with self._lock:
            self.calls.append(
                SimpleNamespace(
                    fn=fn,
                    correlation_id=correlation_id,
                    future=future,
                )
            )
        if self.run_in_thread:
            thread = threading.Thread(
                target=self._run, args=(fn, future), daemon=True
            )
            thread.start()
            self._threads.append(thread)
        return future

    def _run(self, fn, future: Future) -> None:  # noqa: ANN001
        try:
            future.set_result(fn())
        except BaseException as exc:  # noqa: BLE001
            future.set_exception(exc)

    def join(self, timeout: float = 5.0) -> None:
        for thread in self._threads:
            thread.join(timeout)

    def seqs(self) -> list[int]:
        return [call.correlation_id for call in self.calls]


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def messages(self) -> list[str]:
        return [self.format(r) for r in self.records]


class capture_logs:
    """上下文管理器：抓取 source 模块（及信号模块）的日志。"""

    LOGGERS = (_LOGGER_NAME, "albuswall.utils.signals")

    def __enter__(self) -> _Capture:
        self._handler = _Capture()
        self._loggers = [logging.getLogger(n) for n in self.LOGGERS]
        self._old_levels = []
        for logger in self._loggers:
            self._old_levels.append(logger.level)
            logger.setLevel(logging.DEBUG)
            logger.addHandler(self._handler)
        return self._handler

    def __exit__(self, *exc_info: object) -> None:
        for logger, level in zip(self._loggers, self._old_levels):
            logger.removeHandler(self._handler)
            logger.setLevel(level)


# ─────────────────────────────────────────────────────────────
# 辅助构造
# ─────────────────────────────────────────────────────────────
def make_source(
        source_id: int,
        source_path: str,
        *,
        title: str = "t",
        tags: Optional[list[str]] = None,
        file_types: Optional[list[str]] = None,
) -> IngestSource:
    return IngestSource(
        id=source_id,
        title=title,
        source_path=source_path,
        # target_path=None → is_void() 不要求目录存在
        target_path=None,
        mount_point=None,
        auto_mount=False,
        file_types=list(file_types) if file_types is not None
        else [".jpg", ".png"],
        tags=tags or [],
    )


def make_service(
        *,
        run_in_thread: bool = False,
        state: Optional[SourceServiceState] = None,
) -> tuple[SourceService, FakeSourceRepo, FakeImportRepo, FakeTaskService]:
    source_repo = FakeSourceRepo()
    import_repo = FakeImportRepo()
    task_service = FakeTaskService(run_in_thread=run_in_thread)
    service = SourceService(
        source_repo,  # type: ignore[arg-type]
        import_repo,  # type: ignore[arg-type]
        task_service,  # type: ignore[arg-type]
        state=state,
    )
    return service, source_repo, import_repo, task_service


def make_media_dir(files: tuple[str, ...] = ("a.jpg", "b.png")) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="albuswall-src-"))
    for name in files:
        (tmp / name).write_text("x", encoding="utf-8")
    return tmp


# ─────────────────────────────────────────────────────────────
# 问题 1：state mutation 方法会 mark_dirty
# ─────────────────────────────────────────────────────────────
def test_state_mutations_mark_dirty() -> None:
    state = SourceServiceState()
    seen: list[str] = []
    state.observe(lambda _node, key: seen.append(key))

    src = make_source(1, "/tmp")
    state.upsert_source(1, src)
    assert seen == ["sources"], seen

    state.upsert_task(1, object())
    assert seen == ["sources", "tasks_by_source"], seen

    seen.clear()
    state.remove_source(1)
    assert seen == ["sources"], seen
    seen.clear()
    state.remove_source(1)  # 不存在 → 不标脏
    assert seen == [], seen

    state.upsert_task(1, object())
    seen.clear()
    state.remove_task(1)
    assert seen == ["tasks_by_source"], seen
    seen.clear()
    state.remove_task(1)
    assert seen == [], seen

    seen.clear()
    state.mark_submitted(7)
    assert seen == ["submitted_seqs"], seen
    seen.clear()
    state.mark_submitted(7)  # 重复 → 不标脏
    assert seen == [], seen

    seen.clear()
    state.discard_submitted(7)
    assert seen == ["submitted_seqs"], seen

    state.upsert_task(1, object())
    seen.clear()
    state.clear_tasks()
    assert seen == ["tasks_by_source"], seen
    seen.clear()
    state.clear_tasks()  # 已空 → 不标脏
    assert seen == [], seen


def test_state_has_no_bypass_left() -> None:
    """公开写路径全部收敛到 state 的 mutation 方法。"""
    import inspect

    source_file = inspect.getsource(sys.modules[SourceService.__module__])
    for needle in (
            "state.sources[",
            "state.sources.pop",
            "state.tasks_by_source[",
            "state.tasks_by_source.pop",
            "state.submitted_seqs.add",
    ):
        assert needle not in source_file, f"residual bypass: {needle}"


# ─────────────────────────────────────────────────────────────
# 问题 1 验收 + 问题 2：observe 通知 / 展示字段 vs 行为字段
# ─────────────────────────────────────────────────────────────
def test_update_and_update_source_broadcast_observe() -> None:
    media = make_media_dir()
    service, repo, _import_repo, _task_service = make_service()
    seen: list[str] = []
    service.state.observe(lambda _node, key: seen.append(key))

    repo.put(make_source(1, str(media), title="old"))
    service.update()
    for expected in ("sources", "tasks_by_source", "submitted_seqs"):
        assert expected in seen, (expected, seen)

    seen.clear()
    repo.sources[1] = make_source(1, str(media), title="new")
    service.update()
    assert seen == ["sources"], seen  # 仅展示字段：只刷新快照
    assert service.state.sources[1].title == "new"

    seen.clear()
    service.update_source(1)
    assert seen == [], seen  # 完全未变：无通知、保留原引用


def test_title_change_in_update_keeps_seq() -> None:
    media = make_media_dir()
    service, repo, _import_repo, _task_service = make_service()
    repo.put(make_source(1, str(media), title="old"))
    service.update()

    seq_before = service.state.tasks_by_source[1].seq
    submitted_before = set(service.state.submitted_seqs)
    source_obj_before = service.state.sources[1]

    repo.sources[1] = make_source(1, str(media), title="brand-new-title")
    service.update()

    assert service.state.sources[1].title == "brand-new-title"
    assert service.state.sources[1] is not source_obj_before
    assert service.state.tasks_by_source[1].seq == seq_before
    assert set(service.state.submitted_seqs) == submitted_before


def test_behavior_change_in_update_rebuilds_seq() -> None:
    media_a = make_media_dir()
    media_b = make_media_dir(("c.jpg",))
    service, repo, _import_repo, _task_service = make_service()
    repo.put(make_source(1, str(media_a)))
    service.update()

    seq_before = service.state.tasks_by_source[1].seq
    repo.sources[1] = make_source(1, str(media_b))
    service.update()

    seq_after = service.state.tasks_by_source[1].seq
    assert seq_after != seq_before
    assert service.state.sources[1].source_path == str(media_b)


def test_file_types_change_rebuilds_task_and_scan_uses_new_rules() -> None:
    """回归：只改 file_types 也必须重建扫描任务。

    ``SourceExecutor`` 持有的是建任务那一刻的 ``IngestSource`` 快照。旧实现
    的 ``_source_changed`` 只比较 path / mount，改过滤规则不重建任务，于是
    重扫一直用旧规则（日志里 ``File globs: ['*.png']`` 永远不变、匹配 0 个
    文件），用户侧表现为"补上 .jpg 也没用，库一直是空的"。
    """
    media = make_media_dir(("a.jpg", "b.png"))
    service, repo, _import_repo, _task_service = make_service()
    repo.put(make_source(1, str(media), file_types=[".png"]))
    service.update()

    old_seq = service.state.tasks_by_source[1].seq
    old_executor = service.state.tasks_by_source[1]
    assert old_executor.source.file_types == [".png"]

    repo.sources[1] = make_source(
        1, str(media), file_types=[".png", ".jpg"],
    )
    service.update()

    new_executor = service.state.tasks_by_source[1]
    assert new_executor is not old_executor, "改了 file_types 必须换新任务对象"
    assert new_executor.seq != old_seq
    # 任务里带的就是新规则——扫描会用 ['.png', '.jpg'] 去匹配
    assert new_executor.source.file_types == [".png", ".jpg"]

    # 仅有顺序不同也算变化（用户可能只是重排），但值必须逐项相等才不重建
    repo.sources[1] = make_source(
        1, str(media), file_types=[".png", ".jpg"],
    )
    seq_same = service.state.tasks_by_source[1].seq
    service.update()
    assert service.state.tasks_by_source[1].seq == seq_same, "规则未变不该重建"


def test_update_and_update_source_are_consistent() -> None:
    media = make_media_dir()
    other = make_media_dir(("z.jpg",))

    def snapshot(use_update: bool) -> tuple[str, bool]:
        service, repo, _i, _t = make_service()
        repo.put(make_source(1, str(media), title="old"))
        service.update()
        seq_before = service.state.tasks_by_source[1].seq

        repo.sources[1] = make_source(1, str(media), title="新标题")
        if use_update:
            service.update()
        else:
            service.update_source(1)
        seq_same = service.state.tasks_by_source[1].seq == seq_before
        title = service.state.sources[1].title

        repo.sources[1] = make_source(1, str(other), title="新标题")
        if use_update:
            service.update()
        else:
            service.update_source(1)
        seq_rebuilt = service.state.tasks_by_source[1].seq != seq_before
        return title, (seq_same, seq_rebuilt)

    assert snapshot(True) == snapshot(False)


def test_update_drops_removed_source_and_its_task() -> None:
    media = make_media_dir()
    service, repo, _import_repo, _task_service = make_service()
    repo.put(make_source(1, str(media)))
    service.update()
    seq = service.state.tasks_by_source[1].seq

    seen: list[str] = []
    service.state.observe(lambda _node, key: seen.append(key))

    del repo.sources[1]
    service.update()
    assert service.state.sources == {}
    assert service.state.tasks_by_source == {}
    assert seq not in service.state.submitted_seqs
    assert seen.count("sources") == 1, seen
    assert seen.count("tasks_by_source") == 1, seen
    # prune_submitted 既走 __setattr__ 又显式 mark_dirty，会发两次通知
    # （既有行为，本次未改动），这里只要求"至少通知到了"。
    assert seen.count("submitted_seqs") >= 1, seen


# ─────────────────────────────────────────────────────────────
# 问题 3：工作线程不写 state
# ─────────────────────────────────────────────────────────────
def test_worker_thread_settle_defers_to_main_thread() -> None:
    media = make_media_dir()
    service, repo, _import_repo, _task_service = make_service()
    repo.put(make_source(1, str(media)))
    service.update()

    seq = service.state.tasks_by_source[1].seq
    assert seq in service.state.submitted_seqs

    observed: list[tuple[str, int]] = []
    service.state.observe(
        lambda _node, key: observed.append((key, threading.get_ident()))
    )

    # 模拟 TaskService 的 done 回调跑在工作线程
    worker = threading.Thread(target=service._on_task_done, args=(seq,))
    worker.start()
    worker.join(5)

    assert observed == [], f"worker thread broadcast observe: {observed}"
    assert seq in service.state.submitted_seqs, "state changed off main thread"
    assert seq not in service._futures

    # 主线程消费：下一次写路径把它回收掉。这里同时把源删掉，避免回收后
    # 立刻又被重新提交（那样 submitted_seqs 里会再次出现同一个 seq）。
    main_id = threading.get_ident()
    del repo.sources[1]
    service.update_source(1)
    assert seq not in service.state.submitted_seqs
    assert service.state.tasks_by_source == {}
    released = [k for k, tid in observed if k == "submitted_seqs"]
    assert released == ["submitted_seqs"], observed
    assert all(tid == main_id for _k, tid in observed), observed


def test_discard_submitted_only_via_drain() -> None:
    import inspect

    source_file = inspect.getsource(sys.modules[SourceService.__module__])
    lines = [
        line.strip()
        for line in source_file.splitlines()
        if "discard_submitted" in line and not line.strip().startswith("#")
    ]
    # 只允许一处调用点（_drain_settled），其余是 docstring 提及
    calls = [line for line in lines if line.startswith("self.state.")]
    assert calls == ["self.state.discard_submitted(seq)"], lines


# ─────────────────────────────────────────────────────────────
# 问题 3 回归：扫描完成 → 再次提交
# ─────────────────────────────────────────────────────────────
def test_scan_then_resubmit_cycle() -> None:
    media = make_media_dir()
    state = SourceServiceState()
    service, repo, _import_repo, task_service = make_service(
        run_in_thread=True, state=state
    )
    repo.put(make_source(1, str(media)))

    service.update()
    task_service.join()
    seq = service.state.tasks_by_source[1].seq
    assert len(task_service.calls) == 1
    # 工作线程已经把 seq 记进待回收，但 state 还没动
    assert seq in service.state.submitted_seqs

    # 下一轮刷新（主线程）→ 回收 + 重新提交同一个 seq
    service.update_source(1)
    assert seq in service.state.submitted_seqs  # 重新提交后又在途
    assert len(task_service.calls) == 2, task_service.seqs()
    assert task_service.seqs() == [seq, seq]
    task_service.join()


# ─────────────────────────────────────────────────────────────
# 问题 4：scan_finished 订阅者异常不影响任务结果
# ─────────────────────────────────────────────────────────────
def test_scan_finished_subscriber_exception_does_not_fail_task() -> None:
    media = make_media_dir()
    service, repo, import_repo, task_service = make_service(
        run_in_thread=True
    )
    repo.put(make_source(1, str(media)))

    def boom(_sender, **_kwargs) -> None:
        raise RuntimeError("UI 回调炸了")

    service.scan_finished.connect(boom)

    with capture_logs() as logs:
        service.update()
        task_service.join()

    call = task_service.calls[0]
    assert call.future.exception() is None, call.future.exception()
    assert call.future.result() is None
    # 扫描本身成功：候选已写入缓存
    assert import_repo.cached, "scan did not write candidate cache"
    assert any(
        "slot" in m or "scan_finished signal raised" in m
        for m in logs.messages()
    ), logs.messages()


def test_broken_signal_emit_does_not_propagate() -> None:
    """即使 emit 本身炸（不是订阅者），__call__ 也不外抛。"""
    media = make_media_dir()

    class ExplodingSignal(Signal):
        def emit(self, sender=None, **kwargs):  # noqa: ANN001
            raise RuntimeError("emit 自己炸了")

    executor = SourceExecutor(
        source=make_source(1, str(media)),
        repo=FakeImportRepo(),  # type: ignore[arg-type]
        seq=1,
        finished_signal=ExplodingSignal("boom"),
    )
    with capture_logs() as logs:
        executor()  # 不抛
    assert any(
        "scan_finished signal raised" in m for m in logs.messages()
    ), logs.messages()


# ─────────────────────────────────────────────────────────────
# 问题 5：tasks 注解
# ─────────────────────────────────────────────────────────────
def test_tasks_annotation_points_to_source_executor() -> None:
    hint = SourceService.tasks.fget.__annotations__["return"]
    assert hint in ("List['SourceExecutor']", 'List["SourceExecutor"]'), hint
    assert "Task" not in hint.replace("SourceExecutor", "")


# ─────────────────────────────────────────────────────────────
# 问题 6 / 7
# ─────────────────────────────────────────────────────────────
def test_shutdown_clears_futures() -> None:
    media = make_media_dir()
    service, repo, _import_repo, _task_service = make_service()
    repo.put(make_source(1, str(media)))
    service.update()
    assert service._futures, "expected an in-flight future"
    service.shutdown()
    assert service._futures == {}
    assert service.state.sources == {}
    assert service.state.tasks_by_source == {}
    assert service.state.submitted_seqs == set()


def test_closed_log_messages() -> None:
    service, _repo, _import_repo, _task_service = make_service()
    service.shutdown()
    with capture_logs() as logs:
        service.update()
        service.update_source(1)
    messages = logs.messages()
    assert any("update() ignored" in m for m in messages), messages
    assert any("update_source() ignored" in m for m in messages), messages


# ─────────────────────────────────────────────────────────────
# 运行器
# ─────────────────────────────────────────────────────────────
def main() -> int:
    tests = [
        (name, obj)
        for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    failures = 0
    for name, func in tests:
        try:
            func()
        except Exception:  # noqa: BLE001
            failures += 1
            print(f"FAIL {name}")
            traceback.print_exc()
        else:
            print(f"ok   {name}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
