#
"""回归：源被删除后，扫描任务不得再往候选缓存里写行。

现场（修复前的日志）：

    23:46:47  source 17 软删 → purge（清 assets + 删 ingest_source 行）
    23:47:03  新建 source 18（同一个目录）→ SourceService 的写路径
              把 source 17 的旧扫描任务**又重新提交了一遍**
    23:47:03  sqlite3.IntegrityError: FOREIGN KEY constraint failed
              （asset_candidate_cache.source_id → ingest_source.id）

三层修复，本脚本逐层验证：

1. ``services.on_source_trash_changed``：回收站事件 → 同步调度侧跟踪集，
   从根上不再重新提交已删除源的扫描任务；
2. ``SourceExecutor``：开扫前校验源是否仍有效，失效则安静跳过；
3. ``ImportRepository.create_candidate_cache_batch``：事务内剔除源已消失
   的候选，不再让 FK 约束把"源已删除"演成事务回滚 + traceback。

不依赖 pytest：直接 ``python test/test_source_deleted_scan.py`` 运行。
"""

from __future__ import annotations

import sys
import tempfile
import traceback
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
from uuid import uuid4

try:  # 允许直接以文件路径运行
    import albuswall  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from albuswall.dto.import_ import AssetCandidateCacheDTO
from albuswall.dto.source import IngestSource, IngestSourceCreate
from albuswall.infrastructure.database import Connector
from albuswall.repositories.import_ import ImportRepository
from albuswall.repositories.source import IngestSourceRepository
from albuswall.services import on_source_trash_changed
from albuswall.services.source import (
    SourceExecutor,
    SourceService,
    SourceServiceState,
)
from albuswall.utils.signals import Signal


# ─────────────────────────────────────────────────────────────
# 测试替身
# ─────────────────────────────────────────────────────────────
class FakeSourceRepo:
    """只实现 SourceService / 存活探针用到的读方法。"""

    def __init__(self) -> None:
        self.sources: dict[int, IngestSource] = {}

    def put(self, source: IngestSource) -> None:
        self.sources[source.id] = source

    # 读
    def get_source_candidates(self) -> list[IngestSource]:
        return list(self.sources.values())

    def get_sync_candidate(self, source_id: int) -> Optional[IngestSource]:
        return self.sources.get(source_id)

    def list_view_dtos(self) -> list[IngestSource]:
        return list(self.sources.values())

    def get_view_dto(self, source_id: int) -> Optional[IngestSource]:
        return self.sources.get(source_id)

    def exists(self, source_id: int, *, include_deleted: bool = False) -> bool:
        return source_id in self.sources

    def create(self, dto):  # noqa: ANN001
        raise AssertionError("unexpected create()")

    def update(self, source_id: int, dto) -> bool:  # noqa: ANN001
        raise AssertionError("unexpected update()")


class FakeImportRepo:
    """记录候选缓存写入，供 SourceExecutor 的真实扫描逻辑使用。"""

    def __init__(self) -> None:
        self.cached: set[tuple[int, str]] = set()
        self.batches: list[list[AssetCandidateCacheDTO]] = []

    def find_missing_paths(self, source_id: int, *paths: str) -> list[str]:
        return [p for p in paths if (source_id, p) not in self.cached]

    def create_candidate_cache_batch(
            self, batch: list[AssetCandidateCacheDTO]
    ) -> int:
        self.batches.append(list(batch))
        for candidate in batch:
            self.cached.add((candidate.source_id, candidate.path))
        return len(batch)


class FakeTaskService:
    """只记录提交，不真正执行。"""

    def __init__(self) -> None:
        self.calls: list[SimpleNamespace] = []

    def submit(
            self, fn, *args, executor=None, priority: int = 0,
            correlation_id=None, **kwargs,
    ) -> Future:  # noqa: ANN001
        future: Future = Future()
        self.calls.append(SimpleNamespace(fn=fn, seq=correlation_id))
        return future

    def submitted_source_ids(self) -> list[int]:
        return [call.fn.source.id for call in self.calls]


class SpyService:
    """记录 update_source 调用，用于验证事件分派规则。"""

    def __init__(self) -> None:
        self.calls: list[int] = []

    def update_source(self, source_id: int) -> None:
        self.calls.append(source_id)


# ─────────────────────────────────────────────────────────────
# 辅助构造
# ─────────────────────────────────────────────────────────────
def make_source(source_id: int, source_path: str) -> IngestSource:
    return IngestSource(
        id=source_id,
        title="t",
        source_path=source_path,
        target_path=None,
        mount_point=None,
        auto_mount=False,
        file_types=[".jpg", ".png"],
        tags=[],
    )


def make_media_dir(files: tuple[str, ...] = ("a.jpg", "b.png")) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="albuswall-scan-"))
    for name in files:
        (tmp / name).write_text("x", encoding="utf-8")
    return tmp


def make_service() -> tuple[SourceService, FakeSourceRepo, FakeImportRepo,
                            FakeTaskService]:
    source_repo = FakeSourceRepo()
    import_repo = FakeImportRepo()
    task_service = FakeTaskService()
    service = SourceService(
        source_repo,  # type: ignore[arg-type]
        import_repo,  # type: ignore[arg-type]
        task_service,  # type: ignore[arg-type]
        state=SourceServiceState(),
    )
    return service, source_repo, import_repo, task_service


def make_candidate(source_id: int, path: str) -> AssetCandidateCacheDTO:
    return AssetCandidateCacheDTO(
        uuid=uuid4(),
        path=path,
        source_id=source_id,
        mime_type="image/jpeg",
    )


# ─────────────────────────────────────────────────────────────
# 1. 根因：回收站事件必须同步调度侧跟踪集
# ─────────────────────────────────────────────────────────────
def test_purged_event_is_ignored() -> None:
    """purge 跑在 reconciler 工作线程上，不得借此跨线程改 state。"""
    spy = SpyService()
    on_source_trash_changed(spy, None, source_id=17, kind="purged")
    assert spy.calls == [], spy.calls


def test_soft_delete_and_restore_events_refresh_source() -> None:
    spy = SpyService()
    on_source_trash_changed(spy, None, source_id=17, kind="soft_delete")
    on_source_trash_changed(spy, None, source_id=17, kind="restore")
    assert spy.calls == [17, 17], spy.calls


def test_soft_delete_does_not_resubmit_stale_scan() -> None:
    """现场复现：软删 1 个源后新建源，旧任务不得被重新提交。"""
    media = make_media_dir()
    other = make_media_dir(("z.jpg",))
    service, repo, _import_repo, task_service = make_service()

    repo.put(make_source(17, str(media)))
    service.update()
    assert task_service.submitted_source_ids() == [17]
    assert 17 in service.state.tasks_by_source

    # 回收站软删：DB 行不可见 + 事件送达（修复前缺的就是这一步）
    del repo.sources[17]
    on_source_trash_changed(service, None, source_id=17, kind="soft_delete")

    assert service.state.sources == {}, service.state.sources
    assert service.state.tasks_by_source == {}, service.state.tasks_by_source

    # 用户随后新建源（同一路径）：只应提交新源的任务
    repo.put(make_source(18, str(other)))
    service.update()
    assert task_service.submitted_source_ids() == [17, 18]
    assert 17 not in service.state.tasks_by_source


def test_restore_event_reschedules_source() -> None:
    media = make_media_dir()
    service, repo, _import_repo, task_service = make_service()

    repo.put(make_source(17, str(media)))
    service.update()
    del repo.sources[17]
    on_source_trash_changed(service, None, source_id=17, kind="soft_delete")
    assert service.state.tasks_by_source == {}

    # 撤销软删：行重新可见 → 事件让调度侧重新挂载 / 建任务
    repo.put(make_source(17, str(media)))
    on_source_trash_changed(service, None, source_id=17, kind="restore")
    assert 17 in service.state.sources
    assert task_service.submitted_source_ids() == [17, 17]


# ─────────────────────────────────────────────────────────────
# 2. SourceExecutor：源失效就跳过，不写候选
# ─────────────────────────────────────────────────────────────
def test_executor_skips_when_source_is_gone() -> None:
    media = make_media_dir()
    import_repo = FakeImportRepo()
    events: list = []
    signal = Signal(name="test-scan-finished")
    signal.connect(lambda _sender, *, event: events.append(event))

    executor = SourceExecutor(
        source=make_source(17, str(media)),
        repo=import_repo,  # type: ignore[arg-type]
        seq=0,
        finished_signal=signal,
        is_source_active=lambda _sid: False,
    )
    result = executor.run()
    executor()  # 走完整流程：跳过 + 仍然发出完成事件

    assert result.skipped is True
    assert result.ok is True and result.error is None
    assert result.scanned_file_count == 0
    assert result.inserted_count == 0
    assert result.is_noop is False, "被跳过的事件不该被当成 noop"
    assert import_repo.cached == set(), import_repo.cached
    assert len(events) == 1 and events[0].skipped is True, events


def test_executor_scans_when_source_is_active() -> None:
    media = make_media_dir()
    import_repo = FakeImportRepo()
    executor = SourceExecutor(
        source=make_source(18, str(media)),
        repo=import_repo,  # type: ignore[arg-type]
        seq=1,
        finished_signal=None,
        is_source_active=lambda _sid: True,
    )
    result = executor.run()

    assert result.skipped is False
    assert result.inserted_count == 2
    assert len(import_repo.cached) == 2


def test_executor_skips_without_probe_when_probe_missing() -> None:
    """未注入存活探针（老调用方）时保持原行为：照常扫描。"""
    media = make_media_dir()
    import_repo = FakeImportRepo()
    executor = SourceExecutor(
        source=make_source(18, str(media)),
        repo=import_repo,  # type: ignore[arg-type]
        seq=2,
    )
    assert executor.run().skipped is False


# ─────────────────────────────────────────────────────────────
# 3. 仓储兜底：源在扫描途中消失时，候选被剔除而不是撞外键
# ─────────────────────────────────────────────────────────────
def test_repo_drops_candidates_for_soft_deleted_source() -> None:
    db = Connector()  # 内存库，自带 schema
    sources = IngestSourceRepository(db)
    import_repo = ImportRepository(db)

    source_id = sources.create(
        IngestSourceCreate(title="t", source_path="/tmp/albuswall-none")
    )
    assert import_repo.create_candidate_cache_batch(
        [make_candidate(source_id, "a.jpg")]
    ) == 1

    # 扫描进行中，源被软删（还没 purge）
    assert sources.soft_delete(source_id) is True

    # 不再抛 sqlite3.IntegrityError
    assert import_repo.create_candidate_cache_batch(
        [make_candidate(source_id, "b.jpg")]
    ) == 0
    assert {c.path for c in import_repo.get_pending_candidates()} == {"a.jpg"}


def test_repo_drops_candidates_for_purged_source() -> None:
    db = Connector()
    sources = IngestSourceRepository(db)
    import_repo = ImportRepository(db)

    source_id = sources.create(
        IngestSourceCreate(title="t", source_path="/tmp/albuswall-none")
    )
    assert sources.soft_delete(source_id) is True
    assert sources.purge(source_id).purged is True

    # 行已被物理删除：FK 目标不存在
    assert import_repo.create_candidate_cache_batch(
        [make_candidate(source_id, "c.jpg")]
    ) == 0
    assert list(import_repo.get_pending_candidates()) == []


# ─────────────────────────────────────────────────────────────
# 4. 端到端：真实仓储 + SourceService，复现现场时序
# ─────────────────────────────────────────────────────────────
def test_end_to_end_delete_while_scan_in_flight() -> None:
    media = make_media_dir()
    db = Connector()
    sources = IngestSourceRepository(db)
    import_repo = ImportRepository(db)
    task_service = FakeTaskService()

    service = SourceService(
        sources,
        import_repo,
        task_service,  # type: ignore[arg-type]
        state=SourceServiceState(),
    )
    source_id = sources.create(
        IngestSourceCreate(
            title="t", source_path=str(media),
            file_types=[".jpg", ".png"],
        )
    )
    service.update_source(source_id)

    executor = service.state.tasks_by_source[source_id]
    in_flight = executor  # 已提交、尚未开始执行的任务

    # 扫描跑起来 → 候选落库
    first = executor.run()
    assert first.skipped is False and first.inserted_count == 2

    # 用户删源：DB 软删 + 回收站事件
    assert sources.soft_delete(source_id) is True
    on_source_trash_changed(service, None, source_id=source_id,
                            kind="soft_delete")
    assert service.state.tasks_by_source == {}

    # 手中那个"迟到"的任务才真正开始执行：不报错、不写候选
    late = in_flight.run()
    assert late.skipped is True and late.error is None
    assert sources.purge(source_id).purged is True

    # 扫描中途源被 purge 的极端时序：仓储静默剔除，不抛外键错误
    assert import_repo.create_candidate_cache_batch(
        [make_candidate(source_id, "late.jpg")]
    ) == 0


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
