#
"""统一任务服务的具体实现。

该模块提供：
    - 优先级队列 + 背压
    - 线程 / 进程混合调度
    - 基于 psutil 的动态全局并发调整
    - 背压可观测性（计数 + 回调）

公共契约定义在 :mod:`task_service.protocol`；
本模块是唯一官方实现，包含所有内部管理接口。

只依赖契约的调用方，应当面向 ``TaskServiceProtocol`` 编程，
而不是直接 import 本模块。
"""

from __future__ import annotations

import inspect
import itertools
import multiprocessing
import pickle
import queue
import threading
import time
from concurrent.futures import (
    ThreadPoolExecutor,
    ProcessPoolExecutor,
    Future,
    InvalidStateError,
)
from traceback import format_exc
from typing import Callable, Optional

import psutil

from albuswall.log import getLogger, Logger
from albuswall.dto.task import (
    BackpressureEvent,
    BackpressureReason,
    ExecutorType,
    PermitKind,
    TaskServiceStats,
)
from albuswall.common.exceptions import BackpressureError

from ._semaphore import DynamicSemaphore
from ._task import _Task

_logger: Logger = getLogger(__package__)


# noinspection PyShadowingNames,SpellCheckingInspection
class TaskService:
    """
    Unified task service: 所有任务进入同一个优先队列，受全局并发限制约束。
    支持线程 / 进程执行，通过 psutil 动态调整全局并发上限。

    动态调整策略:
      - 高负载（CPU/mem 超阈值）: 立即快速下调（减半），有下限保护
      - 低负载（CPU/mem 均低于低阈值）: 需连续 N 次确认，才 +1，有上限保护
      - 其他: 保持不变

    背压:
      - 队列满（queue_full）: submit / put_back 时抛出或设置 BackpressureError
      - 阻塞式背压（permit_wait）: 等待 permit 超阈值时记录

    进程任务约束:
      - ``executor=ExecutorType.PROCESS`` 的任务提交到 ProcessPoolExecutor(spawn)
      - fn、args、kwargs 必须可 pickle
      - 明确拒绝: lambda、局部函数、绑定方法

    put_back 语义:
      - 调度线程拿到任务后，若因 shutdown 或重新调度需放回队列
      - 若队列已满，任务会被丢弃，但对应 future 会被设置 BackpressureError
      - 调用方应通过 future.result() / future.exception() 感知

    关闭 / 提交竞态:
      - ``submit`` 的「关闭检查 + 入队」是原子操作（受 ``_submit_lock`` 保护）
      - ``shutdown`` 里的 ``_drain_queue`` 也持同一把锁
      - 因此不存在「submit 通过了 is_set 检查、drain 已经跑完、任务才入队」的
        永久悬空场景
    """

    def __init__(self,
                 initial_max_concurrency: int = 10,
                 min_concurrency: int = 1,
                 max_concurrency: Optional[int] = None,
                 process_workers: Optional[int] = None,
                 cpu_high_threshold: float = 80.0,
                 cpu_low_threshold: float = 30.0,
                 mem_high_threshold: float = 85.0,
                 mem_low_threshold: float = 50.0,
                 check_interval: float = 5.0,
                 scheduler_threads: int = 2,
                 max_queue_size: int = 1000,
                 submit_timeout: float = 5.0,
                 low_load_streak: int = 3,
                 wait_warn_threshold: float = 1.0):
        if max_concurrency is None:
            max_concurrency = max(initial_max_concurrency * 4,
                                  initial_max_concurrency + 1)
        max_concurrency = max(max_concurrency, min_concurrency)
        initial_max_concurrency = max(min_concurrency,
                                      min(initial_max_concurrency, max_concurrency))

        self._min_concurrency = min_concurrency
        self._max_concurrency = max_concurrency
        self._check_interval = check_interval
        self._cpu_high = cpu_high_threshold
        self._cpu_low = cpu_low_threshold
        self._mem_high = mem_high_threshold
        self._mem_low = mem_low_threshold
        self._low_load_streak = max(1, low_load_streak)
        self._submit_timeout = submit_timeout
        self._wait_warn_threshold = wait_warn_threshold

        _logger.info(
            "Initializing TaskService, initial concurrency=%d [min=%d, max=%d], "
            "process pool size=%s, scheduler threads=%d, queue maxsize=%d",
            initial_max_concurrency, min_concurrency, max_concurrency,
            process_workers if process_workers else 'auto',
            scheduler_threads, max_queue_size)

        # 全局动态信号量
        self._global_sem = DynamicSemaphore(
            initial=initial_max_concurrency,
            min_value=min_concurrency,
            max_value=max_concurrency,
        )

        # 进程池信号量
        if process_workers is not None:
            self._process_workers = max(1, process_workers)
        else:
            self._process_workers = max(1, psutil.cpu_count(logical=True) or 1)
        self._process_sem = threading.BoundedSemaphore(self._process_workers)
        _logger.info("Process pool semaphore initialized to %d", self._process_workers)

        # 底层执行器
        self._thread_executor = ThreadPoolExecutor(max_workers=max_concurrency)
        self._process_executor = ProcessPoolExecutor(
            max_workers=self._process_workers,
            mp_context=multiprocessing.get_context("spawn"),
        )
        _logger.trace("Thread pool max_workers=%d; process pool max_workers=%d",
                      max_concurrency, self._process_workers)

        # 有界优先队列
        self._queue: queue.PriorityQueue = queue.PriorityQueue(maxsize=max_queue_size)
        self._seq_counter = itertools.count()

        # 关闭标志
        self._shutdown = threading.Event()

        # 「提交 / 关闭」互斥锁：
        #   - submit 侧：保护 is_set() 检查 + queue.put 的原子性
        #   - shutdown 侧：_drain_queue 持锁，确保 drain 之后不会再有新任务入队
        #   - _put_back 也纳入此锁，并做二次 is_set 检查
        # 注意：_record_backpressure 会调用用户回调，绝不能在持锁状态下调用。
        self._submit_lock = threading.Lock()

        # 背压可观测性
        self._stats_lock = threading.Lock()
        self._stats: TaskServiceStats = {
            "submitted": 0,
            "rejected": 0,
            "backpressure_reject": 0,
            "backpressure_wait": 0,
            "last_backpressure_ts": 0.0,
        }
        self._on_backpressure: Optional[Callable[[BackpressureEvent], None]] = None
        self._bp_log_throttle = 1.0
        self._last_wait_log_ts = 0.0

        # 调度线程
        self._scheduler_threads = []
        for i in range(scheduler_threads):
            t = threading.Thread(target=self._scheduler_loop,
                                 name=f"TaskScheduler-{i}", daemon=True)
            t.start()
            self._scheduler_threads.append(t)
        _logger.info("Started %d scheduler threads", scheduler_threads)

        # 资源监控线程
        self._monitor_thread = threading.Thread(target=self._monitor_resources,
                                                name="TaskResourceMonitor", daemon=True)
        self._monitor_thread.start()
        _logger.info("Started resource monitor thread")

    # ============================================================
    # 公共契约（TaskServiceProtocol）
    # ============================================================
    def submit(self, fn: Callable, *args,
               executor: ExecutorType = ExecutorType.THREAD,
               priority: int = 0,
               **kwargs) -> Future:
        # 归一化 + 运行时白名单：非法值（含大小写错误）直接 ValueError。
        # 允许调用方继续传字符串 "thread" / "process"，因为是 str 子类。
        executor = ExecutorType(executor)

        if executor == ExecutorType.PROCESS:
            self._validate_process_task(fn, args, kwargs)

        future: Future = Future()
        seq = next(self._seq_counter)
        task = _Task(
            priority=priority,
            seq=seq,
            fn=fn,
            args=args,
            kwargs=kwargs,
            executor=executor,
            future=future,
        )

        put_failed = False
        # 原子区：关闭检查 + 计数 + 入队。
        # 与 shutdown 的 _drain_queue 互斥，杜绝「drain 之后才入队」的悬空任务。
        with self._submit_lock:
            if self._shutdown.is_set():
                _logger.error(
                    "Attempt to submit task after service shutdown, function=%s",
                    getattr(fn, "__name__", type(fn).__name__),
                )
                raise RuntimeError("TaskService is shut down, cannot submit new tasks")

            with self._stats_lock:
                self._stats["submitted"] += 1

            try:
                self._queue.put(task, timeout=self._submit_timeout)
            except queue.Full:
                put_failed = True

        # 锁外记录背压 / 抛错：_record_backpressure 会调用用户回调，
        # 回调内部如果重入 submit 会死锁。
        if put_failed:
            with self._stats_lock:
                self._stats["rejected"] += 1
            self._record_backpressure(
                BackpressureReason.QUEUE_FULL, seq=seq, priority=priority,
            )
            raise BackpressureError(
                reason=BackpressureReason.QUEUE_FULL,
                queue_size=self._queue.qsize(),
                max_queue_size=self._queue.maxsize,
            )

        name = getattr(fn, "__name__", type(fn).__name__)
        _logger.trace("Task enqueued: seq=%d, priority=%d, executor=%s, function=%s",
                      seq, priority, executor.value, name)
        return future

    def shutdown(self, wait: bool = True):
        if self._shutdown.is_set():
            return
        _logger.info("Starting TaskService shutdown, wait=%s", wait)
        self._shutdown.set()

        for t in self._scheduler_threads:
            t.join(timeout=3)
            if t.is_alive():
                _logger.warning("Scheduler thread %s did not exit cleanly", t.name)
        _logger.trace("Scheduler threads have all exited")

        self._drain_queue()

        self._monitor_thread.join(timeout=3)
        if self._monitor_thread.is_alive():
            _logger.warning("Resource monitor thread did not exit cleanly")
        else:
            _logger.trace("Resource monitor thread joined cleanly")

        _logger.trace("Closing thread pool and process pool...")
        self._thread_executor.shutdown(wait=wait)
        self._process_executor.shutdown(wait=wait)
        _logger.info("TaskService shutdown complete")

    def get_stats(self) -> TaskServiceStats:
        with self._stats_lock:
            return dict(self._stats)  # type: ignore[return-value]

    def set_backpressure_callback(
            self, cb: Optional[Callable[[BackpressureEvent], None]]
    ):
        self._on_backpressure = cb

    # ============================================================
    # 只读状态（具体实现额外提供的观测接口）
    # ============================================================
    @property
    def queue_size(self) -> int:
        return self._queue.qsize()

    @property
    def global_concurrency_max(self) -> int:
        return self._global_sem.max

    @property
    def global_concurrency_available(self) -> int:
        return self._global_sem.available

    @property
    def global_concurrency_current(self) -> int:
        return self._global_sem.current

    # ============================================================
    # 内部：背压记录
    # ============================================================
    def _record_backpressure(self, reason: BackpressureReason, **extra):
        qsize = self._queue.qsize()
        qmax = self._queue.maxsize
        now = time.time()

        with self._stats_lock:
            if reason in (BackpressureReason.QUEUE_FULL,
                          BackpressureReason.PUT_BACK_FAILED):
                self._stats["backpressure_reject"] += 1
            else:
                self._stats["backpressure_wait"] += 1
            self._stats["last_backpressure_ts"] = now

        if reason == BackpressureReason.PERMIT_WAIT:
            if now - self._last_wait_log_ts < self._bp_log_throttle:
                return
            self._last_wait_log_ts = now

        _logger.warning(
            "BACKPRESSURE reason=%s queue=%d/%d concurrency=%d/%d extra=%s",
            reason.value, qsize, qmax,
            self._global_sem.current, self._global_sem.max,
            extra,
        )

        cb = self._on_backpressure
        if cb is not None:
            try:
                # noinspection calling-non-callable
                cb({
                    "reason": reason,
                    "queue_size": qsize,
                    "queue_max": qmax,
                    "concurrency_current": self._global_sem.current,
                    "concurrency_max": self._global_sem.max,
                    "ts": now,
                    **extra,
                })
            except Exception as exc:
                _logger.error("Backpressure callback raised: %s", exc)

    # ============================================================
    # 内部：process 任务校验
    # ============================================================
    @staticmethod
    def _validate_process_task(fn: Callable, args: tuple, kwargs: dict) -> None:
        if not callable(fn):
            raise TypeError("process task fn must be callable")

        if inspect.ismethod(fn):
            raise TypeError(
                "process executor does not support bound methods; "
                "wrap it as a top-level function"
            )

        if inspect.isfunction(fn):
            if fn.__name__ == "<lambda>":
                raise TypeError("process executor does not support lambda")
            if "<locals>" in fn.__qualname__:
                raise TypeError("process executor does not support local functions")

        try:
            pickle.dumps((fn, args, kwargs), protocol=pickle.HIGHEST_PROTOCOL)
        except Exception as exc:
            raise TypeError(f"process task is not picklable: {exc}") from exc

    # ============================================================
    # 内部：调度循环
    # ============================================================
    def _scheduler_loop(self):
        _logger.trace("Scheduler thread started: %s", threading.current_thread().name)

        while not self._shutdown.is_set():
            try:
                task: _Task = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue

            seq = task.seq
            is_process = (task.executor == ExecutorType.PROCESS)

            if is_process:
                if not self._acquire_with_shutdown(
                        self._process_sem, seq, PermitKind.PROCESS):
                    self._put_back(task)
                    break

            if not self._acquire_with_shutdown(
                    self._global_sem, seq, PermitKind.GLOBAL):
                if is_process:
                    self._process_sem.release()
                self._put_back(task)
                break

            _logger.trace(
                "Task seq=%d acquired permits (global current=%d/%d, process available=%s)",
                seq,
                self._global_sem.current, self._global_sem.max,
                getattr(self._process_sem, "_value", "?"),
            )

            if is_process:
                self._submit_process(task)
            else:
                self._submit_thread(task)

        _logger.trace("Scheduler thread exited: %s", threading.current_thread().name)

    def _acquire_with_shutdown(self, sem, task_seq: int, kind: PermitKind) -> bool:
        start = time.monotonic()
        warned = False
        while not self._shutdown.is_set():
            try:
                if sem.acquire(timeout=0.5):
                    waited = time.monotonic() - start
                    if waited >= self._wait_warn_threshold:
                        self._record_backpressure(
                            BackpressureReason.PERMIT_WAIT,
                            seq=task_seq,
                            kind=kind,
                            waited=round(waited, 3),
                        )
                    return True
            except Exception as exc:
                _logger.warning("Semaphore acquire failed: %s", exc)
                return False

            if not warned and (time.monotonic() - start) >= self._wait_warn_threshold:
                warned = True
                self._record_backpressure(
                    BackpressureReason.PERMIT_WAIT,
                    seq=task_seq,
                    kind=kind,
                    waited=round(time.monotonic() - start, 3),
                )
        return False

    def _put_back(self, task: _Task):
        if self._shutdown.is_set():
            if not task.future.done():
                task.future.set_exception(RuntimeError("TaskService is shutting down"))
            return

        put_failed = False
        # 与 submit / _drain_queue 同一把锁，保证 drain 之后不会再往队列里塞东西。
        with self._submit_lock:
            # 二次检查：shutdown 可能刚刚在锁外设置
            if self._shutdown.is_set():
                if not task.future.done():
                    task.future.set_exception(
                        RuntimeError("TaskService is shutting down")
                    )
                return
            try:
                self._queue.put(task, timeout=1.0)
            except queue.Full:
                put_failed = True

        if put_failed:
            # 锁外记录背压：_record_backpressure 会调用用户回调，
            # 回调内部若重入 submit 会死锁。
            self._record_backpressure(
                BackpressureReason.PUT_BACK_FAILED, seq=task.seq,
            )
            if not task.future.done():
                task.future.set_exception(BackpressureError(
                    reason=BackpressureReason.PUT_BACK_FAILED,
                    queue_size=self._queue.qsize(),
                    max_queue_size=self._queue.maxsize,
                ))

    # ============================================================
    # 内部：提交到线程池 / 进程池
    # ============================================================
    def _submit_thread(self, task: _Task):
        seq = task.seq
        try:
            fut = self._thread_executor.submit(
                task.fn, *task.args, **task.kwargs,  # type: ignore[arg-type]
            )
        except Exception as exc:
            _logger.error("Failed to submit thread task seq=%d: %s", seq, exc)
            # set_exception 理论上可能抛 InvalidStateError；permit 必须在 finally 释放
            try:
                if not task.future.done():
                    task.future.set_exception(exc)
            finally:
                self._global_sem.release()
            return

        def callback(completed_future: Future):
            try:
                if not task.future.done():
                    task.future.set_result(completed_future.result())
            except Exception as exc:
                if not task.future.done():
                    task.future.set_exception(exc)
                _logger.error("Thread task seq=%d execution error: %s", seq, exc)
            finally:
                self._global_sem.release()

        fut.add_done_callback(callback)

    def _submit_process(self, task: _Task):
        seq = task.seq
        try:
            fut = self._process_executor.submit(
                task.fn, *task.args, **task.kwargs,  # type: ignore[arg-type]
            )
        except Exception as exc:
            _logger.error("Failed to submit process task seq=%d: %s", seq, exc)
            # 两个 permit 都必须在 finally 释放，避免 set_exception 抛异常时泄漏
            try:
                if not task.future.done():
                    task.future.set_exception(exc)
            finally:
                self._global_sem.release()
                self._process_sem.release()
            return

        def callback(completed_future: Future):
            try:
                if not task.future.done():
                    task.future.set_result(completed_future.result())
            except Exception as exc:
                if not task.future.done():
                    task.future.set_exception(exc)
                _logger.error("Process task seq=%d execution error: %s", seq, exc)
            finally:
                self._global_sem.release()
                self._process_sem.release()

        fut.add_done_callback(callback)

    # ============================================================
    # 内部：资源监控 & 动态调整
    # ============================================================
    # noinspection broad-exception
    def _monitor_resources(self):
        _logger.trace("Resource monitor thread started")

        try:
            psutil.cpu_percent(interval=None)
        except Exception:
            _logger.trace("Failed to preheat cpu_percent, "
                          "its subsequent return might be zero: %s",
                          format_exc())

        low_streak = 0
        while not self._shutdown.is_set():
            try:
                cpu = psutil.cpu_percent(interval=None)
                mem = psutil.virtual_memory().percent
            except Exception:
                _logger.warning("Failed to sample system resources: %s", format_exc())
                self._shutdown.wait(self._check_interval)
                continue

            high = (cpu > self._cpu_high) or (mem > self._mem_high)
            low = (cpu < self._cpu_low) and (mem < self._mem_low)

            if high:
                low_streak = 0
                cur_max = self._global_sem.max
                step = max(1, cur_max // 2)
                delta = self._global_sem.decrease_max(step)
                if delta > 0:
                    _logger.info(
                        "High resource usage (CPU=%.1f%%, mem=%.1f%%), "
                        "concurrency reduced %d -> %d (current in-flight=%d)",
                        cpu, mem, cur_max, self._global_sem.max,
                        self._global_sem.current,
                    )
                else:
                    _logger.trace(
                        "High resource usage but concurrency already at min (%d)",
                        self._global_sem.min,
                    )
            elif low:
                low_streak += 1
                if low_streak >= self._low_load_streak:
                    low_streak = 0
                    cur_max = self._global_sem.max
                    delta = self._global_sem.increase_max(1)
                    if delta > 0:
                        _logger.trace(
                            "Low resource usage (CPU=%.1f%%, mem=%.1f%%), "
                            "concurrency increased %d -> %d",
                            cpu, mem, cur_max, self._global_sem.max,
                        )
                    else:
                        _logger.trace(
                            "Concurrency already at hard max (%d)",
                            self._global_sem.hard_max,
                        )
            else:
                low_streak = 0

            self._shutdown.wait(self._check_interval)

        _logger.trace("Resource monitor thread exited")

    # ============================================================
    # 内部：关闭辅助
    # ============================================================
    @staticmethod
    def _fail_task(task: _Task, exc: BaseException):
        if not task.future.done():
            try:
                task.future.set_exception(exc)
            except InvalidStateError:
                pass

    def _drain_queue(self):
        # 持 _submit_lock：保证与 submit 的「关闭检查 + 入队」原子操作互斥。
        # 在本方法返回之后，任何 submit 都会看到 _shutdown 已置位并抛错，
        # 不会再往队列里塞入新任务。
        with self._submit_lock:
            while True:
                try:
                    task: _Task = self._queue.get_nowait()
                except queue.Empty:
                    break
                self._fail_task(task, RuntimeError("TaskService is shutting down"))
