#
"""外部资源 ↔ db 记录的对账器。

db 是真相源，外部资源（缩略图目录、缓存文件……）是副本。
本模块负责把「副本里已经不该存在的 key」清掉，不负责生成。

对外只有三件事：
    - ``invalidate(name, key)`` —— 已知某个 key 在 db 中已消失
    - ``reconcile(name)``       —— 不知道具体哪些 key，全量对账一次
    - ``start()``               —— 跑一遍所有 on_startup=True 的注册项

所有实际工作提交给 task 服务执行；本类不碰 IO、不做业务判断、
不持有长期后台线程（仅在 debounce 期间短暂存在一个 Timer）。
"""

from __future__ import annotations

import threading
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Callable, Optional

from albuswall.common.exceptions import BackpressureError
from albuswall.log import getLogger, Logger
from albuswall.infrastructure.task.protocol import TaskServiceProtocol

_logger: Logger = getLogger(__package__)

IncrementalFn = Callable[[set[str]], None]
FullFn = Callable[[], None]
ShouldRunFn = Callable[[], bool]

# 提交失败 / 谓词拒绝时的最小重试间隔，避免 CPU 高企时高频空转
_RETRY_MIN_DELAY = 10.0
_PRIORITY = 5


@dataclass
class _Entry:
    name: str
    incremental: IncrementalFn
    full: FullFn
    debounce: float
    priority: int
    on_startup: bool
    should_run: Optional[ShouldRunFn]

    # 可变状态，全部在 Reconciler._lock 保护下读写
    pending: set[str] = field(default_factory=set)
    full_requested: bool = False
    timer: Optional[threading.Timer] = None
    in_flight: Optional[Future] = None


# noinspection broad-exception
class Reconciler:
    """把「外部资源 ↔ db 记录」的对账统一收口的基础设施。"""

    def __init__(
            self,
            task_service: TaskServiceProtocol,
            *,
            log: Optional[Logger] = None,
    ) -> None:
        self._tasks = task_service
        self._log = log or _logger
        self._entries: dict[str, _Entry] = {}
        self._lock = threading.RLock()
        self._shutdown = False

    # ============================================================
    # 公共 API
    # ============================================================
    def register(
            self,
            name: str,
            *,
            incremental: IncrementalFn,
            full: FullFn,
            on_startup: bool = False,
            debounce: float = 3.0,
            priority: int = _PRIORITY,
            should_run: Optional[ShouldRunFn] = None,
    ) -> None:
        """注册一个对账目标。

        :param name:        唯一标识，与其它对账目标隔离
        :param incremental: 收到一批「已知失效」的 key，把它们的外部资源清掉
        :param full:        全量对账：扫描外部资源与 db 对比，清理差集
        :param on_startup:  ``start()`` 时自动跑一次 ``full``
        :param debounce:    ``invalidate`` 的合并窗口（秒）
        :param priority:    提交给 task 服务的优先级；默认为负值，
                            让对账永远排在用户可见任务之后
        :param should_run:  可选准入谓词；返回 False 时不提交，
                            等下一次触发再试
        """
        if debounce < 0:
            raise ValueError("debounce must be >= 0")
        with self._lock:
            if self._shutdown:
                raise RuntimeError("Reconciler already shut down")
            if name in self._entries:
                raise ValueError(f"duplicate reconciler: {name!r}")
            self._entries[name] = _Entry(
                name=name,
                incremental=incremental,
                full=full,
                debounce=debounce,
                priority=priority,
                on_startup=on_startup,
                should_run=should_run,
            )
            self._log.debug("reconciler %s registered", name)

    def invalidate(self, name: str, key: str) -> None:
        """标记一个 key 已失效。

        多次调用会被 debounce 合并成一批；若已有任务在飞，
        则直接并入 pending，由任务完成回调接手。
        """
        with self._lock:
            if self._shutdown:
                return
            e = self._entries.get(name)
            if e is None:
                raise KeyError(f"unknown reconciler: {name!r}")
            e.pending.add(key)
            if e.in_flight is not None and not e.in_flight.done():
                return
            self._arm_timer_locked(e)

    def reconcile(self, name: str) -> Optional[Future]:
        """触发一次全量对账。

        同名任务已在飞时返回现有的 future（全量请求会被排到当前任务之后）；
        否则立刻提交并返回新 future；提交失败返回 None。
        """
        with self._lock:
            if self._shutdown:
                return None
            e = self._entries.get(name)
            if e is None:
                raise KeyError(f"unknown reconciler: {name!r}")
            e.full_requested = True
            if e.in_flight is not None and not e.in_flight.done():
                return e.in_flight
            return self._dispatch_locked(e)

    def start(self) -> None:
        """跑一遍所有 ``on_startup=True`` 的注册项。非阻塞。"""
        with self._lock:
            if self._shutdown:
                return
            names = [n for n, e in self._entries.items() if e.on_startup]
        for n in names:
            try:
                self.reconcile(n)
            except Exception:
                self._log.exception("reconciler %s startup reconcile failed", n)

    def shutdown(self) -> None:
        """取消所有挂起的定时器。进行中的 task 由 task 服务负责收尾。"""
        with self._lock:
            self._shutdown = True
            for e in self._entries.values():
                if e.timer is not None:
                    e.timer.cancel()
                    e.timer = None
            self._entries.clear()

    # ============================================================
    # 内部
    # ============================================================
    def _arm_timer_locked(self, e: _Entry, delay: Optional[float] = None) -> None:
        if e.timer is not None:
            e.timer.cancel()
        d = e.debounce if delay is None else delay
        e.timer = threading.Timer(d, self._on_timer, args=(e.name,))
        e.timer.daemon = True
        e.timer.start()  # type: ignore

    def _on_timer(self, name: str) -> None:
        with self._lock:
            if self._shutdown:
                return
            e = self._entries.get(name)
            if e is None:
                return
            e.timer = None
            if e.in_flight is not None and not e.in_flight.done():
                return
            self._dispatch_locked(e)

    def _dispatch_locked(self, e: _Entry) -> Optional[Future]:
        """已持锁。从 full / incremental 中择一提交，或什么都不做。"""
        if self._shutdown:
            return None

        if e.full_requested:
            fn: Callable = e.full
            is_full = True
            payload: Optional[set[str]] = None
        elif e.pending:
            fn = e.incremental
            is_full = False
            payload = set(e.pending)
        else:
            return None

        # 准入谓词
        if e.should_run is not None:
            try:
                ok = bool(e.should_run())
            except Exception:
                self._log.exception(
                    "reconciler %s should_run raised; treating as False", e.name)
                ok = False
            if not ok:
                self._log.debug("reconciler %s deferred by should_run", e.name)
                self._arm_timer_locked(e, max(e.debounce, _RETRY_MIN_DELAY))
                return None

        # 通过谓词，先清状态再提交；失败再回滚
        if is_full:
            e.full_requested = False
            e.pending.clear()  # 全量覆盖所有 pending
            args: tuple = ()
        else:
            e.pending.clear()
            args = (payload,)

        try:
            fut = self._tasks.submit(fn, *args, priority=e.priority)
        except (BackpressureError, RuntimeError) as exc:
            self._log.warning("reconciler %s submit failed: %s", e.name, exc)
            if is_full:
                e.full_requested = True
            elif payload:
                e.pending |= payload
            self._arm_timer_locked(e, max(e.debounce, _RETRY_MIN_DELAY))
            return None

        e.in_flight = fut
        fut.add_done_callback(
            lambda f, _n=e.name: self._on_done(_n, f))
        self._log.debug(
            "reconciler %s dispatched (%s)",
            e.name,
            "full" if is_full else f"incr[{len(payload) if payload else 0}]",
        )
        return fut

    def _on_done(self, name: str, fut: Future) -> None:
        try:
            exc = fut.exception()
        except BaseException as ex:  # CancelledError 等
            exc = ex
        if exc is not None:
            self._log.error("reconciler %s task raised: %r", name, exc)

        with self._lock:
            if self._shutdown:
                return
            e = self._entries.get(name)
            if e is None:
                return
            e.in_flight = None
            if e.full_requested or e.pending:
                self._dispatch_locked(e)
