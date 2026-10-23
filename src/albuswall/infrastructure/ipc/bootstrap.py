#
"""IPC 的容器注册与生命周期接线。

挂载点选 ``on_loop`` 而不是 ``on_boot``：
``on_boot`` 钩子按注册顺序执行，而本模块的钩子在**插件激活期**就注册了
（早于 ``register_service``），若在那里启动监听，就可能在
``SourceService.start()`` 之前被外部调用打到一份还没装载的 state 上。
``on_loop`` 在所有 ``on_boot`` 之后、``loop.run()`` 之前执行，是"全部就绪"
的唯一时刻。

拆除用 ``on_final`` 且同样是最早注册的，因此最先执行：先关掉对外入口，
再让服务按自己的顺序收尾。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from albuswall.core import Application
from albuswall.log import getLogger

from .config import Config as _Config
from .server import IpcServer

if TYPE_CHECKING:
    from albuswall.core import Container, MainLoop

__all__ = ["register_ipc"]

_logger = getLogger(__name__)
_config = _Config()


def register_ipc(container: Container) -> None:
    """把 IPC 服务挂进容器并接到应用生命周期上。

    IPC 是否**真的**启动由两件事决定：

    1. 插件层（``plugins/ipc.py``）是否被激活——由 ``config.ini`` 的
       ``[plugin_disables]`` 控制；
    2. ``[ipc] enabled``——由配置控制。

    两者都没有时才是「不监听」。
    """
    # 延迟导入：services 层依赖 infrastructure 的子模块，模块级导入容易
    # 在插件发现阶段撞上半初始化的包。
    from albuswall.services.command import Dispatcher

    if "command_dispatcher" in container or "ipc_server" in container:
        _logger.debug("IPC already registered, skipped.")
        return

    container.register(
        "command_dispatcher",
        lambda: Dispatcher(container),
        returns=Dispatcher,
    )
    container.register(
        "ipc_server",
        lambda: IpcServer(
            container.get("command_dispatcher"),
            call_timeout=_config.call_timeout,
            max_message_bytes=_config.max_message_bytes,
            max_connections=_config.max_connections,
        ),
        returns=IpcServer,
    )

    Application.on_loop(lambda loop: _start(container, loop))
    Application.on_final(lambda: _stop(container))


def _start(container: Container, loop: MainLoop) -> None:
    if not _config.enabled:
        _logger.info("IPC disabled by config ([ipc] enabled = false), not listening.")
        return

    call_soon = getattr(loop, "call_soon", None)
    if not callable(call_soon):
        # 第三方自定义 MainLoop 可能还没实现 call_soon：降级而不是崩溃
        _logger.warning(
            "%s does not implement call_soon(); IPC server not started.",
            type(loop).__name__,
        )
        return

    dispatcher = container.get("command_dispatcher")
    dispatcher.bind(loop)
    server: IpcServer = container.get("ipc_server")

    try:
        listening = server.start()
    except Exception:  # noinspection PyBroadException
        _logger.exception("failed to start IPC server; continuing without it.")
        return

    if not listening:
        _logger.info(
            "another AlbusWall instance already owns the IPC endpoint; "
            "this instance will not be reachable from CLI."
        )
        return

    if _config.event_bridge:
        _wire_events(container, server)


def _stop(container: Container) -> None:
    # 先断掉对外入口，再停服务：避免拆机过程中还有新命令进来
    if container.has_instance("command_dispatcher"):
        container.get("command_dispatcher").close()
    if container.has_instance("ipc_server"):
        server: IpcServer = container.get("ipc_server")
        try:
            server.stop()
        except Exception:  # noinspection PyBroadException
            _logger.exception("failed to stop IPC server")


# --------------------------------------------------------------------------- #
# 内部 Signal → IPC 事件
# --------------------------------------------------------------------------- #
# 契约（见 utils/signals.py）：cb(sender, **kwargs)。这里只做搬运，
# 不解释 payload——payload 的形状归信号的生产方定义。
def _wire_events(container: Container, server: IpcServer) -> None:
    bindings: list[tuple[str, str]] = [
        ("import_service", "assets_imported"),
        ("source_service", "scan_finished"),
    ]
    for service_key, signal_name in bindings:
        _bridge_signal(container, server, service_key, signal_name)


def _bridge_signal(
        container: Container,
        server: IpcServer,
        service_key: str,
        signal_name: str,
) -> None:
    topic = f"{service_key.removesuffix('_service')}.{signal_name}"
    signal = _find_signal(container, service_key, signal_name)
    if signal is None:
        _logger.debug("signal %s.%s not available, event %r skipped",
                      service_key, signal_name, topic)
        return

    def _forward(sender: Any, **payload: Any) -> None:
        try:
            server.broadcast(topic, payload or None)
        except Exception:  # noinspection PyBroadException
            _logger.exception("failed to broadcast %r", topic)

    try:
        # noinspection PyArgumentList
        signal.connect(_forward)
    except Exception:  # noinspection PyBroadException
        _logger.exception("failed to bridge signal %s.%s", service_key, signal_name)
        return
    _logger.debug("bridged %s.%s -> IPC event %r", service_key, signal_name, topic)


def _find_signal(
        container: Container,
        service_key: str,
        signal_name: str,
) -> Callable[..., Any] | None:
    try:
        service = container.get(service_key)
    except Exception:  # noinspection PyBroadException
        return None
    signal = getattr(service, signal_name, None)
    if signal is None or not hasattr(signal, "connect"):
        return None
    return signal
