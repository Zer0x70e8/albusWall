#
"""
SourceServiceWorker 负责生成多个持久化任务（Task），
SourceFacedService 将任务提交给 TaskService。
"""

import logging
import mimetypes
from pathlib import Path
from pprint import pformat
from traceback import format_exc
from typing import Dict, List, TYPE_CHECKING, Optional
from uuid import uuid4

from albuswall.log import TRACE
from albuswall.repositories import (
    IngestSourceRepository, ImportRepository)
from albuswall.dto.source import (
    IngestSourceSyncCandidate, IngestSourceCreate, IngestSourceUpdate)
from albuswall.dto.task import Task
from albuswall.dto.import_ import AssetCandidateCacheDTO
from albuswall.dto.source import SourceScanFinished
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


# noinspection string-conversion-without-dunder-method
class SourceExecutor:
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

    def __call__(self):
        result: Optional[SourceScanFinished] = None
        # noinspection PyBroadException
        try:
            result = self.run()
        except Exception:
            _logger.error(format_exc())
            result = SourceScanFinished(
                source_id=self.source.id,
                task_seq=self.seq,
                scanned_file_count=0,
                new_file_count=0,
                has_new_content=False,
                error=format_exc(),
            )
        finally:
            # 无论成功 / 失败 / 无新内容，均发出一次完成信号，
            # 保证上层能可靠地收到"该 source 已处理完毕"的通知。
            if self._finished_signal is not None and result is not None:
                self._finished_signal.emit(result)

            # # 任务结束时关闭当前线程数据库连接/会话
            # self.repo.close_current_thread()
            # _logger.trace(f"Database connection closed for source id={self.source.id}")

    def run(self) -> SourceScanFinished:
        _logger.trace(f"Executing persist task for source id={self.source.id}, "
                      f"path={self.source.source_path}, target={self.source.target}")

        source_path = Path(self.source.source_path).resolve()
        if not source_path.exists():
            _logger.error(f"Source path {source_path} does not exist for source {self.source.id}")
            return SourceScanFinished(
                source_id=self.source.id,
                task_seq=self.seq,
                scanned_file_count=0,
                new_file_count=0,
                has_new_content=False,
                error=f"Source path does not exist: {source_path}",
            )

        allowed_extensions = self.source.get_allowed_extensions()
        _logger.debug(f"Allowed extensions for source {self.source.id}: {allowed_extensions}")

        # 收集并生成规范化的绝对路径字符串
        abs_paths = []
        for file_path in iter_files_depth_first(source_path, include_dirs=False):
            if file_path.suffix.lower() in allowed_extensions:
                abs_fp = file_path if file_path.is_absolute() else source_path / file_path
                abs_str = abs_fp.resolve().as_posix()
                abs_paths.append(abs_str)
            else:
                _logger.trace(f"Skipping non-allowed file type: {file_path}")

        total_files = len(abs_paths)
        _logger.trace(f"Found {total_files} files with allowed extensions for source {self.source.id}")

        # 分批检查数据库中缺失的路径
        missing_paths = []
        batch_size = 500
        for i in range(0, len(abs_paths), batch_size):
            batch = abs_paths[i:i + batch_size]
            missing_batch = self.repo.find_missing_paths(self.source.id, *batch)
            missing_paths.extend(missing_batch)

        missing_count = len(missing_paths)
        _logger.trace(f"Source {self.source.id}: scanned {total_files} files, "
                      f"found {missing_count} new files to import.")

        # 将缺失路径转换为候选缓存 DTO 并批量插入
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

    def _insert_candidate_cache(
            self,
            missing_abs_paths: List[str],
    ) -> int:
        """
        将缺失的绝对路径转换为候选缓存 DTO，并分批插入数据库。

        Args:
            missing_abs_paths: 缺失文件的绝对路径字符串列表。

        Returns:
            实际写入候选缓存的记录数。
        """
        if not missing_abs_paths:
            return 0

        # 准备 DTO 列表
        candidates: List[AssetCandidateCacheDTO] = []
        for abs_path_str in missing_abs_paths:
            abs_path = Path(abs_path_str)

            # 生成 UUID（可根据需要改为确定性 UUID）
            candidate_uuid = str(uuid4())

            # 推断 MIME 类型
            mime_type, _ = mimetypes.guess_type(abs_path_str)
            if mime_type is None:
                # 降级：根据扩展名自定义映射或使用通用类型
                mime_type = 'application/octet-stream'

            candidates.append(
                AssetCandidateCacheDTO(
                    uuid=candidate_uuid,
                    path=str(abs_path),
                    source_id=self.source.id,
                    mime_type=mime_type
                )
            )

        # 分批插入
        insert_batch_size = 500
        for i in range(0, len(candidates), insert_batch_size):
            batch = candidates[i:i + insert_batch_size]
            self.repo.create_candidate_cache_batch(batch)

        _logger.trace(
            f"Inserted {len(candidates)} candidate cache records for source {self.source.id}.")

        return len(candidates)


# noinspection PyNoneFunctionAssignment,string-conversion-without-dunder-method
class SourceServiceWorker:
    """Ingest source service，负责生成持久化任务。"""

    _seq_counter = 0  # 类变量，用于为 Task 生成唯一序列号

    def __init__(self, repo: IngestSourceRepository, import_repo: ImportRepository):
        self._repo = repo
        self._import_repo = import_repo
        self._sources: Dict[int, IngestSourceSyncCandidate] = {}
        # 改为字典存储，便于按源更新
        self._persist_tasks_by_source: Dict[int, Task] = {}
        self._persist_tasks: List[Task] = []  # 保持兼容，作为视图

        # 新增：单个 source 扫描完成后发出的跨线程安全信号
        # payload: SourceScanFinished
        self.scan_finished: Signal = Signal(name="SourceScanFinished")

    def update(self):
        """
        幂等地更新内部状态和任务列表：
        - 根据仓库最新数据调整 self._sources
        - 仅对新增或配置变更的源执行自动挂载
        - 重建 self._persist_tasks 使其与当前有效源一一对应
        """
        latest_valid = self._get_latest_valid_sources()
        current_ids = set(self._sources.keys())
        new_ids = set(latest_valid.keys()) - current_ids
        removed_ids = current_ids - set(latest_valid.keys())

        _logger.debug(f"Latest valid source candidates count: {len(latest_valid)}")
        _logger.trace(f"Latest valid source candidates: {pformat(latest_valid)}")

        # 处理新增源
        for id_ in new_ids:
            source = latest_valid[id_]
            self._sources[id_] = source
            if source.auto_mount:
                try:
                    self._auto_mount(id_)
                    _logger.trace(f"Mounted new source(id={id_}, "
                                  f"target={source.target},"
                                  f"point={source.mount_point}).")
                except Exception as e:
                    _logger.error(f"Mount failed for new source {id_}: {e}")
                    del self._sources[id_]

        # 处理已存在但可能配置变化的源
        for id_ in current_ids & set(latest_valid.keys()):
            old_source = self._sources[id_]
            new_source: IngestSourceSyncCandidate = latest_valid[id_]
            if self._source_changed(old_source, new_source):
                self._sources[id_] = new_source
                if new_source.auto_mount:
                    try:
                        # Note：真实场景下可能需要先卸载旧挂载点，这里简化处理
                        self._auto_mount(id_)
                        _logger.trace(f"Mounted source(id={id_}, "
                                      f"target={new_source.target},"
                                      f"point={new_source.mount_point}).")
                    except Exception as e:
                        _logger.error(f"Remount failed for source {id_}: {e}")
                        del self._sources[id_]
                        continue
            # 配置未变化则保留原对象

        # 处理删除的源
        _logger.debug(f"Failed source candidates count: {len(removed_ids)}")
        for id_ in removed_ids:
            _logger.trace(f"Failed source candidate: {self._sources[id_]}")
            del self._sources[id_]

        # 重建所有任务
        self._rebuild_all_tasks()

    def update_source(self, source_id: int):
        """
        更新单个源的状态和任务。
        如果源不存在或无效，则移除其任务；否则更新配置并重建该源的任务。
        """
        latest_config = self._get_source_config(source_id)

        # 源已不存在或无效：移除
        if latest_config is None or latest_config.is_void():
            if source_id in self._sources:
                del self._sources[source_id]
            self._remove_task_for_source(source_id)
            return

        # 源新增
        if source_id not in self._sources:
            self._sources[source_id] = latest_config
            if latest_config.auto_mount:
                try:
                    self._auto_mount(source_id)
                    _logger.trace(f"Mounted new source(id={source_id}, "
                                  f"target={latest_config.target},"
                                  f"point={latest_config.mount_point}).")
                except Exception as e:
                    _logger.error(f"Mount failed for source {source_id}: {e}")
                    del self._sources[source_id]
                    self._remove_task_for_source(source_id)
                    return
        else:
            # 源已存在，检查配置变化
            old = self._sources[source_id]
            if self._source_changed(old, latest_config):
                self._sources[source_id] = latest_config
                if latest_config.auto_mount:
                    try:
                        self._auto_mount(source_id)
                        _logger.trace(f"Remounted source(id={source_id}, "
                                      f"target={latest_config.target},"
                                      f"point={latest_config.mount_point}).")
                    except Exception as e:
                        _logger.error(f"Remount failed for source {source_id}: {e}")
                        del self._sources[source_id]
                        self._remove_task_for_source(source_id)
                        return
            # 配置未变化，不需要重新挂载

        # 更新该源的任务
        self._update_task_for_source(source_id)

    def _get_latest_valid_sources(self) -> Dict[int, IngestSourceSyncCandidate]:
        """从仓库获取当前所有有效源，返回 id -> source 字典"""
        candidates = self._repo.get_source_candidates()
        return {c.id: c for c in candidates if not c.is_void()}

    def _get_source_config(self, source_id: int) -> Optional[IngestSourceSyncCandidate]:
        """从仓库获取单个源的最新配置（通过全部候选过滤实现）"""
        candidates = self._repo.get_source_candidates()
        for c in candidates:
            if c.id == source_id:
                return c
        return None

    @staticmethod
    def _source_changed(old: IngestSourceSyncCandidate,
                        new: IngestSourceSyncCandidate) -> bool:
        """判断源配置是否发生变化"""
        return (old.target != new.target or
                old.mount_point != new.mount_point or
                old.source_path != new.source_path)

    def _rebuild_all_tasks(self):
        """重新为所有有效源生成任务，替换旧任务集合"""
        self._persist_tasks_by_source.clear()
        for source_id in self._sources:
            self._update_task_for_source(source_id)
        self._persist_tasks = list(self._persist_tasks_by_source.values())

    def _update_task_for_source(self, source_id: int):
        """为指定源生成新任务（若源有效）或移除旧任务（若源无效）"""
        if source_id not in self._sources:
            # 源无效，移除任务
            if source_id in self._persist_tasks_by_source:
                del self._persist_tasks_by_source[source_id]
                self._persist_tasks = list(self._persist_tasks_by_source.values())
            return

        source = self._sources[source_id]
        seq = SourceServiceWorker._seq_counter
        SourceServiceWorker._seq_counter += 1

        executor = SourceExecutor(
            source=source,
            repo=self._import_repo,
            seq=seq,
            finished_signal=self.scan_finished,
        )

        task = Task(
            priority=8,
            seq=seq,
            fn=executor,
            args=(),
            kwargs={},
            executor="thread",
        )
        self._persist_tasks_by_source[source_id] = task
        self._persist_tasks = list(self._persist_tasks_by_source.values())
        _logger.debug(
            f"Created/updated persist task for source(id={source_id}), seq={seq}")

    def _remove_task_for_source(self, source_id: int):
        """移除指定源的任务"""
        if source_id in self._persist_tasks_by_source:
            del self._persist_tasks_by_source[source_id]
            self._persist_tasks = list(self._persist_tasks_by_source.values())

    @property
    def tasks(self) -> List[Task]:
        """返回所有待执行的持久化任务"""
        return self._persist_tasks

    def _auto_mount(self, id_: int):
        """执行自动挂载（原方法保持不变）"""
        current = self._sources[id_]
        assert current.mount_point
        assert current.target
        mount_point = Path(current.mount_point)
        mount_target = current.target
        source_path = Path(current.source_path)

        auto_mount(
            mount_point=mount_point,
            mount_target=mount_target,
            source_path=source_path,
            logger=_logger,
        )


class SourceFacedService:
    """Ingest source service 门面，负责协调 worker 和任务队列。"""

    def __init__(self,
                 source_repo: IngestSourceRepository,
                 import_repo: ImportRepository,
                 task_service: "TaskService"):
        self._source_repo = source_repo
        self._import_repo = import_repo
        self._worker = SourceServiceWorker(source_repo, import_repo)
        self._task: "TaskService" = task_service
        self._submitted_seqs = set()

    def update(self):
        """幂等地更新 worker 并仅提交新任务"""
        self._worker.update()
        self._submit_new_tasks()

    def update_source(self, source_id: int):
        """按源更新：更新该源的任务并提交新任务"""
        self._worker.update_source(source_id)
        self._submit_new_tasks()

    def _submit_new_tasks(self):
        """扫描当前所有任务，提交尚未提交的"""
        for task in self._worker.tasks:
            if task.seq not in self._submitted_seqs:
                self._task.submit(
                    task.fn,
                    *task.args,
                    executor=task.executor,
                    priority=task.priority,
                    **task.kwargs
                )
                self._submitted_seqs.add(task.seq)
        # 清理已不存在的任务 seq，防止 set 无限增长
        valid_seqs = {t.seq for t in self._worker.tasks}
        self._submitted_seqs &= valid_seqs

    def start(self):
        self.update()

    def create_source(self, source: IngestSourceCreate):
        self._source_repo.create(source)

    def update_source_config(self, source_id: int, source: IngestSourceUpdate):
        # 注意：与 update_source 方法名称冲突，这里改为 update_source_config 避免混淆
        self._source_repo.update(source_id, source)

    # alias
    create = create_source

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


# alias
SourceService = SourceFacedService
