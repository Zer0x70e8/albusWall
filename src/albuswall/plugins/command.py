#
""""""

from typing import Callable

from albuswall.core import Container, plugin, Application
from albuswall.log import getLogger
from albuswall.service.command import CommandService, setup_handlers

_logger = getLogger(__name__)


def _assemble_commands(container: Container) -> None:
    """装配所有 handler 模块，随即冻结注册表。

    两步必须在**同一个** loop 回调里按序执行：``freeze()`` 是"装配已结束"
    的运行时断言，此后任何 ``register`` 都会抛 ``RegistryFrozen``。拆成两个
    ``on_loop`` 也"碰巧"能跑（``run_hooks`` 按注册顺序执行），但把顺序押在
    hook 注册序上不值得——一个回调写完，顺序就是代码顺序。

    依赖用 ``require``：``command_registry`` 缺失属于装配错，应在启动期
    炸，而不是被 ``get`` 吞成 ``None`` 后在 ``.freeze()`` 上抛出
    ``AttributeError``。
    """
    registry = container.require("command_registry")
    setup_handlers(registry, container)
    registry.freeze()


@plugin("command", version="1.0.0")
def command(container: Container) -> tuple[Callable[[], None], Callable[[], None]]:
    def start() -> None:
        service: CommandService | None = container.get("command_service", None)
        if service is None:
            return

        Application.on_loop(lambda app: _assemble_commands(container))

    def stop() -> None:
        _logger.info("Command service stopped.")

    return start, stop
