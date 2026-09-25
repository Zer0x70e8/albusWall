#
""""""

import sys
import threading
import traceback
from typing import TYPE_CHECKING, Callable, Optional, Any

# 引导期异常兜底（必须放在任何可能失败的代码之前）
# 目的：从 import 那一刻起，保证未捕获异常一定写到 stderr，
#       即使后面 _Application 构造失败也不会静默丢失。

_origin_sys_excepthook = sys.excepthook
_origin_threading_excepthook = threading.excepthook


def _bootstrap_sys_hook(exc_type, exc_value, exc_tb):
    # noinspection broad-exception
    try:
        sys.stderr.write("[albuswall][bootstrap] uncaught exception:\n")
        traceback.print_exception(exc_type, exc_value, exc_tb, file=sys.stderr)
    except Exception:
        # 兜底输出自身不能再抛
        pass


def _bootstrap_thread_hook(args):
    # noinspection broad-exception
    try:
        sys.stderr.write("[albuswall][bootstrap] uncaught thread exception:\n")
        traceback.print_exception(
            args.exc_type, args.exc_value, args.exc_traceback, file=sys.stderr
        )
    except Exception:
        pass


sys.excepthook = _bootstrap_sys_hook
threading.excepthook = _bootstrap_thread_hook

# ============================================================
# 正常导入
# ============================================================

from .bootstrap import Container
from .main_loop import HeadlessMainLoop, MainLoop
from albuswall.plugin.manager import PluginManager

if TYPE_CHECKING:
    from albuswall.configue import Configue

# 约定：handler 返回 False 表示“我处理不了，请继续往下传”
ExceptionHandler = Callable[[type, BaseException, Any], Optional[bool]]
ThreadExceptionHandler = Callable[[threading.ExceptHookArgs], Optional[bool]]


class LazyConfigue:
    def __set_name__(self, owner, name):
        self.name = name

    def __get__(self, obj, obj_type=None):
        if obj is None:
            return self
        # 第一次访问时创建并缓存
        if self.name not in obj.__dict__:
            # 延迟导入，避免循环依赖
            from albuswall.configue import Configue
            obj.__dict__[self.name] = Configue()
        return obj.__dict__[self.name]


class _Application:
    configure: "Configue" = LazyConfigue()  # type: ignore

    def __init__(self):
        self.container: Container = Container()
        self.plugins = PluginManager()
        self._main_loop: Optional[MainLoop] = None
        self._quit_hooks: list[Callable[[str], None]] = []
        self._loop_hooks: list[Callable[[MainLoop], None]] = []

        self.log_enable = False

        # 记住系统原始 hook，便于二次委托
        # 注意：这里使用模块加载时捕获的原始 hook，
        # 而不是当前（可能已被 _bootstrap_* 替换的）sys.excepthook，
        # 否则二次委托会绕回引导钩子。
        self._origin_sys_excepthook = _origin_sys_excepthook
        self._origin_threading_excepthook = _origin_threading_excepthook

        # 内部真实 handler（通过 property 对外暴露，可随时替换）
        # 约定：返回 False 表示“我处理不了，请继续往下传”
        # 默认就返回 False，等于默认走系统原始 hook（打印到 stderr）
        self._handle_exception: ExceptionHandler = lambda *a: False
        self._thread_exception: ThreadExceptionHandler = lambda *a: False

        self.setup()

    # ---------- 异常处理属性：随时可替换，wrapper 会读最新值 ----------
    @property
    def handle_exception(self) -> ExceptionHandler:
        return self._handle_exception

    @handle_exception.setter
    def handle_exception(self, fn: ExceptionHandler) -> None:
        self._handle_exception = fn

    @property
    def thread_exception(self) -> ThreadExceptionHandler:
        return self._thread_exception

    @thread_exception.setter
    def thread_exception(self, fn: ThreadExceptionHandler) -> None:
        self._thread_exception = fn

    # ---------- 二次委托入口 ----------
    def call_origin_exception(
            self,
            exc_type: type,
            exc_value: BaseException,
            exc_tb: Any,
    ) -> None:
        """把异常二次交给系统原始的 excepthook（默认打印到 stderr）。"""
        # noinspection bad-argument-type
        self._origin_sys_excepthook(exc_type, exc_value, exc_tb)

    def call_origin_thread_exception(self, args: threading.ExceptHookArgs) -> None:
        """把线程异常二次交给系统原始的 threading.excepthook。"""
        self._origin_threading_excepthook(args)

    @property
    def main_loop(self) -> MainLoop:
        if self._main_loop is None:
            self._main_loop = HeadlessMainLoop()
        # noinspection bad-return
        return self._main_loop

    @main_loop.setter
    def main_loop(self, loop: MainLoop):
        if self._main_loop is not None and self._main_loop is not loop:
            # noinspection string-conversion-without-dunder-method
            msg = (f"Replacing existing main loop "
                   f"{self._main_loop!r} with {loop!r}")
            raise RuntimeError(msg)
        self._main_loop = loop

    # hook

    def on_quit(self, fn: Callable[[str], None]) -> Callable[[str], None]:
        """装饰器：注册退出钩子。可重复注册，先注册先执行。"""
        self._quit_hooks.append(fn)
        return fn

    def on_loop(self, fn: Callable[[MainLoop], None]) -> Callable[[MainLoop], None]:
        """装饰器：注册进入主循环前的钩子。可重复注册，先注册先执行。"""
        self._loop_hooks.append(fn)
        return fn

    #
    def request_quit(self, reason: str = "") -> None:
        for fn in self._quit_hooks:
            fn(reason)
        if self._main_loop is not None:
            self._main_loop.quit()

    def setup(self):
        self.container.reg("configue", lambda: self.configure, returns="Configue")
        self.container.reg("plugins", lambda: self.plugins, returns=PluginManager)

        # 安装稳定的 wrapper：每次都从 self 读取当前 handler
        # noinspection broad-exception,none-function-assignment,unreachable-code,simplify-boolean-check
        def _sys_hook(exc_type, exc_value, exc_tb):
            handler = self._handle_exception
            if handler is None:
                # 没有 handler，直接走系统默认
                self._origin_sys_excepthook(exc_type, exc_value, exc_tb)
                return
            try:
                result = handler(exc_type, exc_value, exc_tb)
            except Exception:
                # handler 自己崩了，二次交给系统原始 hook
                self._origin_sys_excepthook(exc_type, exc_value, exc_tb)
                return
            # handler 显式返回 False → 二次委托
            if result is False:
                self._origin_sys_excepthook(exc_type, exc_value, exc_tb)

        # noinspection broad-exception,none-function-assignment,unreachable-code,simplify-boolean-check
        def _thread_hook(args):
            handler = self._thread_exception
            if handler is None:
                self._origin_threading_excepthook(args)
                return
            try:
                result = handler(args)
            except Exception:
                self._origin_threading_excepthook(args)
                return
            if result is False:
                self._origin_threading_excepthook(args)

        sys.excepthook = _sys_hook
        threading.excepthook = _thread_hook

        # # 标记：albuswall 已经接管异常钩子
        # sys.stderr.write("[albuswall] exception hooks installed\n")

    def exec(self) -> int:
        # 1) boot：让容器跑 on_boot 钩子（mkdir 等）
        self.container.exec()

        # 2) main loop：唯一的阻塞点
        for fn in self._loop_hooks:
            fn(self.main_loop)
        code = self.main_loop.run()

        # 3) finally：收尾
        self.container.finally_()
        return code


class Application:
    """Interface."""
    try:
        _object = _Application()
    except BaseException as _e:  # noqa: BLE001
        sys.stderr.write(
            "[albuswall][bootstrap] _Application() 构造失败，"
            "Application 将不可用\n"
        )
        traceback.print_exception(type(_e), _e, _e.__traceback__, file=sys.stderr)
        raise

    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __getattr__(self, name):
        # 把未定义的属性访问委托给 _object
        return getattr(self._object, name)

    def __setattr__(self, name, value):
        # 让 `app.handle_exception = fn` 这类赋值真正落到 _object 上
        if name in {"_object", "_instance"}:
            object.__setattr__(self, name, value)
        else:
            setattr(self._object, name, value)

    @property
    def plugins(self) -> PluginManager:
        return self._object.plugins

    @property
    def container(self):
        return self._object.container

    @property
    def config(self):
        return self._object.configure

    @property
    def configue(self):
        return self._object.configure

    @classmethod
    def instance(cls) -> _Application:
        return cls._object

    @classmethod
    def exec(cls) -> int:
        return cls._object.exec()

    @classmethod
    def request_quit(cls, reason: str = "") -> None:
        cls._object.request_quit(reason)

    # ---------- 装饰器 API ----------
    @classmethod
    def on_quit(cls, fn: Callable | None = None):
        """
        两种用法：
            @Application.on_quit
            def cleanup(reason): ...

            @Application.on_quit()
            def cleanup(reason): ...
        """
        if fn is not None:
            cls._object.on_quit(fn)
            return fn

        def deco(f):
            cls._object.on_quit(f)
            return f

        return deco

    @classmethod
    def on_loop(cls, fn: Callable | None = None):
        if fn is not None:
            cls._object.on_loop(fn)
            return fn

        def deco(f):
            cls._object.on_loop(f)
            return f

        return deco
