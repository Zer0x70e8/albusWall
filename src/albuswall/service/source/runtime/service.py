#
"""``SourceRuntime`` —— 扫描任务的常驻任务层。

职责边界（严格）
----------------
**持有**：

* ``tasks_by_source`` —— 每个被跟踪源当前的扫描任务（:class:`SourceExecutor`）；
* ``submitted_seqs``   —— 在途集合（提交成功 → 任务到终态时回收）；
* ``_settled_seqs``    —— 工作线程攒下的待回收 seq；
* ``_futures``         —— 在途 ``Future`` 句柄（仅用于释放 / 关联）。

**不做**：不读全量源列表、不判 ``is_void``、不挂载、不写 ``ingest_source``
—— 那些是 :class:`~albuswall.service.source.state.service.SourceStateService`
的职责。两端之间只有一份**计划**：

.. code-block:: python

    runtime.reconcile(added=..., rebuild=..., snapshot=..., removed_ids=...)

四个关键字的语义（顺序即执行顺序：removed → rebuild → added → snapshot）：

===============  =========================================================
参数             动作
===============  =========================================================
``added``        新建 executor（新 seq），加入任务表
``rebuild``      丢弃旧 executor，新建（新 seq）；旧 seq 由 prune 清
``snapshot``     **原地**更新 executor 持有的源快照（不换 seq、不重提）
``removed_ids``  从任务表删除 executor
===============  =========================================================

幂等派发（重要）
----------------
提交只看 ``_pending`` —— 本轮（或上轮因背压 / 关闭而没提交成功的）
``_create_task`` 集合。**已结算的任务不会因为一次无关的对账被重新激活**：
旧实现遍历整张任务表，导致"新建一个源，其余所有源都被重扫一遍"。
任务什么时候会重新派发，只有三种情况：

1. ``rebuild`` / ``added``（源配置变了或刚被纳入跟踪）；
2. ``rescan``（触发器到点，立刻换 seq 重扫）；
3. :meth:`force_scan`（运行时控制命令，强制扫一次）。

``submitted_seqs`` 因此纯粹是"在途"语义：任务到终态由 done 回调登记、
主线程 ``_drain_settled`` 回收，不再兼任"该不该重扫"的判断。

可观测与运行时控制
------------------
:meth:`status` 返回每源的 :class:`ScanTaskSnapshot`（调度态 + 最近一次
``scan_finished``），供命令层只读展示；:meth:`force_scan` /
:meth:`stop_scan` 是唯一的两个运行时控制入口。三者都**经主线程边界**
执行（任务表 / 在途集合只允许主线程改），因此可以从 IPC 线程直接调。

线程契约
--------
``_on_task_done`` 跑在 ``TaskService`` 的工作线程上，只写 ``_futures`` /
``_settled_seqs``（各自带锁）；真正的回收（``_drain_settled``）只在主线程的
``reconcile`` / ``rescan`` 末尾发生。这样"任务表 / 在途集合"这两个共享
结构永远只被主线程改动。

依赖
----
``ImportRepository``（执行体读写候选缓存）、``TaskService``（提交 / 背压 /
关闭），以及 ``IngestSourceRepository``（**仅**用于给 executor 注入
``exists`` 存活探针）。本模块不 import ``state`` / ``command`` 子包。
"""

from __future__ import annotations

import dataclasses
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass
from itertools import count as _count
from collections.abc import Callable
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Set,
    TypeVar,
)

from albuswall.common.exceptions import (
    BackpressureError,
    TaskServiceShutdownError,
)
from albuswall.dto.source.query import IngestSource
from albuswall.log import getLogger
from albuswall.repositories.import_ import ImportRepository
from albuswall.repositories.source import IngestSourceRepository
from albuswall.utils.signals import Signal

from .executor import SourceExecutor
from .scanning import SCAN_TASK_EXECUTOR, SCAN_TASK_PRIORITY

if TYPE_CHECKING:  # 仅类型检查期需要，避免运行期拉起 infrastructure
    from albuswall.dto.source.events import SourceScanFinished
    from albuswall.infrastructure.task import TaskService

    from ...main_loop_service import MainLoopService

__all__ = ["ScanTaskSnapshot", "SourceRuntime"]


@dataclass(frozen=True, slots=True)
class ScanTaskSnapshot:
    """单个源的扫描任务快照（只读视图，供命令层展示）。

    ``state`` 的含义（调度态，不是扫描结果）：

    ==========  ==========================================================
    ``pending``  已调度、还没成功提交给 TaskService（刚建 / 背压重试中）
    ``running``  已提交、尚未结算（在途）
    ``finished`` 已结算的一次性任务：**不会自动重跑**，等 ``rescan`` /
                 ``rebuild`` / ``force_scan`` 才会换 seq 重新派发
    ``stopped``  已被 :meth:`SourceRuntime.stop_scan` 撤掉（或从未调度），
                 只剩历史扫描结果
    ==========  ==========================================================

    ``last_scan`` 是最近一次 ``SourceScanFinished`` 的摊平字典（含
    ``error`` / ``skipped`` / 各种计数），还没扫过时为 ``None``。

    ``submitted`` 是**在途集合的原始成员关系**：任务到终态后它要等主线程
    下一次 `reconcile` 才回落，所以可能比 ``state`` 短暂滞后（``state``
    以 ``last_scan.task_seq == seq`` 为准，更贴近"扫完了没有"）。
    """

    source_id: int
    seq: int
    source_path: str
    state: str
    submitted: bool
    last_scan: Optional[Dict[str, Any]] = None
    last_scan_at: Optional[float] = None

_logger = getLogger(__name__)

T = TypeVar("T")

# 类级 seq 计数器：跨实例保证 seq 唯一（防止一次误重建把旧 seq 复用）。
_SEQ_COUNTER = _count()


class SourceRuntime:
    """扫描任务的常驻层：接收计划、管理执行体、向 TaskService 提交。"""

    def __init__(
            self,
            import_repo: ImportRepository,
            task_service: "TaskService",
            source_repo: IngestSourceRepository,
            *,
            main_loop: "MainLoopService | None" = None,
    ) -> None:
        self._import_repo = import_repo
        self._task_service = task_service
        # 只作为 executor 的存活探针来源；runtime 自己不查询源配置。
        self._source_repo = source_repo
        #: 主线程调度器。``None`` = 直接同步执行（单测 / 未装配主循环）。
        #: 任务表与在途集合是主线程独有的结构，public 入口靠它保证边界。
        self._main_loop = main_loop

        # 每源当前任务（SourceExecutor）。键是 source_id。
        self._tasks_by_source: Dict[int, SourceExecutor] = {}
        # 已调度、尚未成功提交的 source_id（幂等派发的唯一依据）。
        self._pending: Set[int] = set()
        # 在途 seq 集合（提交成功加入，到终态回收）。
        self._submitted_seqs: Set[int] = set()
        # 工作线程登记的待回收 seq（只在主线程消费）。
        self._settled_seqs: Set[int] = set()
        # 在途 future 句柄：仅用于关联 / 释放，不参与调度判断。
        self._futures: Dict[int, Future[Any]] = {}
        # 最近一次扫描结果（source_id → 摊平的 SourceScanFinished + 时间戳）。
        # 由工作线程在 scan_finished 回调里写，主线程读；用独立锁保护。
        self._last_scans: Dict[int, Dict[str, Any]] = {}

        self._futures_lock = threading.Lock()
        self._settled_lock = threading.Lock()
        self._last_scans_lock = threading.Lock()

        # 单 source 扫描完成信号（实例属性，实例独占）。
        # 契约：cb(sender: SourceExecutor, *, event: SourceScanFinished)
        self.scan_finished: Signal = Signal(name="SourceScanFinished")

        self._started = False

    # ------------------------------------------------------------------ 日志
    def describe(self, *, verbose: bool = False) -> str:
        base = (
            f"SourceRuntime(tasks={len(self._tasks_by_source)}, "
            f"submitted={sorted(self._submitted_seqs)})"
        )
        if verbose:
            details = ", ".join(
                f"{sid}->seq={e.seq}" for sid, e in self._tasks_by_source.items()
            )
            base = base[:-1] + f", map={{{details}}})"
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

    # ------------------------------------------------------------------ 线程边界
    def _on_main(self, fn: Callable[[], T]) -> T:
        """把 ``fn`` 送到主循环线程执行（已在该线程时直接跑）。

        任务表 / 在途集合 / ``_pending`` 只允许主线程改：``reconcile`` 与
        ``rescan`` 本来就由 state 层在主线程调用，而运行控制命令
        （``force_scan`` / ``stop_scan``）和观测（``status``）可能从 IPC
        线程进来 —— 这里统一收口。
        """
        if self._main_loop is None:
            return fn()
        return self._main_loop.call(fn)

    # ------------------------------------------------------------------ 视图
    @property
    def tasks(self) -> List[SourceExecutor]:
        """当前任务视图（快照列表，调用方改它不影响内部表）。"""
        return list(self._tasks_by_source.values())

    @property
    def task_seqs(self) -> Mapping[int, int]:
        """``{source_id: seq}`` 只读视图，便于测试与诊断。"""
        return {sid: e.seq for sid, e in self._tasks_by_source.items()}

    @property
    def submitted_seqs(self) -> Set[int]:
        """在途 seq 集合的副本。"""
        return set(self._submitted_seqs)

    def has_task(self, source_id: int) -> bool:
        return source_id in self._tasks_by_source

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        """初始化运行时结构。幂等。"""
        if self._started:
            _logger.debug("SourceRuntime already started; ignored")
            return
        self._tasks_by_source.clear()
        self._pending.clear()
        self._submitted_seqs.clear()
        with self._last_scans_lock:
            self._last_scans.clear()
        self._drain_settled()
        # 记录最近一次扫描结果，供 ``status()`` / 命令层展示。
        self.scan_finished.connect(self._on_scan_finished)
        self._started = True
        _logger.info("SourceRuntime started")

    def shutdown(self) -> None:
        """清空任务表 / 在途集合，并释放 future 句柄。

        * 幂等；
        * **不**取消在途任务、**不**通知 TaskService —— 任务归属 TaskService，
          关闭顺序由装配层保证（runtime 在 TaskService 之前拆）；
        * 关闭后 ``reconcile`` / ``rescan`` 变成 no-op。
        """
        if not self._started:
            return
        self._tasks_by_source.clear()
        self._pending.clear()
        self._submitted_seqs.clear()
        self.scan_finished.disconnect(self._on_scan_finished)
        with self._futures_lock:
            self._futures.clear()
        with self._settled_lock:
            self._settled_seqs.clear()
        with self._last_scans_lock:
            self._last_scans.clear()
        self._started = False
        _logger.debug("SourceRuntime shut down")

    @property
    def started(self) -> bool:
        return self._started

    # ------------------------------------------------------------------ 计划
    def reconcile(
            self,
            *,
            added: Optional[Mapping[int, IngestSource]] = None,
            rebuild: Optional[Mapping[int, IngestSource]] = None,
            snapshot: Optional[Mapping[int, IngestSource]] = None,
            removed_ids: Iterable[int] = (),
    ) -> None:
        """执行一次由 state 层算出的对账计划（线程边界：主线程）。"""
        self._on_main(lambda: self._reconcile(
            added=added, rebuild=rebuild,
            snapshot=snapshot, removed_ids=removed_ids,
        ))

    def _reconcile(
            self,
            *,
            added: Optional[Mapping[int, IngestSource]] = None,
            rebuild: Optional[Mapping[int, IngestSource]] = None,
            snapshot: Optional[Mapping[int, IngestSource]] = None,
            removed_ids: Iterable[int] = (),
    ) -> None:
        """执行一次由 state 层算出的对账计划。

        顺序固定为 ``removed → rebuild → added → snapshot``：
        先腾出位置（已删源的 seq 不再占位），再换新，最后刷新快照；
        末尾统一 ``_drain_settled`` + ``_submit_pending`` —— 一轮计划只提交
        **本轮新建 / 重建**的任务，已结算的任务不会被顺手重新激活
        （幂等派发，见模块 docstring）。
        """
        if not self._started:
            _logger.debug("reconcile() ignored: SourceRuntime is not started")
            return

        for source_id in removed_ids:
            self._drop_task(source_id)

        for source_id, source in (rebuild or {}).items():
            # rebuild = 扫描行为变了：旧 executor 连快照带 seq 一起作废。
            self._drop_task(source_id)
            self._create_task(source_id, source)

        for source_id, source in (added or {}).items():
            self._create_task(source_id, source)

        for source_id, source in (snapshot or {}).items():
            executor = self._tasks_by_source.get(source_id)
            if executor is None:
                # 计划与任务表不一致（state 认为在跟踪，runtime 却没有任务）：
                # 按 added 处理，宁可多扫一次也不要"看得到源却从不扫描"。
                _logger.debug(
                    "snapshot for untracked source %d; creating task", source_id
                )
                self._create_task(source_id, source)
                continue
            executor.update_source(source)

        self._drain_settled()
        self._submit_pending()

    def rescan(self, source_id: int) -> bool:
        """强制重扫单个源（线程边界：主线程）。"""
        return self._on_main(lambda: self._rescan(source_id))

    def _rescan(self, source_id: int) -> bool:
        """强制重扫单个源：换 seq + 立刻提交。

        供"触发器到点"这类外部唤醒使用（``trigger_refresh_signal``）。
        与 ``rebuild`` 的区别：``rebuild`` 由本轮计划末尾统一提交，
        ``rescan`` 自己提交 —— 触发器的语义就是"现在就扫"。

        Returns:
            ``True`` 表示已提交一个新 seq；``False`` 表示该源不在任务表里
            （未跟踪 / 已禁用 / 刚被删除），没有可重扫的任务。
        """
        executor = self._tasks_by_source.get(source_id)
        if executor is None:
            _logger.debug(
                "rescan(%d) ignored: no task for this source", source_id
            )
            return False

        source = executor.source
        self._drop_task(source_id)
        new_executor = self._create_task(source_id, source)

        self._drain_settled()
        self._submit_pending()
        return new_executor.seq in self._submitted_seqs

    # ------------------------------------------------------------------ 内部：任务表
    def _create_task(
            self, source_id: int, source: IngestSource
    ) -> SourceExecutor:
        """为一个源生成新的扫描任务（覆盖同源旧任务）。"""
        seq = next(_SEQ_COUNTER)
        executor = SourceExecutor(
            source=source,
            repo=self._import_repo,
            seq=seq,
            finished_signal=self.scan_finished,
            # 权威存活判断走 source 仓：任务真正执行时源可能已被删除
            # （回收站软删 / purge），executor 据此跳过而不是撞外键。
            is_source_active=self._source_repo.exists,
        )
        self._tasks_by_source[source_id] = executor
        # 新调度的任务默认待提交：``_submit_pending`` 只看这个集合，
        # 已结算的历史任务因此不会被无关的对账重新激活。
        self._pending.add(source_id)
        _logger.debug(
            "Scheduled scan task: source=%d seq=%d path=%s",
            source_id, seq, source.source_path,
        )
        return executor

    def _drop_task(self, source_id: int) -> None:
        """从任务表移除某源的任务（不取消在途任务，由 seq 自然过期）。"""
        self._pending.discard(source_id)
        executor = self._tasks_by_source.pop(source_id, None)
        if executor is not None:
            _logger.trace(
                "Dropped scan task: source=%d seq=%d", source_id, executor.seq
            )

    # ------------------------------------------------------------------ 内部：提交
    def _submit_pending(self) -> None:
        """提交 ``_pending`` 里的任务（幂等：已结算的任务不会被重新激活）。

        为什么只看 ``_pending``：它记录"已调度但还没成功提交"的 source ——
        ``_create_task`` 加入、提交成功 / 被 drop 时移除。旧实现遍历**整张
        任务表**，于是任何一次对账都会把已扫完的任务重新提交一遍（新建
        一个源 ⇒ 其余所有源重扫）。现在重扫只有三条入口：
        ``rebuild`` / ``added``、:meth:`rescan`、:meth:`force_scan`。

        ``submitted_seqs`` 因此纯粹是"在途"语义：提交成功后加入；任务到
        终态由 done 回调 (:meth:`_on_task_done`) 登记、主线程
        ``_drain_settled`` 回收。

        两条失败通道都必须就地接住，**不能**让异常冒到调用方
        （``create`` / ``update`` 都是 UI 写路径）：

        * ``BackpressureError``（队列满，拒绝式背压）：记 warning、**保留**
          在 ``_pending`` 里，下次对账或 ``source.scan`` 时重试；
        * ``TaskServiceShutdownError``（服务已关闭 / 正在关闭）：记 error
          并**提前 return** —— 此时后面每个任务都必然失败，继续尝试只会
          刷日志。

        成功提交时把 ``seq`` 作为 ``correlation_id`` 交给 TaskService：
        背压回调里能据此反查是哪个 source。
        """
        for source_id in tuple(self._pending):
            executor = self._tasks_by_source.get(source_id)
            if executor is None:
                # 任务已被 drop / removed：清掉孤儿 pending 记录。
                self._pending.discard(source_id)
                continue
            seq = executor.seq
            if seq in self._submitted_seqs:
                self._pending.discard(source_id)
                continue

            try:
                future = self._task_service.submit(
                    executor,
                    executor=SCAN_TASK_EXECUTOR,
                    priority=SCAN_TASK_PRIORITY,
                    correlation_id=seq,
                )
            except BackpressureError as exc:
                _logger.warning(
                    "Scan task rejected by backpressure, will retry on next "
                    "refresh: seq=%d (%s)", seq, exc,
                )
                continue
            except TaskServiceShutdownError as exc:
                _logger.error(
                    "TaskService is shut down, stop submitting scan tasks "
                    "for this round: seq=%d (%s)", seq, exc,
                )
                return

            self._pending.discard(source_id)
            self._submitted_seqs.add(seq)
            with self._futures_lock:
                self._futures[seq] = future
            future.add_done_callback(
                lambda _future, _seq=seq: self._on_task_done(_seq)
            )
            _logger.debug(
                "Submitted scan task: seq=%d priority=%d",
                seq, SCAN_TASK_PRIORITY,
            )

        alive = {e.seq for e in self._tasks_by_source.values()}
        pruned = self._prune_submitted(alive)
        if pruned:
            _logger.trace(
                "Pruned %d stale submitted seq(s); alive=%d",
                pruned, len(alive),
            )

    def _prune_submitted(self, alive_seqs: Set[int]) -> int:
        """丢掉已不在任务表里的旧 seq，返回清掉的条数。"""
        stale = self._submitted_seqs - alive_seqs
        if stale:
            self._submitted_seqs &= alive_seqs
        return len(stale)

    # ------------------------------------------------------------------ 内部：结算
    def _on_task_done(self, seq: int) -> None:
        """TaskService 的 done 回调（**工作线程**）：登记待回收的 seq。

        任务无论成功 / 失败 / 被背压 / 被 shutdown 丢弃，都会走到这里：
        不回收的话 ``submitted_seqs`` 只增不减，源扫完一次就永远提交不出去。

        **这里只做线程安全的最小操作**：摘掉 future 句柄、把 seq 塞进
        ``_settled_seqs``。真正的回收留给主线程的 :meth:`_drain_settled`。
        """
        with self._futures_lock:
            self._futures.pop(seq, None)
        with self._settled_lock:
            self._settled_seqs.add(seq)
        _logger.trace("Scan task settled, queued for release: seq=%d", seq)

    def _drain_settled(self) -> None:
        """在主线程回收 ``_on_task_done`` 攒下的 seq。

        只允许从主线程的写路径调用（``reconcile`` / ``rescan``）。
        """
        with self._settled_lock:
            pending, self._settled_seqs = self._settled_seqs, set()
        for seq in pending:
            self._submitted_seqs.discard(seq)

    def _on_scan_finished(
            self, _sender: object, *, event: "SourceScanFinished"
    ) -> None:
        """``scan_finished`` 槽（**工作线程**）：给 ``status()`` 留一份最近结果。

        只写 ``_last_scans``（带锁的最小操作），不碰任务表 / 在途集合 ——
        后两者只归主线程。扫描结果本身已经在事件里，这里只是"留档"。
        """
        try:
            record = dataclasses.asdict(event)
        except TypeError:  # pragma: no cover - 非 dataclass 的实现方
            record = {
                "source_id": event.source_id,
                "task_seq": getattr(event, "task_seq", None),
                "error": getattr(event, "error", None),
            }
        record["received_at"] = time.time()
        with self._last_scans_lock:
            self._last_scans[event.source_id] = record

    # ------------------------------------------------------------------ 运行时控制
    def force_scan(self, source: IngestSource) -> Optional[int]:
        """**强制**（重新）派发一次扫描，返回新 seq；runtime 未启动返回 ``None``。

        与 :meth:`rescan` 的区别：``rescan`` 只对"已经在任务表里"的源生效，
        而本方法不看任务表 —— 传进来的源快照会被直接建成新任务并立刻提交。
        用于"我就想现在扫一次"（即使该源当前未被跟踪 / 被禁用）。

        不改变 state 层的跟踪集：下次对账时，若 state 认为该源不该被跟踪，
        它会用 ``removed_ids`` 把任务撤掉。线程边界：主线程。
        """
        return self._on_main(lambda: self._force_scan(source))

    def _force_scan(self, source: IngestSource) -> Optional[int]:
        if not self._started:
            _logger.debug(
                "force_scan(%d) ignored: SourceRuntime is not started",
                source.id,
            )
            return None

        self._drop_task(source.id)
        executor = self._create_task(source.id, source)
        self._drain_settled()
        self._submit_pending()
        _logger.debug(
            "Forced scan: source=%d seq=%d submitted=%s",
            source.id, executor.seq, executor.seq in self._submitted_seqs,
        )
        return executor.seq if executor.seq in self._submitted_seqs else None

    def stop_scan(self, source_id: int) -> bool:
        """**停止**该源的扫描任务，返回是否真的撤掉了什么东西。

        * 从任务表移除 executor（之后 ``rescan`` 对该源变成 no-op）；
        * 丢弃 ``_pending`` 记录（不会在下一轮被自动补交）；
        * 取消**尚未开始**的在途任务（``Future.cancel`` 只能拦住未启动的；
          已经跑起来的扫描不会被中断，其结果照常写候选缓存并 emit）。

        想恢复扫描：``source.scan``（强制）或让状态层重新纳入跟踪。
        线程边界：主线程。
        """
        return self._on_main(lambda: self._stop_scan(source_id))

    def _stop_scan(self, source_id: int) -> bool:
        executor = self._tasks_by_source.get(source_id)
        seq = executor.seq if executor is not None else None
        self._drop_task(source_id)

        if seq is None:
            _logger.debug("stop_scan(%d): no task to stop", source_id)
            return False

        with self._futures_lock:
            future = self._futures.pop(seq, None)
        cancelled = False
        if future is not None:
            cancelled = future.cancel()
        with self._settled_lock:
            self._settled_seqs.discard(seq)
        self._submitted_seqs.discard(seq)

        _logger.debug(
            "Stopped scan task: source=%d seq=%d in_flight_cancelled=%s",
            source_id, seq, cancelled,
        )
        return True

    # ------------------------------------------------------------------ 可观测
    def status(self, source_id: Optional[int] = None) -> List[ScanTaskSnapshot]:
        """每源扫描任务的快照（``source_id`` 省略 = 全部）。线程边界：主线程。

        覆盖三类源：任务表里的（pending / running / finished）、只剩历史
        扫描结果的（stopped）。返回按 ``source_id`` 升序，顺序稳定便于 diff。
        """
        return self._on_main(lambda: self._status(source_id))

    def _status(self, source_id: Optional[int] = None) -> List[ScanTaskSnapshot]:
        with self._last_scans_lock:
            last_scans = dict(self._last_scans)

        source_ids = set(self._tasks_by_source) | set(last_scans)
        if source_id is not None:
            source_ids = {sid for sid in source_ids if sid == source_id}

        snapshots: List[ScanTaskSnapshot] = []
        for sid in sorted(source_ids):
            executor = self._tasks_by_source.get(sid)
            record = last_scans.get(sid)
            if executor is None:
                snapshot = ScanTaskSnapshot(
                    source_id=sid,
                    seq=int((record or {}).get("task_seq") or 0),
                    source_path="",
                    state="stopped",
                    submitted=False,
                    last_scan=record,
                    last_scan_at=(record or {}).get("received_at"),
                )
            else:
                submitted = executor.seq in self._submitted_seqs
                # "已出结果"比"是否已从在途集合里回收"更权威：任务到终态后
                # done 回调把 seq 登记进 _settled_seqs，真正回收要等主线程的
                # 下一次对账 —— 这段时间里 submitted_seqs 会短暂滞后，不该让
                # 用户看到"扫描完了还显示 running"。
                reported = (
                    record is not None
                    and record.get("task_seq") == executor.seq
                )
                if sid in self._pending:
                    state = "pending"
                elif submitted and not reported:
                    state = "running"
                else:
                    state = "finished"
                snapshot = ScanTaskSnapshot(
                    source_id=sid,
                    seq=executor.seq,
                    source_path=executor.source.source_path,
                    state=state,
                    submitted=submitted,
                    last_scan=record,
                    last_scan_at=(record or {}).get("received_at"),
                )
            snapshots.append(snapshot)
        return snapshots
