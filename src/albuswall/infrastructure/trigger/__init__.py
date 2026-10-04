#
"""
Trigger 基础设施的 bootstrap。

职责：
* 把 TriggerFacadeService 及其默认后端注册进 DI 容器；
* 把生命周期的 stop 挂到容器 final 回调上；
* 保持基础设施的纯粹性——不读取任何 repository，配置由应用层喂入。

应用层需要自己做两件事：
1. 在合适的时机调用 ``container.require("trigger_facade").start(configs)``；
2. 当数据变化时调用 ``reload(configs)`` / ``upsert(key, config)`` / ``remove(key)``。
"""

from typing import Optional

from albuswall.core import Container, Application
from albuswall.log import getLogger

from .trigger import (
    SchedulerService,
    TriggerFacadeService,
)
from .protocols import (
    TriggerHandler,
    DeviceTriggerHandler
)

_logger = getLogger(__name__)


def register_trigger_service(
        container: Container,
        *,
        scheduler_handler: Optional[TriggerHandler] = None,
        device_handler: Optional[DeviceTriggerHandler] = None,
) -> None:
    """把触发器基础设施注册进容器。

    ``update_callback`` 不再由调用方注入 —— 它固定为发射
    ``trigger_refresh_signal``。出站链路由应用层单独接线。
    """

    def _fire(key: object) -> None:
        # 延迟解析：注册时信号可能还没注册
        container.get("trigger_refresh_signal").emit(key)

    def _make_scheduler() -> TriggerHandler:
        if scheduler_handler is not None:
            return scheduler_handler
        return SchedulerService(_fire, _logger)

    def _make_facade() -> TriggerFacadeService:
        return TriggerFacadeService(
            _fire,
            scheduler_handler=container.get("trigger_scheduler"),
            device_handler=device_handler,
        )

    container.register("trigger_scheduler", _make_scheduler, returns=TriggerHandler)
    container.register("trigger_facade", _make_facade, returns=TriggerFacadeService)

    def _shutdown_trigger() -> None:
        if not container.has_instance("trigger_facade"):
            _logger.debug("trigger_facade was never instantiated; nothing to stop")
            return
        # noinspection broad-exception
        try:
            container.require("trigger_facade").stop()
        except Exception:
            _logger.exception("Failed to stop trigger facade")

    Application.on_final(_shutdown_trigger)
