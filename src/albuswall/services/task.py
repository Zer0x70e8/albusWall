#
"""
Unified Task Service: supports hybrid thread/process scheduling, priority queue,
dynamic concurrency control, detailed logging and backpressure observability.
"""

import inspect
import itertools
import multiprocessing
import pickle
import threading
import queue
import time
import psutil
from dataclasses import dataclass, field
from traceback import format_exc
from typing import Callable, Optional, Any
from concurrent.futures import (
    ThreadPoolExecutor,
    ProcessPoolExecutor,
    Future,
    InvalidStateError,
)

from albuswall.log import getLogger, Logger
from albuswall.dto.task import ExecutorType
from albuswall.common.exceptions import BackpressureError

_logger: Logger = getLogger(__name__)


@dataclass(order=True)
class _Task:
    """Task item in the priority queue."""
    priority: int
    seq: int
    fn: Callable[..., Any] = field(compare=False)
    args: tuple[Any, ...] = field(compare=False)
    kwargs: dict[str, Any] = field(compare=False)
    executor: ExecutorType = field(compare=False)
    future: Future = field(compare=False)


class DynamicSemaphore:
    """
    A semaphore that supports dynamically adjusting the maximum number of permits.

    关键语义:
      - 内部用 `_current` 记录"当前已持有 permit 的数量"
      - `_max` 是当前上限, 限制在 [_min, _hard_max] 之间
      - `acquire` 会阻塞直到 `_current < _max`
      - `release` 只会递减 `_current`, 不会让 `_current` 超过 `_max`
      - `decrease_max` 之后, 新任务会真正等待, 直到在跑的任务数降到新上限以下

    注意:
      - `decrease_max()` 之后，`_current` 可能暂时大于 `_max`。
        此时 `available` 返回 0，`acquire()` 会阻塞，直到有任务释放 permit，
        使 `_current < _max`。这是刻意行为，不是 bug。
    """

    def __init__(self, initial: int,
                 min_value: int = 1,
                 max_value: Optional[int] = None):
        if initial < min_value:
            initial = min_value
        if max_value is not None and initial > max_value:
            initial = max_value

        self._cond = threading.Condition()
        self._current = 0
        self._max = initial
        self._min = max(0, min_value)
        self._hard_max = max_value if max_value is not None else initial

    # ---------- acquire / release ----------
    def acquire(self, blocking: bool = True, timeout: Optional[float] = None) -> bool:
        with self._cond:
            if not blocking:
                if self._current < self._max:
                    self._current += 1
                    return True
                return False

            if timeout is None:
                while self._current >= self._max:
                    self._cond.wait()
                self._current += 1
                return True

            deadline = time.monotonic() + timeout
            while self._current >= self._max:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(remaining)
            self._current += 1
            return True

    def release(self):
        with self._cond:
            if self._current > 0:
                self._current -= 1
                self._cond.notify()
            else:
                _logger.warning("DynamicSemaphore.release() called with _current == 0")

    # ---------- dynamic adjust ----------
    def increase_max(self, n: int = 1) -> int:
        """Increase max permits (capped by hard_max). Returns the actual delta."""
        with self._cond:
            old = self._max
            self._max = min(self._hard_max, self._max + n)
            if self._max != old:
                self._cond.notify_all()
            return self._max - old

    def decrease_max(self, n: int = 1) -> int:
        """Decrease max permits (floored by _min). Returns the actual delta."""
        with self._cond:
            old = self._max
            self._max = max(self._min, self._max - n)
            if self._max != old:
                self._cond.notify_all()
            return old - self._max

    # ---------- properties ----------
    @property
    def max(self) -> int:
        return self._max

    @property
    def min(self) -> int:
        return self._min

    @property
    def hard_max(self) -> int:
        return self._hard_max

    @property
    def current(self) -> int:
        return self._current

    @property
    def available(self) -> int:
        return max(0, self._max - self._current)


# noinspection PyShadowingNames,SpellCheckingInspection
class TaskService:
    """
    Unified task service: all tasks enter a single priority queue, subject to a global concurrency limit.
    Supports both thread and process execution, dynamically adjusts global concurrency limit via psutil.

    动态调整策略:
      - 高负载 (CPU/mem 超过高阈值): 立即快速下调 (减半), 有下限保护
      - 低负载 (CPU/mem 都低于低阈值): 需要连续 N 次确认, 才 +1, 有上限保护
      - 其他情况: 保持不变

    背压:
      - 队列满 (queue_full): submit / put_back 时抛出或设置 BackpressureError
      - 阻塞式背压 (permit_wait): 等待 permit 超过阈值时记录

    进程任务约束:
      - `executor="process"` 的任务会被提交到 ProcessPoolExecutor(spawn)。
      - fn、args、kwargs 必须可 pickle。
      - 建议只使用模块顶层函数。
      - 明确拒绝：lambda、局部函数、绑定方法。
        （绑定方法如 ThumbnailService._work_one 请包装成顶层函数再提交。）

    put_back 语义:
      - 调度线程拿到任务后，如果因为 shutdown 或重新调度需要放回队列。
      - 如果队列已满，任务会被丢弃，但对应 future 会被设置 BackpressureError。
      - 调用方应通过 future.result() / future.exception() 感知。
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
        """
        :param initial_max_concurrency: 初始全局最大并发
        :param min_concurrency: 动态下限 (不会低于它)
        :param max_concurrency: 动态上限 (不会高于它); 默认 = initial * 4
        :param process_workers: 进程池 worker 数, 默认 = CPU 逻辑核数
        :param cpu_high_threshold: CPU 高阈值 (%)
        :param cpu_low_threshold: CPU 低阈值 (%)
        :param mem_high_threshold: 内存高阈值 (%)
        :param mem_low_threshold: 内存低阈值 (%)
        :param check_interval: 资源监控周期 (秒)
        :param scheduler_threads: 调度线程数
        :param max_queue_size: 任务队列容量上限 (背压)
        :param submit_timeout: submit 入队超时 (秒), 超时抛 BackpressureError
        :param low_load_streak: 连续多少次低负载才允许 +1
        :param wait_warn_threshold: 等待 permit 超过该秒数记为一次阻塞式背压
        """
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

        # 有界优先队列 (背压)
        self._queue: queue.PriorityQueue = queue.PriorityQueue(maxsize=max_queue_size)
        self._seq_counter = itertools.count()

        # 关闭标志
        self._shutdown = threading.Event()

        # ---------- 背压可观测性 ----------
        self._stats_lock = threading.Lock()
        self._stats = {
            "submitted": 0,
            "rejected": 0,
            "backpressure_reject": 0,  # 拒绝式: 队列满
            "backpressure_wait": 0,  # 阻塞式: 等 permit 超阈值
            "last_backpressure_ts": 0.0,
        }
        self._on_backpressure: Optional[Callable[[dict], None]] = None
        # 阻塞式背压日志节流: 距上次记录不足该秒数则跳过
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

    # ---------- 背压钩子 ----------
    def set_backpressure_callback(self, cb: Optional[Callable[[dict], None]]):
        """注册背压回调。cb 会收到一个 dict: {reason, queue_size, ...}。"""
        self._on_backpressure = cb

    def _record_backpressure(self, reason: str, **extra):
        """背压统一入口: 计数 + WARNING 日志 + 回调。"""
        qsize = self._queue.qsize()
        qmax = self._queue.maxsize
        now = time.time()

        with self._stats_lock:
            if reason == "queue_full" or reason == "put_back_failed":
                self._stats["backpressure_reject"] += 1
            else:
                self._stats["backpressure_wait"] += 1
            self._stats["last_backpressure_ts"] = now

        # 阻塞式背压做节流, 避免日志风暴
        if reason == "permit_wait":
            if now - self._last_wait_log_ts < self._bp_log_throttle:
                return
            self._last_wait_log_ts = now

        _logger.warning(
            "BACKPRESSURE reason=%s queue=%d/%d concurrency=%d/%d extra=%s",
            reason, qsize, qmax,
            self._global_sem.current, self._global_sem.max,
            extra,
        )

        cb = self._on_backpressure
        if cb is not None:
            cb: Callable[[dict], None]
            try:
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

    def get_stats(self) -> dict:
        with self._stats_lock:
            return dict(self._stats)

    # ---------- Process Task Validation ----------
    @staticmethod
    def _validate_process_task(fn: Callable, args: tuple, kwargs: dict) -> None:
        """
        校验 process 任务是否适合进入 ProcessPoolExecutor(spawn)。

        规则:
          - fn 必须可调用
          - 拒绝绑定方法、lambda、局部函数
          - fn/args/kwargs 必须可 pickle

        注意:
          即使 pickle.dumps 通过，也不代表子进程一定能 import 到 fn。
          最稳妥的用法是：fn 是模块顶层函数。
        """
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

    # ---------- Submit Task ----------
    def submit(self, fn: Callable, *args,
               executor: ExecutorType = "thread",
               priority: int = 0,
               **kwargs) -> Future:
        """
        Submit a task to the unified queue.

        :param fn: 可调用对象
        :param executor: 'thread' 或 'process'
        :param priority: 优先级, 数字越小优先级越高
        :return: Future 对象
        :raises RuntimeError: 服务已关闭
        :raises TypeError: process 任务不可 pickle 或使用了不支持的函数形式
        :raises BackpressureError: 队列已满 (背压触发)
        """
        if self._shutdown.is_set():
            _logger.error("Attempt to submit task after service shutdown, function=%s",
                          getattr(fn, "__name__", type(fn).__name__))
            raise RuntimeError("TaskService is shut down, cannot submit new tasks")

        if executor == "process":
            self._validate_process_task(fn, args, kwargs)

        with self._stats_lock:
            self._stats["submitted"] += 1

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

        try:
            self._queue.put(task, timeout=self._submit_timeout)
        except queue.Full:
            with self._stats_lock:
                self._stats["rejected"] += 1
            self._record_backpressure("queue_full", seq=seq, priority=priority)
            raise BackpressureError(
                reason="queue_full",
                queue_size=self._queue.qsize(),
                max_queue_size=self._queue.maxsize,
            )

        name = getattr(fn, "__name__", type(fn).__name__)
        _logger.trace("Task enqueued: seq=%d, priority=%d, executor=%s, function=%s",
                      seq, priority, executor, name)
        return future

    # ---------- Scheduler Loop ----------
    def _scheduler_loop(self):
        """Scheduler thread: fetch tasks from priority queue, acquire permits, and submit."""
        _logger.trace("Scheduler thread started: %s", threading.current_thread().name)

        while not self._shutdown.is_set():
            try:
                task: _Task = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue

            seq = task.seq
            is_process = (task.executor == "process")

            # 1) 进程任务先获取进程池 permit
            if is_process:
                if not self._acquire_with_shutdown(self._process_sem, seq, "process"):
                    self._put_back(task)
                    break

            # 2) 获取全局 permit
            if not self._acquire_with_shutdown(self._global_sem, seq, "global"):
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

            # 3) 提交
            if is_process:
                self._submit_process(task)
            else:
                self._submit_thread(task)

        _logger.trace("Scheduler thread exited: %s", threading.current_thread().name)

    def _acquire_with_shutdown(self, sem, task_seq: int, kind: str) -> bool:
        """
        在 sem 上循环 acquire(带超时), 同时响应 shutdown。
        超过 _wait_warn_threshold 秒仍未拿到, 记录一次阻塞式背压。
        返回 True 表示成功获取, False 表示服务已关闭。
        """
        start = time.monotonic()
        warned = False
        while not self._shutdown.is_set():
            try:
                if sem.acquire(timeout=0.5):
                    waited = time.monotonic() - start
                    if waited >= self._wait_warn_threshold:
                        self._record_backpressure(
                            "permit_wait",
                            seq=task_seq,
                            kind=kind,
                            waited=round(waited, 3),
                        )
                    return True
            except Exception as exc:
                _logger.warning("Semaphore acquire failed: %s", exc)
                return False

            # 循环内也可以提前预警一次 (等待超过阈值)
            if not warned and (time.monotonic() - start) >= self._wait_warn_threshold:
                warned = True
                self._record_backpressure(
                    "permit_wait",
                    seq=task_seq,
                    kind=kind,
                    waited=round(time.monotonic() - start, 3),
                )
        return False

    def _put_back(self, task: _Task):
        """
        关闭或需要重新调度时, 把任务放回队列。
        放不进去就丢弃任务, 并将 future 设置为 BackpressureError。

        语义:
          - 允许丢弃任务，但不会静默丢失；调用方通过 future 感知异常。
        """
        if self._shutdown.is_set():
            if not task.future.done():
                task.future.set_exception(RuntimeError("TaskService is shutting down"))
            return
        try:
            self._queue.put(task, timeout=1.0)
        except queue.Full:
            self._record_backpressure("put_back_failed", seq=task.seq)
            if not task.future.done():
                task.future.set_exception(BackpressureError(
                    reason="put_back_failed",
                    queue_size=self._queue.qsize(),
                    max_queue_size=self._queue.maxsize,
                ))

    def _submit_thread(self, task: _Task):
        seq = task.seq
        try:
            # 动态展开 fn(*args, **kwargs)，ParamSpec 无法推断，显式忽略
            fut = self._thread_executor.submit(
                task.fn, *task.args, **task.kwargs,  # type: ignore[arg-type]
            )
        except Exception as exc:
            _logger.error("Failed to submit thread task seq=%d: %s", seq, exc)
            if not task.future.done():
                task.future.set_exception(exc)
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
            # 注意：这里必须提交到 _process_executor，而不是 _thread_executor
            fut = self._process_executor.submit(
                task.fn, *task.args, **task.kwargs,  # type: ignore[arg-type]
            )
        except Exception as exc:
            _logger.error("Failed to submit process task seq=%d: %s", seq, exc)
            if not task.future.done():
                task.future.set_exception(exc)
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

    # ---------- Resource Monitor & Dynamic Adjustment ----------
    def _monitor_resources(self):
        """
        周期性检查系统资源, 动态调整全局并发上限。
        - 高负载: 快速下调 (减半, 至少 1)
        - 低负载: 需连续 low_load_streak 次, 才 +1
        """
        _logger.trace("Resource monitor thread started")

        # 预热 cpu_percent
        # noinspection PyBroadException
        try:
            psutil.cpu_percent(interval=None)
        except Exception:
            _logger.trace("Failed to preheat cpu_percent, "
                          "its subsequent return might be zero: %s",
                          format_exc())

        low_streak = 0
        while not self._shutdown.is_set():
            # noinspection PyBroadException
            try:
                cpu = psutil.cpu_percent(interval=None)
                mem = psutil.virtual_memory().percent
            except Exception:
                _logger.warning(
                    "Failed to sample system resources: %s", format_exc())
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
                        _logger.info(
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

    # ---------- Shutdown Helpers ----------
    @staticmethod
    def _fail_task(task: _Task, exc: BaseException):
        if not task.future.done():
            try:
                task.future.set_exception(exc)
            except InvalidStateError:
                pass

    def _drain_queue(self):
        """关闭时清空队列，给未处理任务的 future 统一设置异常。"""
        while True:
            try:
                task: _Task = self._queue.get_nowait()
            except queue.Empty:
                break
            self._fail_task(task, RuntimeError("TaskService is shutting down"))

    # ---------- Shutdown Service ----------
    def shutdown(self, wait: bool = True):
        """
        关闭服务：
          - 设置 shutdown 标志
          - 等待调度线程退出
          - 清空队列中剩余任务，并给 future 设置异常
          - 关闭线程池 / 进程池
        """
        if self._shutdown.is_set():
            return
        _logger.info("Starting TaskService shutdown, wait=%s", wait)
        self._shutdown.set()

        for t in self._scheduler_threads:
            t.join(timeout=3)
            if t.is_alive():
                _logger.warning("Scheduler thread %s did not exit cleanly", t.name)
        _logger.trace("Scheduler threads have all exited")

        # 调度线程退出后，队列中剩余任务不会再被处理，统一失败。
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

    # ---------- Status Query ----------
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


if __name__ == "__main__":
    import random
    import logging


    def io_task(task_id, sleep_time):
        print(f"[Thread] Task {task_id} started (sleep {sleep_time:.2f}s)")
        time.sleep(sleep_time)
        print(f"[Thread] Task {task_id} completed")
        return f"thread-{task_id}"


    def cpu_task(task_id, n):
        print(f"[Process] Task {task_id} started (compute {n} iterations)")
        total = 0
        for i in range(n):
            total += i * i
        print(f"[Process] Task {task_id} completed")
        return f"process-{task_id}"


    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    )


    def on_bp(evt: dict):
        print(f"[ALERT] backpressure triggered: {evt}")


    service = TaskService(
        initial_max_concurrency=6,
        min_concurrency=2,
        max_concurrency=32,
        process_workers=4,
        check_interval=3,
        scheduler_threads=2,
        low_load_streak=3,
        max_queue_size=500,
        submit_timeout=5.0,
        wait_warn_threshold=1.0,
    )
    service.set_backpressure_callback(on_bp)

    futures = []
    for idx in range(10):
        try:
            futures.append(service.submit(
                io_task, idx, random.uniform(0.3, 1.0),
                executor="thread", priority=random.randint(0, 4)))
        except BackpressureError as e:
            print(f"submit rejected by backpressure: {e}")

        try:
            futures.append(service.submit(
                cpu_task, idx, random.randint(1_000_000, 5_000_000),
                executor="process", priority=random.randint(0, 4)))
        except BackpressureError as e:
            print(f"submit rejected by backpressure: {e}")

    for f in futures:
        try:
            f.result()
        except Exception as e:
            print("Task failed:", e)

    print("All tasks completed")
    print("Stats:", service.get_stats())
    service.shutdown()
