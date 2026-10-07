#
"""TrashCleanupScheduler: 周期性把 TrashService.cleanup_expired 投递给 TaskService。

职责边界
--------
* **TrashService**       —— 纯粹的用例编排，无生命周期。只暴露 cleanup_expired()。
* **TaskService**        —— 并发执行 + 优先级 + 背压，统一管理线程池。
* **TrashCleanupScheduler**（本模块）—— 定时投递。不执行任何清理逻辑，
  不感知挂钟时间，不感知保留期策略。

为什么用 TaskService 而非自建线程
----------------------------------
* 清理任务天然和导入 / 缩略图任务共享 CPU / IO，走统一并发限制更合理。
* 背压交给 TaskService 统一处理；清理抢不到 permit 时自动退让，
  不阻塞前台任务。
* 生命周期钩子归一：只需管 scheduler.start/stop，不另开线程池。
* 观测统一：清理任务的执行/异常都进 TaskService 的统计。

时钟契约
--------
调度器**不感知**挂钟时间。周期等待基于 ``threading.Event.wait(timeout)``，
底层是 monotonic 时钟，不受系统时间调整影响。
"什么时候该清理"由 TrashService 内部的水位机制决定。
"""

from __future__ import annotations

import threading
from concurrent.futures import Future
from datetime import timedelta
from typing import Optional

from albuswall.dto.task import ExecutorType
from albuswall.infrastructure.task.protocol import TaskServiceProtocol
from albuswall.log import getLogger

from .service import TrashService

logger = getLogger("albuswall.trash.scheduler")

# ── 策略常量 ────────────────────────────────────────────────────────
DEFAULT_INTERVAL = timedelta(hours=1)
"""清理周期。保留期 30 天，1 小时一轮足以保证"到期后 1 小时内清除"。"""

DEFAULT_INITIAL_DELAY = timedelta(seconds=30)
"""进程启动到首次清理的延迟，让启动阶段其它服务的 IO 先完成。"""

CLEANUP_PRIORITY = 10
"""低优先级。TaskService 按 priority 升序执行，默认 0 是最高。
清理让位给导入 / 缩略图。"""


class TrashCleanupScheduler:
    """定时把 ``TrashService.cleanup_expired`` 提交到 TaskService。

    并发保证
    --------
    同一时刻最多有一个 cleanup 在跑。上一轮的 Future 未完成时，本轮 tick
    被跳过。这条约束是必需的——``cleanup_expired`` 有水位语义，不允许并发。
    """

    def __init__(
            self,
            trash_service: TrashService,
            task_service: TaskServiceProtocol,
            *,
            interval: timedelta = DEFAULT_INTERVAL,
            initial_delay: timedelta = DEFAULT_INITIAL_DELAY,
            priority: int = CLEANUP_PRIORITY,
    ) -> None:
        self._trash = trash_service
        self._task = task_service
        self._interval = interval
        self._initial_delay = initial_delay
        self._priority = priority

        self._stop_event = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._lifecycle_lock = threading.Lock()

        # 上一次提交但尚未完成的 cleanup future；用于去重。
        self._inflight: Optional[Future] = None
        self._inflight_lock = threading.Lock()

    # ────────────────────────────────────────────────────────────────
    # 生命周期
    # ────────────────────────────────────────────────────────────────
    def start(self) -> None:
        """启动后台 tick 线程。幂等。"""
        with self._lifecycle_lock:
            if self._worker is not None and self._worker.is_alive():
                logger.debug("TrashCleanupScheduler already running")
                return
            self._stop_event.clear()
            self._worker = threading.Thread(
                target=self._run_loop,
                name="albuswall-trash-scheduler",
                daemon=True,
            )
            self._worker.start()
            logger.info(
                "TrashCleanupScheduler started (initial_delay=%s, interval=%s)",
                self._initial_delay, self._interval,
            )

    def stop(self, wait: bool = True) -> None:
        """停止 tick 线程。

        * 已提交但未完成的 cleanup 任务不等待——它由 TaskService 管理，
          由 ``TaskService.shutdown(wait=True)`` 统一收割。
        * 本方法只保证"之后不会再向 TaskService 提交新任务"。
        """
        with self._lifecycle_lock:
            worker = self._worker
            if worker is None:
                return
            self._stop_event.set()
            self._worker = None

        # join 在锁外，避免长时间持锁阻塞并发的 start/stop
        if wait and worker.is_alive():
            worker.join()
        logger.info("TrashCleanupScheduler stopped")

    # ────────────────────────────────────────────────────────────────
    # 手动触发（管理端点 / 测试）
    # ────────────────────────────────────────────────────────────────
    def trigger_now(self) -> Optional[Future]:
        """立即提交一次清理任务，返回对应 Future。

        已有 inflight 时返回 ``None``——调用方据此判断"未触发"。
        用于：管理页"立即清理"按钮、集成测试。
        """
        return self._submit_cleanup()

    # ────────────────────────────────────────────────────────────────
    # 内部：tick 循环
    # ────────────────────────────────────────────────────────────────
    def _run_loop(self) -> None:
        if self._stop_event.wait(self._initial_delay.total_seconds()):
            return

        while True:
            self._submit_cleanup()
            if self._stop_event.wait(self._interval.total_seconds()):
                return

    # noinspection broad-exception
    def _submit_cleanup(self) -> Optional[Future]:
        """向 TaskService 提交一次清理。已有 inflight 时跳过本轮。

        异常隔离：``submit`` 本身可能因背压 / 服务已关闭抛错，捕获后
        只记日志，不打断 tick 循环——下一轮再试。
        """
        with self._inflight_lock:
            if self._inflight is not None and not self._inflight.done():
                logger.debug(
                    "Previous cleanup still in flight; skipping this tick"
                )
                return None

            try:
                fut = self._task.submit(
                    self._trash.cleanup_expired,
                    executor=ExecutorType.THREAD,
                    priority=self._priority,
                )
            except Exception:
                logger.exception("Failed to submit trash cleanup task")
                return None

            self._inflight = fut

        fut.add_done_callback(self._on_cleanup_done)
        return fut

    # noinspection broad-exception
    @staticmethod
    def _on_cleanup_done(fut: Future) -> None:
        """任务完成回调：仅记录异常，不修改状态。

        清理本身已经做了水位保护，单次失败不应影响下一轮——水位没被
        推进，下一轮会重试。
        """
        try:
            fut.result()
        except Exception:
            logger.exception("Trash cleanup task raised")
