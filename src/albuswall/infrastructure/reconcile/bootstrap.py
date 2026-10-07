#
"""把 Reconciler 注册进容器，并挂生命周期钩子。"""

from albuswall.core import Container, Application
from albuswall.log import getLogger

from albuswall.infrastructure.reconcile.service import Reconciler

_logger = getLogger(__package__)
app = Application.instance()


def register_reconciler(
        container,  # type: Container
) -> None:
    container.register(
        "reconciler",
        lambda: Reconciler(container.get("task_service")),
        returns=Reconciler,
    )

    def _start_reconciler() -> None:
        # 惰性：没人构造过就不启动，也就没有 on_startup 项要跑
        if not container.has_instance("reconciler"):
            _logger.debug("Reconciler was never instantiated, skip start.")
            return
        container.get("reconciler").start()

    def _shutdown_reconciler() -> None:
        if not container.has_instance("reconciler"):
            _logger.debug("Reconciler was never instantiated, skip shutdown.")
            return
        container.get("reconciler").shutdown()

    # 启动：在 task_service 已就绪、其它服务注册完之后。
    app.on_boot(_start_reconciler)

    # 关闭：必须在 task_service 之前。
    # task/bootstrap.py 用 on_final 注册了 task 关闭；这里也用 on_final，
    # 但注册顺序靠后 → 后注册的 on_final 先跑 → 我们先停，
    # 之后 task 服务的关闭才把余下任务清空。
    app.on_final(_shutdown_reconciler)
