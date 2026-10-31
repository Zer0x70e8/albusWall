#
"""Project app object."""

from __future__ import annotations

import logging
from typing import Self, Callable, Any

from albuswall.utils.hooks import run_hooks

from .builtin_hooks import register_builtin_hooks
from .container import Container
from .excepthook import install_excepthook
from .exit_codes import ExitCode
from .main_loop import MainLoop
from .plugins import PluginManager
from .registry import hooks_map, register
from .slot import SlotNotResolvedError, build_default_slots
from .types import HookNames, Hooks, LoopHook, SimpleHook

__all__ = ["Application", "CoreApplication"]

logger = logging.getLogger(__package__)

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

    # 配置单例不再挂在这里：旧包的 ``configure = Lazy(Configue)`` 是无参构造，
    # 新包 ``Configue(base_dir)`` 必须显式给根目录，于是由
    # ``configue.bootstrap.setup_config(container)`` 装配后注册进容器。
    # 取用方式：``container.require("config")`` / ``container.require("configue")``。

    def __init__(self, main_loop: MainLoop | None = None) -> None:
        if not CoreApplication._hooks_registered:
            register_builtin_hooks()
            CoreApplication._hooks_registered = True
        self._is_finally = False
        self.container = Container()
        self.container.set("app", self)  # 让 PluginManager 能从容器取
        self.slots = build_default_slots()
        if main_loop is not None:
            self.slots.get("main_loop").set_explicit(main_loop)

    # ---------------- 槽位 API ----------------
    def provide_slot(
            self,
            slot_name: str,
            name: str,
            factory: Callable[[Container], Any],
            *,
            priority: int = 0,
            source: str = "host",
    ) -> None:
        self.slots.get(slot_name).register(
            name, factory, priority=priority, source=source)

    def set_slot(self, slot_name: str, value: Any) -> None:
        self.slots.get(slot_name).set_explicit(value)

    def resolve_slot(self, slot_name: str) -> Any:
        slot = self.slots.get(slot_name)
        if slot.resolved is not None:
            return slot.resolved
        run_hooks(hooks_map[slot.select_hook])
        return slot.resolve(self.container)

        # ---------------- 便捷访问器 ----------------

    @property
    def plugins(self) -> PluginManager:
        resolved = self.slots.get("plugins").resolved
        if resolved is None:
            raise SlotNotResolvedError(
                "slot 'plugins' is not resolved; call "
                "app.resolve_slot('plugins') during boot")
        return resolved  # type: ignore[no-any-return]

    # ---------------- 生命周期 ----------------
    def exec(self) -> ExitCode | int:
        try:
            self.resolve_slot("config")
            self.resolve_slot("plugins")

            plugins = self.plugins
            if not plugins.setup_done:
                plugins.setup_all()
            run_hooks(hooks_map["boot"])
            run_hooks(hooks_map["init"])

            self.resolve_slot("main_loop")  # 插件此时可提供候选

            if not plugins.started:
                plugins.start_all()
            run_hooks(hooks_map["loop"], args=(self,))

            loop_slot = self.slots.get("main_loop")
            loop = loop_slot.resolved
            assert loop is not None
            loop_slot.mark_started()
            return _to_exit_code(loop.run())
        finally:
            self.finally_()

    def finally_(self) -> None:
        """Graceful exit.

        先停插件，再跑 exit 钩子。不 return、不 raise，不修改退出码。

        插件先于 exit 钩子停止：内置 exit 钩子因此不能依赖「某插件的资源还活
        着」。exit 钩子抛异常只记日志——退出路径上不允许再把进程弄崩。
        """
        if self._is_finally:
            return
        self._is_finally = True
        plugins = self.slots.get("plugins").resolved
        if plugins is not None:
            plugins.stop_all()
        try:
            run_hooks(hooks_map["exit"])
        except Exception:
            logger.exception("exit hook failed")


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
    def provide_slot(cls, slot_name, name, factory, *, priority=0, source="host"):
        cls._instance.provide_slot(slot_name, name, factory,
                                   priority=priority, source=source)

    @classmethod
    def set_slot(cls, slot_name, value):
        cls._instance.set_slot(slot_name, value)

    @classmethod
    def resolve_slot(cls, slot_name):
        return cls._instance.resolve_slot(slot_name)

    # 兼容糖（保留旧调用点）
    @classmethod
    def set_main_loop(cls, loop):
        cls.set_slot("main_loop", loop)

    @classmethod
    def provide_main_loop(cls, name, factory, *, priority=0):
        cls.provide_slot("main_loop", name, factory, priority=priority)

    @classmethod
    def resolve_main_loop(cls):
        return cls.resolve_slot("main_loop")

    @classmethod
    def on_select_config(cls, hook):
        register("select_config", hook)

    @classmethod
    def on_select_plugins(cls, hook):
        register("select_plugins", hook)

    @classmethod
    def on_select_loop(cls, hook):
        register("select_loop", hook)

    @classmethod
    def plugins(cls) -> PluginManager:
        """取实例级插件管理器。"""
        assert cls._instance is not None
        return cls._instance.plugins

    @classmethod
    def load_plugins(cls, *, group: str = "albuswall.plugins") -> PluginManager:
        """加载第三方 ``entry_points`` 声明的插件，返回管理器。"""
        manager = cls.plugins()
        manager.load_entry_points(group=group)
        return manager

    @classmethod
    def exec(cls) -> ExitCode:
        assert cls._instance is not None
        return ExitCode(cls._instance.exec())
