#
""""""

from collections.abc import Callable
from typing import Any, TypedDict

from albuswall.core import Application, Container
from albuswall.utils.registry import build_getters, register_all

from .command import CommandService, IpcRequestService, setup_handlers


class Services(TypedDict):
    command_service: CommandService
    ipc_request_service: IpcRequestService


_ARG_MAP: dict[str, Callable[[Any, Container], Any]] = {
    # 从容器取，而不是直接 get_default_registry()：两者当前是同一个对象
    # （command/__init__.py 的 registry_command_registry 就是把该单例注册成
    # "command_registry"），但从容器取让测试能用 register("command_registry",
    # ...) 覆盖它而不污染全局单例；缺 key 时构造期抛 MissingDependencyError
    # （KeyError 子类），启动期即炸。
    "command_service": lambda cls, c: cls(c.require("command_registry")),
    # IPC 适配器：只依赖 ipc manager 和命令服务本身。
    "ipc_request_service": lambda cls, c: cls(
        c.get("ipc_service"),
        c.get("command_service"),
    ),
}


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


def register_service(container: Container):
    register_all(
        build_getters(
            container,
            Services.__annotations__,
            arg_map=_ARG_MAP
        ),
        container.reg,
    )

    # 顺序有意义，别对调：``_assemble_commands`` 必须先跑完（含 freeze），
    # 再安装 IPC handler。ipc manager 的 accept 线程早在 on_init 就起来了
    # （infrastructure/ipc/manager.py 的 ``Application.on_init(_setup)``），
    # 客户端随时可能连进来；若先 ``start()``，就会出现「handler 已挂上、
    # 注册表还空」的窗口，窗口内 ``help`` 拿到的是 unknown_command（exit 8），
    # 而优雅的 not-ready 语义是 no_handler。
    Application.on_loop(lambda app: _assemble_commands(container))
    Application.on_loop(lambda app: container.get("ipc_request_service").start())
    Application.on_exit(lambda: container.get("ipc_request_service").shutdown())
