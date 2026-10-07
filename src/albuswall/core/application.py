#
""""""

import sys
import threading
import traceback
from typing import TYPE_CHECKING, Any, Callable, Optional

# ============================================================
# 引导期异常兜底
# ============================================================
# 目的：从 import 那一刻起，保证未捕获异常一定写到 stderr，
#       即使后面 _Application 构造失败也不会静默丢失。
# 注意：必须放在任何可能失败的 import 之前。

_origin_sys_excepthook = sys.excepthook
_origin_threading_excepthook = threading.excepthook


# noinspection broad-exception
def _bootstrap_sys_hook(exc_type, exc_value, exc_tb):
    try:
        sys.stderr.write("[albuswall][bootstrap] uncaught exception:\n")
        traceback.print_exception(exc_type, exc_value, exc_tb, file=sys.stderr)
    except Exception:  # noinspection PyBroadException
        pass


# noinspection broad-exception
def _bootstrap_thread_hook(args):
    try:
        sys.stderr.write("[albuswall][bootstrap] uncaught thread exception:\n")
        traceback.print_exception(
            args.exc_type, args.exc_value, args.exc_traceback, file=sys.stderr
        )
    except Exception:  # noinspection PyBroadException
        pass


sys.excepthook = _bootstrap_sys_hook
threading.excepthook = _bootstrap_thread_hook

# ============================================================
# 正常导入
# ============================================================

from .container import Container
from .main_loop import MainLoop, HeadlessMainLoop
from albuswall.plugin.manager import PluginManager

if TYPE_CHECKING:
    from albuswall.configue import Configue

# ---------- 类型别名 ----------
# 异常 handler 约定：返回 False 表示"我处理不了，请继续往下传"
ExceptionHandler = Callable[
    [type[BaseException], BaseException, Any],
    Optional[bool],
]
ThreadExceptionHandler = Callable[
    [threading.ExceptHookArgs],
    Optional[bool],
]

# 实际传给 _boot_fn 的是 _Application 实例；__main__ 侧形参可写
# Application（门面）也可写 _Application，这里统一放宽为 Any。
BootFn = Callable[[Any], None]
LifecycleHook = Callable[[], None]  # on_boot / on_final
QuitHook = Callable[[str], None]  # on_quit
LoopHook = Callable[[MainLoop], None]  # on_loop


class LazyConfigue:
    """延迟构造 Configue，避开 import 循环。

    非数据描述符：一旦实例 __dict__ 里有了同名条目，就不再触发。
    """

    def __set_name__(self, owner, name):
        self.name = name

    def __get__(self, obj, obj_type=None):
        if obj is None:
            return self
        if self.name not in obj.__dict__:
            from albuswall.configue import Configue  # 延迟导入，避免循环依赖
            obj.__dict__[self.name] = Configue()
        return obj.__dict__[self.name]


# noinspection broad-exception,simplify-boolean-check
class _Application:
    """生命周期编排者。

    exec() 走五个 phase：

        boot     -- _boot_fn 把所有东西 register 进容器
        setup    -- 确保 configure 就绪，建数据目录，抓 UI 的 main_loop
        wire     -- 跑 _boot_hooks（连信号、启动服务）
        run      -- 跑 _loop_hooks + main_loop.run()
        teardown -- 跑 _final_hooks

    四类钩子全部挂在 Application 上，Container 只做注册表。
    组件（UI / service）只暴露方法，不需要知道自己是第几个被调的。
    """

    configure: "Configue" = LazyConfigue()  # type: ignore

    def __init__(self) -> None:
        self.container = Container()
        self.plugins = PluginManager()

        self._main_loop: Optional[MainLoop] = None
        self._ui: Any = None
        self._boot_fn: Optional[BootFn] = None

        # 四类钩子，按注册顺序执行
        self._boot_hooks: list[LifecycleHook] = []
        self._final_hooks: list[LifecycleHook] = []
        self._quit_hooks: list[QuitHook] = []
        self._loop_hooks: list[LoopHook] = []

        self.log_enable = False

        # 记住最初的系统 hook，用于二次委托
        self._origin_sys_excepthook = _origin_sys_excepthook
        self._origin_threading_excepthook = _origin_threading_excepthook

        # 内部真实 handler（property 对外暴露，可随时替换）
        self._handle_exception: ExceptionHandler = lambda *a: False
        self._thread_exception: ThreadExceptionHandler = lambda *a: False

        # 安装 wrapper：每次都从 self 读最新 handler
        self._install_hooks()

    # ============================================================
    # 异常 handler（可随时替换）
    # ============================================================
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

    # ---------- 二次委托 ----------
    def call_origin_exception(
            self,
            exc_type: type[BaseException],
            exc_value: BaseException,
            exc_tb: Any,
    ) -> None:
        """把异常二次交给系统原始的 excepthook（默认打印到 stderr）。"""
        self._origin_sys_excepthook(exc_type, exc_value, exc_tb)

    def call_origin_thread_exception(self, args: threading.ExceptHookArgs) -> None:
        """把线程异常二次交给系统原始的 threading.excepthook。"""
        self._origin_threading_excepthook(args)

    def _install_hooks(self) -> None:
        """安装稳定的 wrapper。每次调用时从 self 读当前 handler，
        因此替换 handle_exception / thread_exception 不需要重装。"""

        def _sys_hook(exc_type, exc_value, exc_tb):
            handler = self._handle_exception
            try:
                result = handler(exc_type, exc_value, exc_tb)
            except Exception:  # noinspection PyBroadException
                # handler 自己崩了，二次交给系统原始 hook
                self._origin_sys_excepthook(exc_type, exc_value, exc_tb)
                return
            # handler 显式返回 False → 二次委托
            if result is False:
                self._origin_sys_excepthook(exc_type, exc_value, exc_tb)

        def _thread_hook(args):
            handler = self._thread_exception
            try:
                result = handler(args)
            except Exception:  # noinspection PyBroadException
                self._origin_threading_excepthook(args)
                return
            if result is False:
                self._origin_threading_excepthook(args)

        sys.excepthook = _sys_hook
        threading.excepthook = _thread_hook

    # ============================================================
    # main_loop
    # ============================================================
    @property
    def main_loop(self) -> MainLoop:
        """未设置时惰性回退 HeadlessMainLoop（无 UI 也能跑）。"""
        loop = self._main_loop
        if loop is None:
            loop = HeadlessMainLoop()
            self._main_loop = loop
        return loop

    @main_loop.setter
    def main_loop(self, loop: MainLoop) -> None:
        existing = self._main_loop
        if existing is not None and existing is not loop:
            msg = (
                f"Replacing existing main loop "
                f"{type(existing).__name__} with "
                f"{type(loop).__name__}"
            )
            raise RuntimeError(msg)
        self._main_loop = loop

    # ============================================================
    # 钩子注册
    # ============================================================
    def on_boot(self, fn: LifecycleHook) -> LifecycleHook:
        """装饰器/函数：注册进入主循环前的钩子。先注册先执行。"""
        self._boot_hooks.append(fn)
        return fn

    def on_final(self, fn: LifecycleHook) -> LifecycleHook:
        """装饰器/函数：注册退出时的钩子。先注册先执行。

        由于注册顺序 = 执行顺序，写 `on_final` 时请按"先注册的先拆"排布，
        即：后注册的先被调用的依赖，应该后注册。
        """
        self._final_hooks.append(fn)
        return fn

    def on_quit(self, fn: QuitHook) -> QuitHook:
        """装饰器/函数：注册退出钩子（主循环之前）。先注册先执行。"""
        self._quit_hooks.append(fn)
        return fn

    def on_loop(self, fn: LoopHook) -> LoopHook:
        """装饰器/函数：注册进入主循环前的钩子（可拿到 main_loop）。先注册先执行。"""
        self._loop_hooks.append(fn)
        return fn

    # ============================================================
    # 主动退出
    # ============================================================
    def request_quit(self, reason: str = "") -> None:
        for fn in self._quit_hooks:
            try:
                fn(reason)
            except Exception:  # noinspection PyBroadException
                traceback.print_exc()
        loop = self._main_loop
        if loop is None:
            return
        try:
            loop.quit()
        except Exception:  # noinspection PyBroadException
            traceback.print_exc()

    # ============================================================
    # 生命周期编排
    # ============================================================
    def boot(self, fn: BootFn) -> None:
        """由 __main__ 提供：把所有东西 register 进 container。"""
        self._boot_fn = fn

    def exec(self) -> int | str:
        self._phase_boot()
        try:
            self._phase_setup()
            self._phase_wire()
            return self._phase_run()
        finally:
            self._phase_teardown()

    # ---------- phase 1: boot ----------
    def _phase_boot(self) -> None:
        """跑 __main__ 提供的注册函数。此时容器只有注册，无实例。"""
        if self._boot_fn is None:
            return
        self._boot_fn(self)

    # ---------- phase 2: setup ----------
    def _phase_setup(self) -> None:
        """构造关键单例：configure 就绪 → 建数据目录 → 抓 main_loop。"""
        # ① 强制访问一次，确保 LazyConfigue 完成构造
        configure = self.configure

        # ② 数据目录（所有 service 都不需要自己建）
        data_dir = configure.static.path.data
        data_dir.mkdir(parents=True, exist_ok=True)

        # ③ UI 可选：拿到就抓 main_loop
        #    （container.get(name, default) 语义：未注册时返回 default）
        self._ui = self.container.get("ui", None)
        if self._ui is not None:
            loop = getattr(self._ui, "main_loop", None)
            if loop is not None:
                # 直接写私有属性，绕过 setter 的"只能设一次"约束
                self._main_loop = loop

    # ---------- phase 3: wire ----------
    def _phase_wire(self) -> None:
        """跑 on_boot 钩子。某个钩子失败不阻断其余钩子。"""
        for fn in self._boot_hooks:
            try:
                fn()
            except Exception:  # noinspection PyBroadException
                traceback.print_exc()

    # ---------- phase 4: run ----------
    def _phase_run(self) -> int | str:
        loop = self.main_loop
        for fn in self._loop_hooks:
            try:
                fn(loop)
            except Exception:  # noinspection PyBroadException
                traceback.print_exc()
        return loop.run()

    # ---------- phase 5: teardown ----------
    def _phase_teardown(self) -> None:
        """跑 on_final 钩子。某个钩子失败不阻断其余钩子。"""
        for fn in self._final_hooks:
            try:
                fn()
            except Exception:  # noinspection PyBroadException
                traceback.print_exc()


class Application:
    """单例门面。

    - import 时立即构造 _Application（安装异常钩子），
    - 所有属性访问委托给它，
    - classmethod 暴露常用入口。
    """

    _instance = None

    try:
        _object = _Application()
    except BaseException as _e:  # noqa: BLE001
        sys.stderr.write(
            "[albuswall][bootstrap] _Application() 构造失败，"
            "Application 将不可用\n"
        )
        traceback.print_exception(
            type(_e), _e, _e.__traceback__, file=sys.stderr
        )
        raise

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

    # ============================================================
    # 快捷属性
    # ============================================================
    @property
    def plugins(self) -> PluginManager:
        return self._object.plugins

    @property
    def container(self) -> Container:
        return self._object.container

    @property
    def config(self):
        return self._object.configure

    @property
    def configue(self):
        return self._object.configure

    # ============================================================
    # classmethod 门面
    # ============================================================
    @classmethod
    def instance(cls) -> _Application:
        return cls._object

    @classmethod
    def boot(cls, fn: BootFn) -> None:
        cls._object.boot(fn)

    @classmethod
    def exec(cls) -> int | str:
        return cls._object.exec()

    @classmethod
    def request_quit(cls, reason: str = "") -> None:
        cls._object.request_quit(reason)

    # ============================================================
    # 装饰器 API（都支持 @X 和 @X() 两种用法）
    # ============================================================
    @classmethod
    def on_boot(cls, fn: LifecycleHook | None = None):
        """
            @Application.on_boot
            def wire(): ...

            @Application.on_boot()
            def wire(): ...
        """
        if fn is not None:
            cls._object.on_boot(fn)
            return fn

        def deco(f):
            cls._object.on_boot(f)
            return f

        return deco

    @classmethod
    def on_final(cls, fn: LifecycleHook | None = None):
        if fn is not None:
            cls._object.on_final(fn)
            return fn

        def deco(f):
            cls._object.on_final(f)
            return f

        return deco

    @classmethod
    def on_quit(cls, fn: QuitHook | None = None):
        if fn is not None:
            cls._object.on_quit(fn)
            return fn

        def deco(f):
            cls._object.on_quit(f)
            return f

        return deco

    @classmethod
    def on_loop(cls, fn: LoopHook | None = None):
        if fn is not None:
            cls._object.on_loop(fn)
            return fn

        def deco(f):
            cls._object.on_loop(f)
            return f

        return deco
