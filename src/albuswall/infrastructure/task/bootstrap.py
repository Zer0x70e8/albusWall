#
""""""

from albuswall.core import Container, Application
from albuswall.configue import ConfigField
from albuswall.log import getLogger

from albuswall.infrastructure.task.protocol import TaskServiceProtocol
from albuswall.infrastructure.task.service import TaskService

_logger = getLogger(__package__)


# noinspection bad-assignment
class Config:
    root = ConfigField("task", default={})  # Ensure section.

    initial_max_concurrency: int = ConfigField("task", "initial_max_concurrency", default=10)
    min_concurrency: int = ConfigField("task", "min_concurrency", default=1)
    max_concurrency: int | None = ConfigField("task", "max_concurrency", default=None)
    process_workers: int | None = ConfigField("task", "process_workers", default=None)

    cpu_high_threshold: float = ConfigField("task", "cpu_high_threshold", default=80.0)
    cpu_low_threshold: float = ConfigField("task", "cpu_low_threshold", default=30.0)
    mem_high_threshold: float = ConfigField("task", "mem_high_threshold", default=85.0)
    mem_low_threshold: float = ConfigField("task", "mem_low_threshold", default=50.0)

    check_interval: float = ConfigField("task", "check_interval", default=5.0)
    scheduler_threads: int = ConfigField("task", "scheduler_threads", default=2)
    max_queue_size: int = ConfigField("task", "max_queue_size", default=1000)
    submit_timeout: float = ConfigField("task", "submit_timeout", default=5.0)
    low_load_streak: int = ConfigField("task", "low_load_streak", default=3)
    wait_warn_threshold: float = ConfigField("task", "wait_warn_threshold", default=1.0)


_config = Config()


def register_task_service(
        container  # type: Container
) -> None:
    container.register(
        "task_service",
        lambda: TaskService(
            initial_max_concurrency=_config.initial_max_concurrency,
            min_concurrency=_config.min_concurrency,
            max_concurrency=_config.max_concurrency,
            process_workers=_config.process_workers,
            cpu_high_threshold=_config.cpu_high_threshold,
            cpu_low_threshold=_config.cpu_low_threshold,
            mem_high_threshold=_config.mem_high_threshold,
            mem_low_threshold=_config.mem_low_threshold,
            check_interval=_config.check_interval,
            scheduler_threads=_config.scheduler_threads,
            max_queue_size=_config.max_queue_size,
            submit_timeout=_config.submit_timeout,
            low_load_streak=_config.low_load_streak,
            wait_warn_threshold=_config.wait_warn_threshold,
        ),
        returns=TaskServiceProtocol,
    )

    def _shutdown_task_service() -> None:
        """仅当 TaskService 真的被实例化过时才关闭它。"""
        if not container.has_instance("task_service"):
            _logger.debug("TaskService was never instantiated, skip shutdown.")
            return
        container.get("task_service").shutdown(wait=True)

    # 插到最前：先停任务，再让下游收尾（db 在 on_final 里追加，顺序靠后）
    Application.on_final(_shutdown_task_service)
