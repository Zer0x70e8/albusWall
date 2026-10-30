#
""""""

from albuswall.configue import configclass
from albuswall.core import Application, Container
from albuswall.infrastructure.task.protocol import TaskServiceProtocol
from albuswall.infrastructure.task.service import TaskService
from albuswall.log import getLogger

_logger = getLogger(__package__)


# noinspection bad-assignment
@configclass("task", "main")
class Config:
    """``[task]`` 段（与旧包 ``ConfigField("task", ...)`` 一一对应）。

    ``root = ConfigField("task", default={})`` 的「确保段存在」不需要了：
    新包 2 层 path + 写入时建段。

    ``int | None`` 字段在旧包 Python 3.10–3.13 上是坏的（``_convert`` 只认
    ``typing.Union``），随本次迁移一并被打进旧包的补丁修好了。
    """

    initial_max_concurrency: int = 10
    min_concurrency: int = 1
    max_concurrency: int | None = None
    process_workers: int | None = None

    cpu_high_threshold: float = 80.0
    cpu_low_threshold: float = 30.0
    mem_high_threshold: float = 85.0
    mem_low_threshold: float = 50.0

    check_interval: float = 5.0
    scheduler_threads: int = 2
    max_queue_size: int = 1000
    submit_timeout: float = 5.0
    low_load_streak: int = 3
    wait_warn_threshold: float = 1.0


_config = Config()


def register_task_service(
        container: Container
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
    Application.on_exit(_shutdown_task_service)
