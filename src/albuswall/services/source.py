#
"""SourceService：导入源扫描与调度服务。

设计要点
--------
* **单类 + 单 state**：原先 ``SourceServiceWorker`` 持有一半状态
  （``_sources`` / ``_persist_tasks_by_source``），``SourceFacedService``
  持有另一半（``_submitted_seqs``），任何一处更新路径都要同时改两个对象，
  否则就会出现"看得到源但看不到任务"或反之。现在所有可变状态收敛到
  :class:`SourceServiceState`，:class:`SourceService` **只持有该对象的引用**。

* **不做跨线程同步**：state 容器本身不加锁。若调用方从多线程访问，
  请自行串行化（例如统一走事件循环，或外部加锁）。这与旧实现一致，
  只是把"要同步哪几块"从隐式知识变成了显式的对象。

* **约定**：
    - 读模型统一为 :class:`IngestSource`（``IngestSourceSyncCandidate`` /
      ``IngestSourceViewDTO`` 是它的过渡别名）；
    - PATCH 语义走 ``UNSET`` / ``None`` 二分（见 ``dto.sentinel``）；
    - 状态字面量走枚举，禁止裸字符串；
    - "是否启用"是 DB 列（``ingest_source.disabled``），不再有内存 set 副本。
"""

from __future__ import annotations

import mimetypes
from itertools import count as _count
from pathlib import Path
from pprint import pformat
from traceback import format_exc
from typing import (
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    TYPE_CHECKING,
    TypeVar,
)
from uuid import uuid4

from albuswall.repositories import (
    IngestSourceRepository,
    ImportRepository,
)
from albuswall.configue.state import (
    ObservableNamespace,
    StateField,
    ObservableContext
)
from albuswall.log import getLogger
from albuswall.dto.task import ExecutorType, Task
from albuswall.dto.import_ import AssetCandidateCacheDTO
from albuswall.dto.source import (
    MANUAL_SOURCE_ID,
    IngestSource,
    IngestSourceCreate,
    IngestSourceUpdate,
    SourceScanFinished,
)
from albuswall.utils.signals import Signal
from albuswall.utils.mount import auto_mount
from albuswall.utils.path import iter_files_depth_first

if TYPE_CHECKING:
    from albuswall.infrastructure.task import TaskService

_logger = getLogger(__name__)

_SCAN_BATCH_SIZE = 500
_T = TypeVar("_T")


def _chunked(seq: Sequence[_T], size: int) -> Iterable[Sequence[_T]]:
    """把序列切成固定大小的批次（最后一批可能更短）。"""
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


class SourceServiceState(ObservableNamespace):
    """SourceService 的全部可变状态容器。

    与 ObservableNamespace 的关系：
      * 字段用 StateField 声明 → 类型校验 / 默认值语义和别的 state 子树一致；
      * 写字段 → 走 _ctx.mark_dirty → 触发 observe 回调（UI 可订阅）；
      * **不**挂到 DynamicConfig、**不** autosave、**不**落盘 —— 值里含运行时对象
        （Source 实例、Task 句柄……），天生不可序列化，因此继承 ObservableNamespace
        而不是 StatefulNamespace。
    """

    # ── 声明式字段 ─────────────────────────────────
    sources = StateField(dict)  # nested 推断为 False → 原样存
    tasks_by_source = StateField(dict)  # False
    submitted_seqs = StateField(set)  # False

    #
    _ctx: ObservableContext
    _initializing: bool

    def __init__(self, *, _ctx=None, **entries):
        entries.setdefault("sources", {})
        entries.setdefault("tasks_by_source", {})
        entries.setdefault("submitted_seqs", set())
        super().__init__(_ctx=_ctx, **entries)

    # 不再需要 _PLAIN_FIELDS
    # 不再需要 __setattr__ 重写
    # 不再需要手动 mark_dirty —— 走基类写路径即可

    # ── 视图 ──────────────────────────────────────
    @property
    def source_ids(self) -> set[int]:
        return set(self.sources)

    @property
    def tasks(self) -> list:
        return list(self.tasks_by_source.values())

    # ── 变更（原地操作需手动标脏）───────────────
    def clear_tasks(self) -> None:
        if self.tasks_by_source:
            self.tasks_by_source.clear()
            self._ctx.mark_dirty(self, "tasks_by_source")

    def drop_source(self, source_id: int) -> None:
        if self.sources.pop(source_id, None) is not None:
            self._ctx.mark_dirty(self, "sources")
        if self.tasks_by_source.pop(source_id, None) is not None:
            self._ctx.mark_dirty(self, "tasks_by_source")

    def prune_submitted(self, alive_seqs: set[int]) -> int:
        stale = self.submitted_seqs - alive_seqs
        if stale:
            self.submitted_seqs &= alive_seqs
            self._ctx.mark_dirty(self, "submitted_seqs")
        return len(stale)

    #
    def describe(self, *, verbose: bool = False) -> str:
        base = (
            f"SourceServiceState(sources={len(self.sources)}, "
            f"tasks={len(self.tasks_by_source)}, "
            f"submitted={len(self.submitted_seqs)})"
        )
        if verbose:
            details = ", ".join(
                f"{sid}->{s.source_path!r}" for sid, s in self.sources.items()
            )
            base += f" [sources={{{details}}}]"
        return base

    def __str__(self) -> str:
        return self.describe()

    def __repr__(self) -> str:
        return self.describe()

    def __format__(self, spec: str) -> str:
        if spec == "":
            return self.describe()
        if spec in ("v", "verbose"):
            return self.describe(verbose=True)
        raise ValueError(f"Unsupported format spec: {spec!r}")


# noinspection string-conversion-without-dunder-method
class SourceExecutor:
    """扫描单个 source，把新发现的文件写入候选缓存。"""

    def __init__(
            self,
            source: IngestSource,
            repo: ImportRepository,
            seq: int,
            finished_signal: Optional[Signal] = None,
    ):
        self.source = source
        self.repo = repo
        self.seq = seq
        self._finished_signal = finished_signal

    # log
    def describe(self, *, verbose: bool = False) -> str:
        base = (
            f"SourceExecutor(source_id={self.source.id}, "
            f"seq={self.seq}, "
            f"path={self.source.source_path!r})"
        )
        if verbose:
            base = base[:-1] + (
                f", target={self.source.target_path!r}, "
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

    # run
    def __call__(self) -> None:
        result: Optional[SourceScanFinished] = None
        try:
            result = self.run()
        except Exception as exc:
            err = format_exc()
            _logger.error("Scan failed for source %d: %s", self.source.id, exc)
            _logger.trace("Traceback for source %d:\n%s", self.source.id, err)
            result = self._failed(err)
        finally:
            # 无论成功 / 失败 / 无新内容，均发出一次完成信号，
            # 保证上层能可靠收到 "该 source 已处理完毕" 的通知。
            if self._finished_signal is not None and result is not None:
                self._finished_signal.emit(result)

    def run(self) -> SourceScanFinished:
        _logger.debug("Scanning %s", self)

        source_path = Path(self.source.source_path).resolve()
        if not source_path.exists():
            _logger.error(
                "Source path does not exist: source_id=%d path=%s",
                self.source.id, source_path,
            )
            return self._failed(f"Source path does not exist: {source_path}")

        allowed_extensions = self.source.get_allowed_extensions()
        _logger.trace(
            "Allowed extensions for source %d: %s",
            self.source.id, allowed_extensions,
        )

        abs_paths = self._collect_candidate_paths(source_path, allowed_extensions)
        total_files = len(abs_paths)
        _logger.debug(
            "Source %d: matched %d file(s) under %s",
            self.source.id, total_files, source_path,
        )

        missing_paths = self._find_missing_paths(abs_paths)
        missing_count = len(missing_paths)
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

    #
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
            source_path: Path, allowed_extensions: Set[str]
    ) -> List[str]:
        """收集所有符合扩展名要求的规范化绝对路径字符串。"""
        result: List[str] = []
        for file_path in iter_files_depth_first(source_path, include_dirs=False):
            if file_path.suffix.lower() not in allowed_extensions:
                _logger.trace("Skipping non-allowed file type: %s", file_path)
                continue
            abs_fp = (
                file_path
                if file_path.is_absolute()
                else source_path / file_path
            )
            result.append(abs_fp.resolve().as_posix())
        return result

    def _find_missing_paths(self, abs_paths: List[str]) -> List[str]:
        """分批在数据库中查询缺失（尚未导入）的路径。"""
        missing: List[str] = []
        for batch in _chunked(abs_paths, _SCAN_BATCH_SIZE):
            found = self.repo.find_missing_paths(self.source.id, *batch)
            _logger.trace(
                "Missing lookup batch: source=%d queried=%d missing=%d",
                self.source.id, len(batch), len(found),
            )
            missing.extend(found)
        return missing

    def _insert_candidate_cache(self, missing_abs_paths: List[str]) -> int:
        """把缺失的绝对路径转换为候选 DTO，分批写入候选缓存。

        Returns:
            实际提交插入的候选记录数。
        """
        if not missing_abs_paths:
            return 0

        candidates = [
            AssetCandidateCacheDTO(
                uuid=str(uuid4()),
                path=str(Path(p)),
                source_id=self.source.id,
                mime_type=(
                        mimetypes.guess_type(p)[0]
                        or "application/octet-stream"
                ),
            )
            for p in missing_abs_paths
        ]

        for batch in _chunked(candidates, _SCAN_BATCH_SIZE):
            self.repo.create_candidate_cache_batch(batch)
            _logger.trace(
                "Inserted candidate batch: source=%d size=%d",
                self.source.id, len(batch),
            )

        _logger.debug(
            "Source %d: cached %d candidate(s)",
            self.source.id, len(candidates),
        )
        return len(candidates)


# noinspection PyNoneFunctionAssignment,string-conversion-without-dunder-method
class SourceService:
    """导入源服务门面：状态收敛到 :class:`SourceServiceState`。

    对外 API
    --------
    * :meth:`start` / :meth:`update` / :meth:`update_source`
    * :meth:`create_source` / :meth:`update_source_config` / :meth:`delete_source`
    * :meth:`enable_source` / :meth:`disable_source`
    * :meth:`list_sources` / :meth:`get_source` / :meth:`has_source`
    * :attr:`scan_finished`（单 source 扫描完成信号）
    * :attr:`state`（唯一可变状态；调用方自行保证线程安全）

    启用 / 禁用语义
    --------------
    "是否启用"是 ``ingest_source.disabled`` 这一 DB 列，没有内存 set 副本。
    ``enable_source`` / ``disable_source`` 走 ``update_source_config`` →
    repo.update → ``update_source``，由 ``IngestSource.is_void()`` 的
    ``disabled`` 短路分支驱动 ``_drop_source``。禁用后的源不出现在
    ``state.sources`` 里，不参与调度。
    """
    source_added = Signal(name="SourceAdded")
    source_updated = Signal(name="SourceUpdated")
    source_removed = Signal(name="SourceRemoved")

    # 类级 seq 计数器：跨实例保证 seq 唯一（防止一次误重建把旧 seq 复用）。
    _seq_counter = _count()

    def __init__(
            self,
            source_repo: IngestSourceRepository,
            import_repo: ImportRepository,
            task_service: "TaskService",
            *,
            state: Optional[SourceServiceState] = None,
    ):
        self._source_repo = source_repo
        self._import_repo = import_repo
        self._task_service = task_service

        # 唯一可变状态容器；允许外部注入以便测试 / 与其他组件共享。
        self.state: SourceServiceState = state or SourceServiceState()

        # 单 source 扫描完成信号；payload: SourceScanFinished
        self.scan_finished: Signal = Signal(name="SourceScanFinished")

        #
        self._closed: bool = False

    # ------------------------------------------------------------------ 日志

    def describe(self, *, verbose: bool = False) -> str:
        if verbose:
            return (
                f"SourceService(state={self.state:v}, "
                f"submitted={sorted(self.state.submitted_seqs)})"
            )
        return f"SourceService(state={self.state})"

    def __str__(self) -> str:
        return self.describe()

    def __format__(self, spec: str) -> str:
        if spec == "":
            return self.describe()
        if spec in ("v", "verbose"):
            return self.describe(verbose=True)
        raise ValueError(f"Unsupported format spec: {spec!r}")

    # ------------------------------------------------------------------ 视图
    # 只读便利属性；底层引用与 state 共享。

    @property
    def sources(self) -> Mapping[int, IngestSource]:
        return self.state.sources

    @property
    def tasks(self) -> List[Task]:
        return self.state.tasks

    # ==================================================================
    # 生命周期
    # ==================================================================

    def start(self) -> None:
        """首次启动：等价于一次完整 :meth:`update`。"""
        self.update()

    def shutdown(self) -> None:
        """关闭服务。

        * 幂等：多次调用无副作用；
        * 不清空 scan_finished 的订阅者（订阅生命周期归订阅方）；
        * 不通知 TaskService，不取消在途任务。

        关闭后：update / update_source / create / delete / update_source_config
        / enable_source / disable_source 全部 no-op（写操作返回 False 或 None，
        无返回值的直接 return）。
        读操作（list_sources / get_source / has_source / get_sync_candidate）
        继续可用 —— 它们只转发 repo，不涉及本服务状态。
        """
        if self._closed:
            return
        self._closed = True

        # 释放引用，便于 GC；也避免误用
        self.state.sources.clear()
        self.state.tasks_by_source.clear()
        self.state.submitted_seqs.clear()

        _logger.debug("SourceService shut down")

    def update(self) -> None:
        """全量刷新：

        1. 从 repo 取当前所有有效源；
        2. 与 ``state.sources`` diff（新增 / 变更 / 删除）；
        3. 重建全部任务；
        4. 提交尚未提交的任务。
        """
        if self._closed:
            _logger.debug("update() ignored: SourceService is closed")
            return

        latest = self._fetch_latest_valid_sources()
        _logger.debug(
            "Refreshing sources: valid=%d current=%d",
            len(latest), len(self.state.sources),
        )
        _logger.trace("Latest valid sources: %s", pformat(latest))

        self._apply_full_snapshot(latest)
        self._rebuild_all_tasks()
        self._submit_new_tasks()

    def update_source(self, source_id: int) -> None:
        """单源刷新：拉取最新配置 → 评估有效性 → 视情况重建该源的任务。

        重建条件（与 :meth:`_source_changed` 对齐）：

        * 源是首次出现（``old is None``）；
        * ``target_path`` / ``mount_point`` / ``source_path`` 任一发生变化。

        仅展示字段（``title`` / ``description`` / ``tags`` / ``trigger_config``）
        变化时：

        * 刷新 ``state.sources`` 快照，让后续 reader 看到新值；
        * **不**重建任务 —— 已在途 / 已提交的扫描任务保持原 seq 不动，
          避免"改个标题就重扫一遍"。

        源不存在 / 无效（含 ``disabled=True``） / 挂载失败时：
        从 state 移除其跟踪记录与任务。无论走哪条分支，最后都会尝试提交新任务。
        """
        if self._closed:
            _logger.debug("update() ignored: SourceService is closed")
            return

        config = self._fetch_source_config(source_id)

        if config is None or config.is_void():
            self._drop_source(source_id)
            self._submit_new_tasks()
            return

        old = self.state.sources.get(source_id)

        if old is None or self._source_changed(old, config):
            # 新增 / 扫描行为变更：需要（重新）挂载 + 重建任务
            if not self._try_mount(source_id, config):
                self._drop_source(source_id)
                self._submit_new_tasks()
                return
            self.state.sources[source_id] = config
            self._rebuild_task_for_source(source_id)
        elif old != config:
            # 仅展示字段变化：刷新快照，保留原任务与 seq
            self.state.sources[source_id] = config
        # else: 完全未变 —— 保留原对象引用，无操作

        self._submit_new_tasks()

    # ==================================================================
    # 写入 API
    # ==================================================================

    def create_source(self, source: IngestSourceCreate) -> Optional[int]:
        """创建导入源并立即调度一次扫描。

        Returns:
            新建 source 的 id；服务已关闭时返回 ``None``。
        """
        if self._closed:
            _logger.debug("write op ignored: SourceService is closed")
            return None

        new_id = self._source_repo.create(source)
        _logger.debug("Created source %d, scheduling initial scan", new_id)
        self.update_source(new_id)
        self.source_added.emit(new_id)
        return new_id

    def update_source_config(
            self, source_id: int, source: IngestSourceUpdate
    ) -> bool:
        """局部更新 + 让 worker 重新评估该源。

        Returns:
            ``True`` 表示确实写入了 DB（``False`` 表示 DTO 全是 ``UNSET``
            或 id 不存在，或服务已关闭）；写入成功才会触发重扫评估。
        """
        if self._closed:
            _logger.debug("write op ignored: SourceService is closed")
            return False

        ok = self._source_repo.update(source_id, source)
        if ok:
            self.update_source(source_id)
            self.source_updated.emit(source_id)
        return ok

    def enable_source(self, source_id: int) -> bool:
        """启用导入源（幂等）。

        走与 ``update_source_config`` 相同的链路：写库 → ``update_source``
        → 源变为 "非 void" → 走 "新增" 分支（重挂载 + 建任务）。
        """
        return self.update_source_config(
            source_id, IngestSourceUpdate(disabled=False)
        )

    def disable_source(self, source_id: int) -> bool:
        """禁用导入源（幂等）。

        禁用后 ``IngestSource.is_void()`` 短路返回 ``True``，
        ``update_source`` 走 ``_drop_source`` 把该源从跟踪集里剔除。
        DB 行保留 —— 禁用是逻辑状态，不是删除。
        """
        return self.update_source_config(
            source_id, IngestSourceUpdate(disabled=True)
        )

    def delete_source(self, source_id: int) -> bool:
        """删除导入源；删除成功则同步刷新 worker 状态。

        repo 侧负责拒绝虚拟根 / 有 assets 引用的情况，这里只做流程编排。
        """
        if self._closed:
            _logger.debug("write op ignored: SourceService is closed")
            return False

        # TODO impl service
        # deleted = self._source_repo.delete(source_id)
        # if deleted:
        #     _logger.debug("Deleted source %d, refreshing worker", source_id)
        #     self.update_source(source_id)
        #     self.source_removed.emit(source_id)
        # return deleted
        return False

    # alias
    create = create_source
    delete = delete_source

    # ==================================================================
    # 查询 API（纯查询，不触发 worker）
    # ==================================================================

    def list_sources(self) -> List[IngestSource]:
        """列出所有导入源（含虚拟根 / 手动导入源），按 id 升序。"""
        return self._source_repo.list_view_dtos()

    def get_source(self, source_id: int) -> Optional[IngestSource]:
        """查询单条导入源；不存在返回 ``None``。"""
        return self._source_repo.get_view_dto(source_id)

    def has_source(self, source_id: int) -> bool:
        """快捷判断导入源是否存在。"""
        return self._source_repo.exists(source_id)

    def get_sync_candidate(self, source_id: int) -> Optional[IngestSource]:
        """单源同步候选，避免 UI 走 ``get_source_candidates()`` 全量遍历。"""
        return self._source_repo.get_sync_candidate(source_id)

    # ==================================================================
    # 内部：源读取 / 变更判定
    # ==================================================================

    def _fetch_latest_valid_sources(self) -> Dict[int, IngestSource]:
        """从 repo 取所有有效源，返回 ``{id: IngestSource}``。

        ``is_void()`` 已把 ``disabled=True`` 判为不可用，因此这里天然过滤
        掉所有被禁用的源，不需要额外的集合。
        """
        candidates = self._source_repo.get_source_candidates()
        result = {
            c.id: c
            for c in candidates
            if c.id != MANUAL_SOURCE_ID and not c.is_void()
        }
        return result

    def _fetch_source_config(
            self, source_id: int
    ) -> Optional[IngestSource]:
        """单源配置读取；虚拟根显式返回 ``None``。"""
        if source_id == MANUAL_SOURCE_ID:
            _logger.warning("Source(id=0) is virtual source, can't enable.")
            return None
        return self._source_repo.get_sync_candidate(source_id)

    @staticmethod
    def _source_changed(old: IngestSource, new: IngestSource) -> bool:
        """判断需要触发重扫/重挂载的字段是否变化。

        仅比较影响"扫描行为"的字段：

        * ``title`` / ``description`` / ``tags`` —— 纯展示字段，不触发；
        * ``trigger_config``                    —— 调度层字段，不触发；
        * ``disabled``                          —— 由 ``is_void`` 短路在
          上游处理，不触发（禁用走 drop、启用走新增，都进不了这条分支）。
        """
        return (
                old.target_path != new.target_path
                or old.mount_point != new.mount_point
                or old.source_path != new.source_path
        )

    # ==================================================================
    # 内部：状态迁移
    # ==================================================================

    def _apply_full_snapshot(self, latest: Dict[int, IngestSource]) -> None:
        """把 ``latest`` 应用到 ``state.sources``（新增 / 变更 / 删除）。

        未变化的源保留原对象引用，避免下游缓存的 key 被无谓打断。
        """
        current_ids = self.state.source_ids
        latest_ids = set(latest)

        for source_id, new_source in latest.items():
            old = self.state.sources.get(source_id)
            if old is not None and not self._source_changed(old, new_source):
                continue

            if self._try_mount(source_id, new_source):
                self.state.sources[source_id] = new_source
                _logger.trace(
                    "Source %d tracked: %s",
                    source_id,
                    pformat(new_source)
                )
            else:
                self.state.sources.pop(source_id, None)

        removed_ids = current_ids - latest_ids
        if removed_ids:
            _logger.debug(
                "Dropping %d stale source(s): %s",
                len(removed_ids), sorted(removed_ids),
            )
        for source_id in removed_ids:
            self.state.sources.pop(source_id, None)

    def _drop_source(self, source_id: int) -> None:
        """把源从 state 中彻底移除（跟踪记录 + 任务）。"""
        self.state.drop_source(source_id)

    # ==================================================================
    # 内部：任务管理
    # ==================================================================

    def _rebuild_all_tasks(self) -> None:
        """丢弃全部任务，为当前所有跟踪源重建。"""
        self.state.clear_tasks()
        for source_id in self.state.sources:
            self._schedule_task_for_source(source_id)

    def _rebuild_task_for_source(self, source_id: int) -> None:
        """仅重建指定源的任务；源已被移除时删除其任务。"""
        if source_id not in self.state.sources:
            self.state.tasks_by_source.pop(source_id, None)
            return
        self._schedule_task_for_source(source_id)

    def _schedule_task_for_source(self, source_id: int) -> None:
        """为有效源生成新的 Task，覆盖旧任务（若有）。"""
        source = self.state.sources[source_id]
        seq = next(SourceService._seq_counter)

        executor = SourceExecutor(
            source=source,
            repo=self._import_repo,
            seq=seq,
            finished_signal=self.scan_finished,
        )
        self.state.tasks_by_source[source_id] = Task(
            priority=8,
            seq=seq,
            fn=executor,
            args=(),
            kwargs={},
            executor=ExecutorType.THREAD,
        )
        _logger.debug(
            "Scheduled scan task: source=%d seq=%d path=%s",
            source_id, seq, source.source_path,
        )

    def _submit_new_tasks(self) -> None:
        """把 ``state.tasks_by_source`` 中尚未提交的任务提交给 TaskService。

        幂等：以 ``task.seq`` 为提交凭据；同时清理已不存在的 seq，
        防止 ``submitted_seqs`` 无限增长。
        """
        for task in self.state.tasks_by_source.values():
            if task.seq in self.state.submitted_seqs:
                continue
            self._task_service.submit(
                task.fn,
                *task.args,
                executor=task.executor,
                priority=task.priority,
                **task.kwargs,
            )
            self.state.submitted_seqs.add(task.seq)
            _logger.debug(
                "Submitted scan task: seq=%d priority=%d",
                task.seq, task.priority,
            )

        alive = {t.seq for t in self.state.tasks_by_source.values()}
        pruned = self.state.prune_submitted(alive)
        if pruned:
            _logger.trace(
                "Pruned %d stale submitted seq(s); alive=%d",
                pruned, len(alive),
            )

    # ==================================================================
    # 内部：挂载
    # ==================================================================

    def _try_mount(self, source_id: int, source: IngestSource) -> bool:
        """按需为 source 执行自动挂载。

        * ``auto_mount=False``：不挂载，视为成功；
        * 挂载异常：记录日志并返回 ``False``。
        """
        if not source.auto_mount:
            return True
        try:
            self._auto_mount(source)
        except Exception as e:
            _logger.error(
                "Auto-mount failed for source %d: %s", source_id, e
            )
            return False
        _logger.debug(
            "Mounted source %d: %s -> %s",
            source_id, source.mount_point, source.source_path,
        )
        return True

    @staticmethod
    def _auto_mount(source: IngestSource) -> None:
        """执行自动挂载（``auto_mount=True`` 时前置字段已由 ``is_void`` 校验）。"""
        assert source.mount_point
        assert source.target_path
        auto_mount(
            mount_point=Path(source.mount_point),
            mount_target=source.target_path,
            source_path=Path(source.source_path),
            logger=_logger,
        )
