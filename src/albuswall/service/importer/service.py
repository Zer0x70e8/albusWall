#
"""``ImportService`` —— ``asset_candidate_cache`` → ``assets`` 的导入闭环。

生命周期
--------
* ``start()``       容器启动时调用，起常驻 worker 并立刻 trigger 一轮
  （避免错过启动前就存在的 pending）；
* ``trigger()``     外部唤醒一轮（当前唯一触发方是装配层接的
  ``SourceRuntime.scan_finished``，以及运维用的 ``import.trigger`` 命令）；
* ``stop_worker()`` 容器关闭时调用。

单轮口径：``process_pending()`` 幂等（``_run_lock`` 防重入），一轮可能连跑
多个 batch，直到 pending 取空。

线程模型
--------
* **worker 线程**（``albuswall-import-worker``）自己按轮询驱动，不占用主循环；
* 每个候选文件投给 ``TaskService`` 的 **process 执行器**
  （:func:`~albuswall.service.importer.processing.process_candidate`），
  主线程只做 ``wait`` + 回收 + 落库；
* 落库（``finalize_candidate`` / ``mark_candidate_failed``）发生在 worker
  线程上，靠仓储的线程本地连接 + 写锁保证安全（与全部后台服务一致）。

职责边界（刻意不做的事）
-----------------------
* **不**在导入完成后自动扫缩略图。缩略图改成**按需生成**：谁访问到资产谁
  发起请求（装配层把 ``ViewService.assets_viewed`` 接到
  ``ThumbnailService.request_bulk``）。原因：全量扫描会让"打开应用就狂跑
  缩略图任务"，磁盘和 CPU 长时间被占满，而用户可能只看头几页。
* **不**决定源文件该不该导入（那是 Stage 1 ``SourceExecutor`` 的 glob
  粗筛 + Stage 2 精判），本服务只负责把候选变成资产行。
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import (
    FIRST_COMPLETED,
    wait,
    TimeoutError as FutureTimeoutError,
)
from typing import TYPE_CHECKING, Dict, Optional

from albuswall.common.enums import CandidateStatus, FileTypeCheckMode
from albuswall.dto.import_ import AssetCreate
from albuswall.dto.source.query import IngestSourceSnapshot
from albuswall.log import Logger, getLogger
from albuswall.utils.signals import SignalDescriptor

from .config import import_config
from .processing import ProcessResult, process_candidate

if TYPE_CHECKING:  # 只为类型标注；运行时鸭子类型，避免装配顺序依赖
    from albuswall.infrastructure.task import TaskService
    from albuswall.repositories.import_ import ImportRepository
    from albuswall.repositories.source import IngestSourceRepository

__all__ = ["ImportService"]

_logger: Logger = getLogger(__name__)

#: 进程池任务的优先级：低于"用户按了按钮"的扫描（0），高于对账（负值）。
IMPORT_PRIORITY = 5


class ImportService:
    """候选缓存 → 资产的导入 worker（被动：只由 ``trigger()`` 驱动）。"""

    # 类属性 → 必须用 SignalDescriptor（lazy、实例间共享）。
    # 契约：cb(sender: ImportService, *, ok, failed, skipped, retry, timeout)
    assets_imported = SignalDescriptor("assets_imported")

    def __init__(
            self,
            repo: "ImportRepository",
            task: "TaskService",
            source_repo: "Optional[IngestSourceRepository]" = None,
    ) -> None:
        self._repo = repo
        self._task = task
        # 可选注入：每轮构建 {source_id: IngestSource} 内存快照，把
        # file_type_check 带给进程池里的精判。未注入时全部降级为 SUFFIX。
        self._source_repo = source_repo

        self._wake = threading.Event()
        self._stop = threading.Event()
        self._run_lock = threading.Lock()
        self._worker: Optional[threading.Thread] = None

    # ================================================================
    # 自省 / 生命周期
    # ================================================================
    def describe(self, *, verbose: bool = False) -> str:
        worker = self._worker
        alive = bool(worker and worker.is_alive())
        base = f"ImportService(running={alive})"
        if verbose:
            base = base[:-1] + (
                f", repo={type(self._repo).__name__}, "
                f"task={type(self._task).__name__}, "
                f"source_repo={type(self._source_repo).__name__})"
            )
        return base

    def start(self) -> None:
        """容器启动时调用一次（幂等）。"""
        self.start_worker()

    def start_worker(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._stop.clear()
        self._worker = threading.Thread(
            target=self._worker_loop,
            name="albuswall-import-worker",
            daemon=True,
        )
        self._worker.start()
        # 启动即扫一遍，避免错过启动前已存在的 pending。
        self.trigger()

    def stop_worker(self, timeout: float = 10.0) -> None:
        self._stop.set()
        self._wake.set()
        worker = self._worker
        if worker and worker.is_alive():
            worker.join(timeout=timeout)

    def trigger(self) -> None:
        """外部模块触发一轮导入（幂等，可重复调用）。"""
        self._wake.set()

    # ================================================================
    # worker 主循环
    # ================================================================
    def _worker_loop(self) -> None:
        # 退避参数在进入循环时读一次；运行期要热更新就改成每轮读。
        conf = import_config()
        initial_backoff = conf["initial_backoff"]
        max_backoff = conf["max_backoff"]
        backoff = initial_backoff

        while not self._stop.is_set():
            self._wake.wait()
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                self.process_pending()
            except Exception:  # noqa: BLE001
                # 持续性故障（DB 锁、磁盘满、source_paths 反序列化异常等）下，
                # 立即自唤会空转刷屏；改为指数退避，退避等待可被 stop 打断。
                _logger.exception(
                    "Import worker raised an exception; retrying in %.1fs",
                    backoff,
                )
                # _stop.wait 返回 True 表示 stop_worker 被调用，直接退出。
                if self._stop.wait(backoff):
                    break
                # 退避结束，自我唤醒排下一轮。
                # 若退避期间外部已 trigger()，_wake 已置位，下轮 wait 立刻返回。
                self._wake.set()
                backoff = min(backoff * 2, max_backoff)
            else:
                # 一轮正常结束即复位退避，保持对正常 trigger 的低延迟响应。
                backoff = initial_backoff

    # ================================================================
    # 单轮处理
    # ================================================================
    def process_pending(self) -> None:
        """执行一轮 pending 处理。带重入保护：已在执行则立即返回。"""
        if not self._run_lock.acquire(blocking=False):
            _logger.debug(
                "An import task is already running; skipping this "
                "process_pending round"
            )
            return
        try:
            self._run_once()
        finally:
            self._run_lock.release()

    def _run_once(self) -> None:
        # 启动前回收崩溃残留（processing 超时后没有主人认领的候选）。
        recovered = self._repo.recover_stale_candidates()
        if recovered:
            _logger.debug("Recovered %d stale candidate(s)", recovered)

        # 预取 source_id → source_path，供子进程解析绝对路径。
        source_paths: Dict[int, str] = self._repo.get_source_paths()
        # 每轮一份内存快照：把 file_type_check 随候选一起交给进程池，
        # 避免子进程查 DB，也不给候选表加 type_check 冗余列。
        source_map = self._load_source_map()

        batch_size = import_config()["batch_size"]

        totals = {"ok": 0, "failed": 0, "skipped": 0, "retry": 0, "timeout": 0}

        while not self._stop.is_set():
            candidates = self._repo.get_pending_candidates(limit=batch_size)
            if not candidates:
                break

            _logger.info(
                "Processing batch of %d pending candidates", len(candidates))
            self._process_batch(candidates, source_paths, source_map, totals)

            # 不足一批说明已到底。
            if len(candidates) < batch_size:
                break

        _logger.info(
            "Import round finished. ok=%d failed=%d skipped=%d retry=%d timeout=%d",
            totals["ok"], totals["failed"], totals["skipped"],
            totals["retry"], totals["timeout"],
        )
        if totals["ok"] > 0:
            try:
                # 契约：cb(sender, *, ok, failed, skipped, retry, timeout)
                self.assets_imported.emit(self, **totals)
            except Exception:  # noqa: BLE001 —— 订阅方异常不该打断 worker
                _logger.exception("emit assets_imported failed")

    # ================================================================
    # 批处理
    # ================================================================
    def _process_batch(
            self,
            candidates,
            source_paths: Dict[int, str],
            source_map: Dict[int, IngestSourceSnapshot],
            totals: Dict[str, int],
    ) -> None:
        futures = {}
        for cand in candidates:
            source_path = source_paths.get(cand.source_id, "")
            # manual source（id=0）不在 get_source_candidates 里，快照缺失时
            # 一律降级为 SUFFIX（只信后缀）。
            source = source_map.get(cand.source_id)
            type_check = (
                source.file_type_check
                if source is not None
                else FileTypeCheckMode.SUFFIX
            )
            try:
                fut = self._task.submit(
                    process_candidate,
                    cand,
                    source_path,
                    type_check,
                    executor="process",
                    priority=IMPORT_PRIORITY,
                )
                futures[fut] = cand
            except Exception:  # noqa: BLE001
                _logger.exception("Failed to submit candidate %s", cand.uuid)
                self._safe_mark_failed(cand.id, "submit_failed")
                totals["failed"] += 1

        if not futures:
            return

        timeout = import_config()["process_timeout"]
        now = time.monotonic()
        # 每个 future 独立的 deadline；从提交完成时刻开始计时。
        deadlines = {fut: now + timeout for fut in futures}
        pending = set(futures.keys())

        while pending:
            now = time.monotonic()
            next_deadline = min(deadlines[fut] for fut in pending)
            wait_timeout = max(0.0, next_deadline - now)

            try:
                done, _ = wait(
                    pending,
                    timeout=wait_timeout,
                    return_when=FIRST_COMPLETED,
                )
            except Exception:  # noqa: BLE001
                _logger.exception(
                    "Exception while waiting for futures to complete; "
                    "cancelling the remaining tasks"
                )
                for fut in pending:
                    fut.cancel()
                break

            for fut in done:
                pending.discard(fut)
                self._handle_result(futures[fut], fut, totals)

            # 处理到期的任务（可能 wait_timeout=0 时立即进来）
            now = time.monotonic()
            expired = [fut for fut in pending if now >= deadlines[fut]]
            for fut in expired:
                pending.discard(fut)
                cand = futures[fut]
                _logger.error(
                    "Candidate %s timed out (>%.0fs)", cand.uuid, timeout)
                fut.cancel()  # 尽力而为；进程池任务未必能真正中断
                self._safe_mark_failed(cand.id, "timeout")
                totals["timeout"] += 1

    def _handle_result(self, cand, fut, totals: Dict[str, int]) -> None:
        """回收单个候选的三态结果。

        分派规则（与 :class:`ProcessResult` 一一对应）：

        * ``skip`` → ``SKIPPED``（不是失败，计数进 skipped，reason 只写日志）；
        * ``fail`` → ``FAILED``（走 ``_safe_mark_failed``，依赖状态机放行
          ``PENDING → FAILED`` —— 当前链路不 claim，候选就是 PENDING）；
        * ``ok``   → ``finalize_candidate``；返回 ``None`` 时再由候选终态区分
          ``SKIPPED``（唯一约束冲突 / 重复导入）与 ``FAILED``（真失败）。
        """
        try:
            result = fut.result()  # 已经 done 或 cancelled，不会阻塞
        except FutureTimeoutError:
            # 保留兜底：wait 循环之外的路径（未来若有别的调用方）也会走这里。
            _logger.error("Candidate %s timed out", cand.uuid)
            fut.cancel()
            self._safe_mark_failed(cand.id, "timeout")
            totals["timeout"] += 1
            return
        except Exception:  # noqa: BLE001
            _logger.exception(
                "Candidate %s raised an exception (worker_exception)", cand.uuid
            )
            self._safe_mark_failed(cand.id, "worker_exception")
            totals["failed"] += 1
            return

        if not isinstance(result, ProcessResult):
            # 旧契约（Optional[AssetCreate]）兜底：不应发生，但语义上
            # None 现在明确属于失败而不是"跳过"。
            _logger.error(
                "Candidate %s returned a non-ProcessResult value: %r",
                cand.uuid,
                result,
            )
            self._safe_mark_failed(cand.id, "bad_result")
            totals["failed"] += 1
            return

        if result.status == "skip":
            self._safe_mark_skipped(cand.id, result.reason or "unknown")
            totals["skipped"] += 1
            return

        if result.status == "fail":
            self._safe_mark_failed(cand.id, result.reason or "process_error")
            totals["failed"] += 1
            return

        # status == "ok"
        if result.dto is None:
            # 契约要求 ok 必带 dto；防御性降级为失败，避免 finalize 里抛 TypeError。
            _logger.error("Candidate %s returned ok but without a dto", cand.uuid)
            self._safe_mark_failed(cand.id, "no_payload")
            totals["failed"] += 1
            return

        self._finalize(cand, result.dto, totals)

    def _finalize(
            self, cand, asset_dto: AssetCreate, totals: Dict[str, int],
    ) -> None:
        try:
            asset_uuid = self._repo.finalize_candidate(cand.id, asset_dto)
        except Exception:  # noqa: BLE001
            # 临时 DB 错误：不动候选状态，下一轮重新取到后重试。
            _logger.exception(
                "DB error while persisting candidate %s; keeping it pending "
                "for retry",
                cand.uuid,
            )
            totals["retry"] += 1
            return

        if asset_uuid:
            totals["ok"] += 1
            _logger.debug(
                "Candidate %s -> asset_uuid=%s", cand.uuid, asset_uuid)
            return

        # repo 判定为永久失败（唯一约束冲突 → skipped，其余 → failed）。
        # get_candidate_status 返回 CandidateStatus 枚举，必须与枚举比较，
        # 不能拿字符串字面量比较（否则永远走 failed 分支）。
        status = self._repo.get_candidate_status(cand.id)
        if status is CandidateStatus.SKIPPED:
            totals["skipped"] += 1
            _logger.info("Candidate %s is a duplicate asset; skipped", cand.uuid)
        else:
            totals["failed"] += 1
            _logger.error(
                "Failed to insert asset: candidate %s status=%s", cand.uuid, status
            )

    def _safe_mark_failed(self, candidate_id, reason: str) -> None:
        """把候选置为 FAILED；失败只记日志，不再向上抛。

        走 repo 的 ``mark_candidate_failed``（状态机已放行 ``PENDING → FAILED``
        与 ``PROCESSING → FAILED``；当前链路不 claim，候选是 PENDING，缺这条
        转移会让失败候选永远停在 pending、被下一轮反复捞出重试）。

        ``reason`` 只落日志——本任务不新增 DB 列。
        """
        try:
            self._repo.mark_candidate_failed(candidate_id)
            _logger.warning(
                "candidate %s failed (status=failed, reason=%s)",
                candidate_id, reason,
            )
        except Exception:  # noqa: BLE001
            _logger.exception(
                "mark failed failed as well; candidate left in its current "
                "state (recover_stale_candidates reclaims it after the "
                "timeout) cand=%s reason=%s",
                candidate_id, reason,
            )

    def _safe_mark_skipped(self, candidate_id, reason: str) -> None:
        """把候选置为 SKIPPED；失败时降级到 FAILED 并记日志。

        走 repo 的 ``update_candidate_status``（状态机已放行
        ``PENDING → SKIPPED`` 与 ``PROCESSING → SKIPPED``）。

        ``reason`` **只落日志**——候选表没有 skip_reason 列，本任务也不加。
        UI 侧统一用 ``status=SKIPPED`` 识别"跳过"，与"重复导入"
        （finalize 内部置 SKIPPED）在日志层面用 reason 区分。
        """
        try:
            self._repo.update_candidate_status(
                candidate_id, CandidateStatus.SKIPPED)
            _logger.info(
                "candidate %s skipped (status=skipped, reason=%s)",
                candidate_id, reason,
            )
        except Exception:  # noqa: BLE001
            _logger.exception(
                "Failed to mark candidate %s as skipped; falling back to failed",
                candidate_id,
            )
            try:
                self._repo.mark_candidate_failed(candidate_id)
            except Exception:  # noqa: BLE001
                _logger.exception(
                    "Fallback mark failed failed too cand=%s", candidate_id
                )

    def _load_source_map(self) -> Dict[int, IngestSourceSnapshot]:
        """构建 ``{source_id: IngestSourceSnapshot}`` 只读配置快照（每轮一次）。

        为什么不在候选表里冗余 type_check 列：``candidate.source_id`` 已能
        反查到源配置，内存快照足够；进程池里不查 DB，所以 type_check 只能在
        **提交任务时**从快照取出，作为参数传给 ``process_candidate``。

        取不到（未注入 source_repo / DB 抖动）时返回空 dict，调用侧 fallback
        到 ``SUFFIX``，不影响导入主流程。
        """
        if self._source_repo is None:
            return {}
        try:
            return {s.id: s for s in self._source_repo.get_source_candidates()}
        except Exception:  # noqa: BLE001
            _logger.exception(
                "Failed to load the source snapshot; all type_check "
                "results fall back to suffix for this round",
            )
            return {}
