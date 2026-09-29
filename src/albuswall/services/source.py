#
"""
SourceServiceWorker 负责生成多个持久化任务（Task），
SourceFacedService 将任务提交给 TaskService。
"""

import logging
import mimetypes
from itertools import count as _count
from pathlib import Path
from pprint import pformat
from traceback import format_exc
from typing import Dict, Iterable, List, Sequence, TYPE_CHECKING, Optional, TypeVar
from uuid import uuid4

from albuswall.log import TRACE
from albuswall.repositories import (
    IngestSourceRepository, ImportRepository)
from albuswall.dto.task import Task
from albuswall.dto.import_ import AssetCandidateCacheDTO
from albuswall.dto.source import (
    MANUAL_SOURCE_ID,
    IngestSourceCreate,
    IngestSourceSyncCandidate,
    IngestSourceUpdate,
    IngestSourceViewDTO,
    SourceScanFinished,
)
from albuswall.utils.signal import Signal
from albuswall.utils.mount import auto_mount
from albuswall.utils.path import iter_files_depth_first

if TYPE_CHECKING:
    from albuswall.log import Logger
    from .task import TaskService

_logger = logging.getLogger(__name__)
# noinspection statement-effect
_logger  # type: Logger
# noinspection unresolved-references
_logger.trace = lambda msg, *args, **kwargs: _logger.log(TRACE, msg, *args, **kwargs)

_SCAN_BATCH_SIZE = 500
_T = TypeVar("_T")


def _chunked(seq: Sequence[_T], size: int) -> Iterable[Sequence[_T]]:
    """把序列切成固定大小的批次（最后一批可能更短）。"""
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


# noinspection string-conversion-without-dunder-method
class SourceExecutor:
    """扫描单个 source，把新发现的文件写入候选缓存。"""

    def __init__(
            self,
            source: IngestSourceSyncCandidate,
            repo: ImportRepository,
            seq: int,
            finished_signal: Optional[Signal] = None,
    ):
        self.source = source
        self.repo = repo
        self.seq = seq
        self._finished_signal = finished_signal

    # ------------------------------------------------------------------
    # 格式化（用于日志）
    # ------------------------------------------------------------------
    def describe(self, *, verbose: bool = False) -> str:
        base = (
            f"SourceExecutor(source_id={self.source.id}, seq={self.seq}, "
            f"path={self.source.source_path!r})"
        )
        if verbose:
            base = base[:-1] + (
                f", target={self.source.target!r}, "
                f"mount_point={self.source.mount_point!r})"
            )
        return base

    def __str__(self) -> str:
        return self.describe()

    def __format__(self, spec: str) -> str:
        if spec == "":
            return self.describe()
        if spec in ("v", "verbose"):
            return self.describe(verbose=True)
        raise ValueError(f"Unsupported format spec: {spec!r}")

    # ------------------------------------------------------------------
    # 执行入口
    # ------------------------------------------------------------------
    def __call__(self):
        result = None
        try:
            result = self.run()
        except Exception as exc:
            err = format_exc()
            _logger.error("Scan failed for source %d: %s", self.source.id, exc)
            _logger.trace(
                "Traceback for source %d:\n%s", self.source.id, err
            )
            result = self._failed(err)
        finally:
            # 无论成功 / 失败 / 无新内容，均发出一次完成信号，
            # 保证上层能可靠地收到 "该 source 已处理完毕" 的通知。
            if self._finished_signal is not None and result is not None:
                self._finished_signal.emit(result)

    def run(self) -> SourceScanFinished:
        # 关键事件：一个 source 开始扫描 → DEBUG
        _logger.debug("Scanning %s", self)

        source_path = Path(self.source.source_path).resolve()
        if not source_path.exists():
            _logger.error(
                "Source path does not exist: source_id=%d path=%s",
                self.source.id, source_path,
            )
            return self._failed(f"Source path does not exist: {source_path}")

        allowed_extensions = self.source.get_allowed_extensions()
        # 详细：可能是一个很长的集合 → TRACE
        _logger.trace(
            "Allowed extensions for source %d: %s",
            self.source.id, allowed_extensions,
        )

        abs_paths = self._collect_candidate_paths(source_path, allowed_extensions)
        total_files = len(abs_paths)
        # 关键：数量汇总 → DEBUG
        _logger.debug(
            "Source %d: matched %d file(s) under %s",
            self.source.id, total_files, source_path,
        )

        missing_paths = self._find_missing_paths(abs_paths)
        missing_count = len(missing_paths)
        # 关键：数量汇总 → DEBUG
        _logger.debug(
            "Source %d: %d/%d file(s) are new",
            self.source.id, missing_count, total_files,
        )

        inserted_count = self._insert_candidate_cache(missing_paths)

        return SourceScanFinished(
            source_id=self.source.id,
            task_seq=self.seq,
            scanned_file_count=total_files,
            new_file_count=missing_count,
            has_new_content=missing_count > 0,
            inserted_count=inserted_count,
            error=None,
        )

    # ------------------------------------------------------------------
    # 内部步骤
    # ------------------------------------------------------------------
    def _failed(self, reason: str) -> SourceScanFinished:
        return SourceScanFinished(
            source_id=self.source.id,
            task_seq=self.seq,
            scanned_file_count=0,
            new_file_count=0,
            has_new_content=False,
            error=reason,
        )

    @staticmethod
    def _collect_candidate_paths(
            source_path: Path, allowed_extensions
    ) -> List[str]:
        """收集所有符合扩展名要求的规范化绝对路径字符串。"""
        result: List[str] = []
        for file_path in iter_files_depth_first(source_path, include_dirs=False):
            if file_path.suffix.lower() not in allowed_extensions:
                # 每个文件都会触发，量大 → TRACE
                _logger.trace("Skipping non-allowed file type: %s", file_path)
                continue
            abs_fp = file_path if file_path.is_absolute() else source_path / file_path
            result.append(abs_fp.resolve().as_posix())
        return result

    def _find_missing_paths(self, abs_paths: List[str]) -> List[str]:
        """分批在数据库中查询缺失（尚未导入）的路径。"""
        missing: List[str] = []
        for batch in _chunked(abs_paths, _SCAN_BATCH_SIZE):
            found = self.repo.find_missing_paths(self.source.id, *batch)
            # 每批一条，量大 → TRACE
            _logger.trace(
                "Missing lookup batch: source=%d queried=%d missing=%d",
                self.source.id, len(batch), len(found),
            )
            missing.extend(found)
        return missing

    def _insert_candidate_cache(self, missing_abs_paths: List[str]) -> int:
        """
        将缺失的绝对路径转换为候选缓存 DTO，并分批插入数据库。

        Args:
            missing_abs_paths: 缺失文件的绝对路径字符串列表。

        Returns:
            实际写入候选缓存的记录数。
        """
        if not missing_abs_paths:
            return 0

        candidates = [
            AssetCandidateCacheDTO(
                uuid=str(uuid4()),
                path=str(Path(p)),
                source_id=self.source.id,
                mime_type=mimetypes.guess_type(p)[0] or "application/octet-stream",
            )
            for p in missing_abs_paths
        ]

        for batch in _chunked(candidates, _SCAN_BATCH_SIZE):
            self.repo.create_candidate_cache_batch(batch)
            # 每批一条 → TRACE
            _logger.trace(
                "Inserted candidate batch: source=%d size=%d",
                self.source.id, len(batch),
            )

        # 关键：结果汇总 → DEBUG
        _logger.debug(
            "Source %d: cached %d candidate(s)",
            self.source.id, len(candidates),
        )
        return len(candidates)


# noinspection PyNoneFunctionAssignment,string-conversion-without-dunder-method
class SourceServiceWorker:
    """Ingest source service，负责生成持久化任务。"""

    # 为 Task 生成唯一序列号；使用 itertools.count 避免手写自增
    _seq_counter = _count()

    def __init__(self, repo: IngestSourceRepository, import_repo: ImportRepository):
        self._repo = repo
        self._import_repo = import_repo
        self._sources: Dict[int, IngestSourceSyncCandidate] = {}
        # 按 source_id 存储任务，便于按源增删改；tasks 属性实时派生视图
        self._persist_tasks_by_source: Dict[int, Task] = {}

        # 单个 source 扫描完成后发出的跨线程安全信号
        # payload: SourceScanFinished
        self.scan_finished: Signal = Signal(name="SourceScanFinished")

    # ------------------------------------------------------------------
    # 格式化（用于日志）
    # ------------------------------------------------------------------
    def describe(self, *, verbose: bool = False) -> str:
        n_src = len(self._sources)
        n_task = len(self._persist_tasks_by_source)
        base = f"SourceServiceWorker(sources={n_src}, tasks={n_task})"
        if verbose:
            details = ", ".join(
                f"{sid}->{s.source_path!r}" for sid, s in self._sources.items()
            )
            base += f" [sources={{{details}}}]"
        return base

    def __str__(self) -> str:
        return self.describe()

    def __format__(self, spec: str) -> str:
        if spec == "":
            return self.describe()
        if spec in ("v", "verbose"):
            return self.describe(verbose=True)
        raise ValueError(f"Unsupported format spec: {spec!r}")

    # ------------------------------------------------------------------
    # 对外更新入口
    # ------------------------------------------------------------------
    def update(self):
        """
        幂等地更新内部状态和任务列表：
        - 根据仓库最新数据调整 self._sources
        - 仅对新增或配置变更的源执行自动挂载
        - 重建任务列表，使其与当前有效源一一对应
        """
        latest_valid = self._get_latest_valid_sources()
        current_ids = set(self._sources)
        latest_ids = set(latest_valid)

        # 关键汇总 → DEBUG
        _logger.debug(
            "Refreshing sources: valid=%d current=%d",
            len(latest_valid), len(current_ids),
        )
        # 详细：整份候选列表 → TRACE
        _logger.trace("Latest valid sources: %s", pformat(latest_valid))

        # 新增 / 配置变更
        for source_id, new_source in latest_valid.items():
            old_source = self._sources.get(source_id)
            if old_source is not None and not self._source_changed(old_source, new_source):
                # 配置未变化，保留原对象
                continue
            if self._try_mount(source_id, new_source):
                self._sources[source_id] = new_source
                _logger.trace("Source %d tracked: %r", source_id, new_source)
            else:
                self._sources.pop(source_id, None)

        # 删除已失效的源
        removed_ids = current_ids - latest_ids
        if removed_ids:
            # 关键汇总 → DEBUG
            _logger.debug(
                "Dropping %d stale source(s): %s",
                len(removed_ids), sorted(removed_ids),
            )
        for source_id in removed_ids:
            _logger.trace("Dropped source %d: %r", source_id, self._sources[source_id])
            del self._sources[source_id]

        self._rebuild_all_tasks()

    def update_source(self, source_id: int):
        """
        更新单个源的状态和任务。
        如果源不存在或无效，则移除其任务；否则更新配置并重建该源的任务。
        """
        latest_config = self._get_source_config(source_id)

        # 源已不存在或无效：移除
        if latest_config is None or latest_config.is_void():
            self._sources.pop(source_id, None)
            self._remove_task_for_source(source_id)
            return

        old = self._sources.get(source_id)
        if old is None or self._source_changed(old, latest_config):
            # 新增或配置变更，需要（重新）挂载
            if not self._try_mount(source_id, latest_config):
                self._sources.pop(source_id, None)
                self._remove_task_for_source(source_id)
                return
            self._sources[source_id] = latest_config
        # 配置未变化时不需要重新挂载

        self._update_task_for_source(source_id)

    # ------------------------------------------------------------------
    # 源查询 & 变更判定
    # ------------------------------------------------------------------
    def _get_latest_valid_sources(self) -> Dict[int, IngestSourceSyncCandidate]:
        """从仓库获取当前所有有效源，返回 id -> source 字典。"""
        candidates = self._repo.get_source_candidates()
        result = {c.id: c for c in candidates if c.id != 0 and not c.is_void()}
        if __debug__ and len(result) != len(candidates):
            _logger.warning(
                "Source(id=0) is virtual source, can't enable. "
                "False positive check: len(result)=%d len(candidates)=%d",
                len(result), len(candidates),
            )
        return result

    def _get_source_config(
            self, source_id: int
    ) -> Optional[IngestSourceSyncCandidate]:
        """从仓库获取单个源的最新配置（单条查询，避免全量遍历）。"""
        if source_id == MANUAL_SOURCE_ID:
            _logger.warning("Source(id=0) is virtual source, can't enable.")
            return None  # 虚拟根永不视为有效扫描源
        return self._repo.get_sync_candidate(source_id)

    @staticmethod
    def _source_changed(
            old: IngestSourceSyncCandidate, new: IngestSourceSyncCandidate
    ) -> bool:
        """判断源配置是否发生变化。"""
        return (
                old.target != new.target
                or old.mount_point != new.mount_point
                or old.source_path != new.source_path
        )

    # ------------------------------------------------------------------
    # 任务管理
    # ------------------------------------------------------------------
    def _rebuild_all_tasks(self):
        """重新为所有有效源生成任务，替换旧任务集合。"""
        self._persist_tasks_by_source.clear()
        for source_id in self._sources:
            self._update_task_for_source(source_id)

    def _update_task_for_source(self, source_id: int):
        """为指定源生成新任务（若源有效）或移除旧任务（若源无效）。"""
        source = self._sources.get(source_id)
        if source is None:
            self._remove_task_for_source(source_id)
            return

        seq = next(SourceServiceWorker._seq_counter)
        executor = SourceExecutor(
            source=source,
            repo=self._import_repo,
            seq=seq,
            finished_signal=self.scan_finished,
        )
        self._persist_tasks_by_source[source_id] = Task(
            priority=8,
            seq=seq,
            fn=executor,
            args=(),
            kwargs={},
            executor="thread",
        )
        # 关键事件 → DEBUG
        _logger.debug(
            "Scheduled scan task: source=%d seq=%d path=%s",
            source_id, seq, source.source_path,
        )

    def _remove_task_for_source(self, source_id: int):
        """移除指定源的任务。"""
        if self._persist_tasks_by_source.pop(source_id, None) is not None:
            # 关键事件 → DEBUG
            _logger.debug("Removed scan task for source %d", source_id)

    @property
    def tasks(self) -> List[Task]:
        """返回所有待执行的持久化任务。"""
        return list(self._persist_tasks_by_source.values())

    # ------------------------------------------------------------------
    # 挂载
    # ------------------------------------------------------------------
    def _try_mount(
            self, source_id: int, source: IngestSourceSyncCandidate
    ) -> bool:
        """
        按需为 source 执行自动挂载。

        - source.auto_mount 为 False：不挂载，视为成功；
        - 挂载异常：记录日志并返回 False。
        """
        if not source.auto_mount:
            return True
        try:
            self._auto_mount(source)
        except Exception as e:
            _logger.error("Auto-mount failed for source %d: %s", source_id, e)
            return False
        # 关键事件 → DEBUG
        _logger.debug(
            "Mounted source %d: %s -> %s",
            source_id, source.mount_point, source.source_path,
        )
        return True

    @staticmethod
    def _auto_mount(source: IngestSourceSyncCandidate):
        """执行自动挂载。"""
        assert source.mount_point
        assert source.target
        auto_mount(
            mount_point=Path(source.mount_point),
            mount_target=source.target,
            source_path=Path(source.source_path),
            logger=_logger,
        )


class SourceFacedService:
    """Ingest source service 门面，负责协调 worker 和任务队列。"""

    def __init__(
            self,
            source_repo: IngestSourceRepository,
            import_repo: ImportRepository,
            task_service: "TaskService",
    ):
        self._source_repo = source_repo
        self._import_repo = import_repo
        self._worker = SourceServiceWorker(source_repo, import_repo)
        self._task: "TaskService" = task_service
        self._submitted_seqs = set()

    # ------------------------------------------------------------------
    # 格式化（用于日志）
    # ------------------------------------------------------------------
    def describe(self, *, verbose: bool = False) -> str:
        if verbose:
            return (
                f"SourceFacedService(worker={self._worker:v}, "
                f"submitted_seqs={sorted(self._submitted_seqs)})"
            )
        return (
            f"SourceFacedService(submitted={len(self._submitted_seqs)}, "
            f"worker={self._worker})"
        )

    def __str__(self) -> str:
        return self.describe()

    def __format__(self, spec: str) -> str:
        if spec == "":
            return self.describe()
        if spec in ("v", "verbose"):
            return self.describe(verbose=True)
        raise ValueError(f"Unsupported format spec: {spec!r}")

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def update(self):
        """幂等地更新 worker 并仅提交新任务。"""
        self._worker.update()
        self._submit_new_tasks()

    def update_source(self, source_id: int):
        """按源更新：更新该源的任务并提交新任务。"""
        self._worker.update_source(source_id)
        self._submit_new_tasks()

    def _submit_new_tasks(self):
        """扫描当前所有任务，提交尚未提交的。"""
        tasks = self._worker.tasks  # property 实时派生，先缓存一次
        for task in tasks:
            if task.seq in self._submitted_seqs:
                continue
            self._task.submit(
                task.fn,
                *task.args,
                executor=task.executor,
                priority=task.priority,
                **task.kwargs,
            )
            self._submitted_seqs.add(task.seq)
            # 关键事件 → DEBUG
            _logger.debug("Submitted scan task: seq=%d priority=%d",
                          task.seq, task.priority)
        # 清理已不存在的任务 seq，防止 set 无限增长
        before = len(self._submitted_seqs)
        self._submitted_seqs &= {t.seq for t in tasks}
        # 详细：仅在确实裁剪时打点 → TRACE
        if len(self._submitted_seqs) != before:
            _logger.trace(
                "Pruned submitted seqs: %d -> %d",
                before, len(self._submitted_seqs),
            )

    def start(self):
        self.update()

    def create_source_default(self, source: IngestSourceCreate) -> int:
        return self._source_repo.create(source)

    def create_source(self, source: IngestSourceCreate) -> int:
        """创建导入源并立即调度一次扫描。

        Returns:
            新建 source 的 id（此前未返回，导致 UI 无法聚焦新卡片）。
        """
        new_id = self._source_repo.create(source)
        # 关键：让 worker 立即拾取新 source 并重建任务 → _submit_new_tasks 提交扫描
        self.update_source(new_id)
        return new_id

    def update_source_config(
            self, source_id: int, source: IngestSourceUpdate
    ) -> bool:
        """局部更新 + 让 worker 重新评估该源（配置可能触发重新扫描）。"""
        ok = self._source_repo.update(source_id, source)
        if ok:
            self.update_source(source_id)
        return ok

    # ------------------------------------------------------------------
    # 信号透传：外部通过 `service.scan_finished` 订阅单 source 完成事件
    # ------------------------------------------------------------------
    @property
    def scan_finished(self) -> Signal:
        """
        单个 source 扫描任务完成时触发的信号。

        handler 签名: handler(event: SourceScanFinished) -> None

        注意：
        - handler 运行在任务执行线程（executor="thread"）中，如有 UI 交互
          或非线程安全状态访问，请自行 post 到主线程（如 Qt 的
          QueuedConnection / queue.Queue）。
        - 每完成一次 SourceExecutor（即使无新内容或发生异常）都会 emit 一次。
        """
        return self._worker.scan_finished

    # ==================================================================
    # 查询：视图 DTO（纯查询，不触发 worker）
    # ==================================================================

    def list_sources(self) -> List[IngestSourceViewDTO]:
        """列出所有导入源（含虚拟根 / 手动导入源），按 id 升序。

        纯查询：转发到 repo，**不**触发 worker 更新。
        """
        return self._source_repo.list_view_dtos()

    def get_source(self, source_id: int) -> Optional[IngestSourceViewDTO]:
        """查询单条导入源视图 DTO；不存在返回 ``None``。

        纯查询：转发到 repo，**不**触发 worker 更新。
        """
        return self._source_repo.get_view_dto(source_id)

    def has_source(self, source_id: int) -> bool:
        """快捷判断导入源是否存在。纯查询，不触发 worker。"""
        return self._source_repo.exists(source_id)

    def get_sync_candidate(
            self, source_id: int
    ) -> Optional[IngestSourceSyncCandidate]:
        """单源同步候选，避免 UI 走 ``get_source_candidates()`` 全量遍历。"""
        return self._source_repo.get_sync_candidate(source_id)

    # ==================================================================
    # 写入：删除
    # ==================================================================

    def delete_source(self, source_id: int) -> bool:
        """删除导入源。

        流程：

        1. ``repo.delete(source_id)`` —— 由 repo 拒绝删除虚拟根，并在
           存在 assets 外键引用时由 DB 抛错；
        2. 删除成功则调用 ``self.update_source(source_id)``，
           让 worker 重新评估该源状态（源不存在 → 移除任务 + 清空缓存）。
        """
        deleted = self._source_repo.delete(source_id)
        if deleted:
            # 关键事件 → DEBUG
            _logger.debug("Deleted source %d, refreshing worker", source_id)
            self.update_source(source_id)
        return deleted

    # alias
    delete = delete_source
    create = create_source

# alias
SourceService = SourceFacedService
