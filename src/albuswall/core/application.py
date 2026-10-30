#
"""Project app object."""

from __future__ import annotations

from typing import Self

from albuswall.utils.hooks import run_hooks
from albuswall.utils.lazy import Lazy

from .builtin_hooks import register_builtin_hooks
from .container import Container
from .excepthook import install_excepthook
from .exit_codes import ExitCode
from .main_loop import HeadlessMainLoop, MainLoop
from .registry import hooks_map, register
from .types import HookNames, Hooks, LoopHook, SimpleHook

__all__ = ["Application", "CoreApplication"]

install_excepthook()


def _to_exit_code(code: int) -> ExitCode:
    """枚举内的值转为 ExitCode，其余（如 128+N 信号码）透传为 int。"""
    # if isinstance(code, ExitCode):
    #     return code
    # if isinstance(code, int) and code in ExitCode._value2member_map_:
    #     return ExitCode(code)
    # return ExitCode(code)
    return ExitCode(code)


class CoreApplication:
    _hooks_registered = False

    _loop: Lazy[MainLoop] = Lazy(HeadlessMainLoop)
    # 配置单例不再挂在这里：旧包的 ``configure = Lazy(Configue)`` 是无参构造，
    # 新包 ``Configue(base_dir)`` 必须显式给根目录，于是由
    # ``configue.bootstrap.setup_config(container)`` 装配后注册进容器。
    # 取用方式：``container.require("config")`` / ``container.require("configue")``。

    def __init__(self, main_loop: MainLoop | None = None) -> None:
        # hook 注册保持幂等；实例字段每次都要初始化
        if not CoreApplication._hooks_registered:
            register_builtin_hooks()
            CoreApplication._hooks_registered = True
        self._is_finally = False
        self.container = Container()
        if main_loop is not None:
            # noinspection bad-assignment
            self._loop = main_loop

    def exec(self) -> ExitCode | int:
        try:
            run_hooks(hooks_map["boot"])
            run_hooks(hooks_map["init"])
            run_hooks(hooks_map["loop"], args=(self, ))
            return _to_exit_code(self._loop.run())
        finally:
            self.finally_()

    def finally_(self) -> None:
        """Graceful exit.

        跑 exit 钩子。不 return、不 raise，不修改退出码。
        """
        if self._is_finally:
            return
        self._is_finally = True
        run_hooks(hooks_map["exit"])


class Application:
    _instance: CoreApplication | None = None
    _self: Application | None = None
    _is_init = False

    # noinspection bad-return
    def __new__(cls) -> Self:
        if cls._self is not None:
            return cls._self
        cls._instance = CoreApplication()
        cls._self = super().__new__(cls)
        return cls._self

    def __init__(self) -> None:
        if Application._is_init:
            return
        Application._is_init = True

    @classmethod
    def instance(cls) -> CoreApplication:
        assert cls._instance is not None
        return cls._instance

    @classmethod
    def register(cls, event: HookNames, hook: Hooks) -> None:
        register(event, hook)

    @classmethod
    def on_boot(cls, hook: SimpleHook) -> None:
        register("boot", hook)

    @classmethod
    def on_init(cls, hook: SimpleHook) -> None:
        register("init", hook)

    @classmethod
    def on_loop(cls, hook: LoopHook) -> None:
        register("loop", hook)

    @classmethod
    def on_exit(cls, hook: SimpleHook) -> None:
        register("exit", hook)

    @classmethod
    def container(cls):
        assert cls._instance is not None
        return cls._instance.container

    @classmethod
    def exec(cls) -> ExitCode:
        assert cls._instance is not None
        return ExitCode(cls._instance.exec())
