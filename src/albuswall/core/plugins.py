#
"""插件系统：声明、发现、生命周期。

三层职责，互不越界：

===============  ==========================================================
层               职责
===============  ==========================================================
声明              ``@plugin`` 装饰器把工厂包成 :class:`Plugin`，追加到模块级
                  ``_pending``；发现层只负责 import（副作用即注册）
管理              :class:`PluginManager`（**实例级**）收集插件、拓扑排序、
                  跑三阶段 ``setup`` / ``start`` / ``stop``
上下文            :class:`PluginContext` 是插件唯一需要的 API：记录
                  ``provide`` / ``on`` 的产物，供 setup 失败时回滚
===============  ==========================================================

设计边界（刻意的选择，不是遗漏）：

* **不做权限围墙**：``ctx.container`` / ``ctx.app`` 直接给出真实对象，插件绕
  过 ``ctx.provide`` 直接 ``container.register`` 也允许——代价是回滚时不会被
  撤销（见 ``docs/plugin-author-guide.md``「hook 撤销」一节）。
* **不做 ServiceProxy**、**不做静态急切加载检测**：只在 ``setup_all`` 里量每
  个插件的 setup 耗时，超过 ``setup_slow_threshold_ms`` 打 warning。
* **工厂签名统一**为 ``Callable[[Container], Any]``：永远收容器，不看
  ``_wants_container`` 的形参判断。所以 ``@plugin`` 工厂的形参名无所谓。
* **容器 last-wins**（后注册覆盖先注册）由 :class:`~albuswall.core.container.
  Container` 自己的语义决定，本模块不引入 ``override`` 之类的新参数。

顶层 import 限制：不允许 import ``albuswall.core.application``（运行期会成环）、
``albuswall.log.bootstrap`` / ``albuswall.configue``（会拉入 configue + core）。
发现层在方法体里延迟 import，避免 import 期副作用。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Protocol,
    cast,
    overload,
    runtime_checkable,
)

from .container import Container
from .registry import register, unregister
from .types import HookNames, SimpleHook

if TYPE_CHECKING:
    from .application import CoreApplication

__all__ = [
    "ENTRY_POINT_GROUP",
    "BasePlugin",
    "DuplicatePluginError",
    "MissingPluginError",
    "Plugin",
    "PluginContext",
    "PluginCycleError",
    "PluginError",
    "PluginManager",
    "plugin",
]

logger = logging.getLogger(__package__)

#: 第三方插件在 ``pyproject.toml`` 里声明的 entry point 组名。
ENTRY_POINT_GROUP = "albuswall.plugins"

#: ``@plugin`` 工厂：永远接收依赖容器。
PluginFactory = Callable[[Container], Any]

#: 插件 logger 的命名空间前缀：``albuswall.plugins.<插件名>``。
_PLUGIN_LOGGER_ROOT = (
    f"{(__package__ or 'albuswall.core').rsplit('.', 1)[0]}.plugins"
)


# ===========================================================================
# 异常
# ===========================================================================

class PluginError(Exception):
    """插件系统异常基类。"""


class MissingPluginError(PluginError, KeyError):
    """``requires`` 里声明的插件没有注册。

    同时是 ``KeyError``（键就是缺失的插件名），方便调用方按 dict 语义处理。
    """


class PluginCycleError(PluginError, RuntimeError):
    """插件 ``requires`` 成环，无法拓扑排序。"""


class DuplicatePluginError(PluginError, ValueError):
    """同名插件被 ``add`` 了两次。"""


# ===========================================================================
# 协议与基类
# ===========================================================================

@runtime_checkable
class Plugin(Protocol):
    """插件协议：``name`` / ``version`` / ``requires`` + 三阶段方法。"""

    name: str
    version: str
    requires: tuple[str, ...]

    def setup(self, ctx: PluginContext) -> None:
        """注册依赖与 hook。此时 config 已就绪，logger 还没。"""
        ...

    def start(self) -> None:
        """启动。此时 hook 与 logger 都已就绪。"""
        ...

    def stop(self) -> None:
        """停止。异常会被 ``PluginManager`` 吞掉并记日志。"""
        ...


class BasePlugin:
    """插件基类：全部方法为空的默认实现。

    继承它只是省掉三个空方法；协议式实现（不继承、只提供同名方法）同样被
    :class:`PluginManager` 接受。
    """

    name: str = ""
    version: str = "0.0.0"
    requires: tuple[str, ...] = ()

    def setup(self, ctx: PluginContext) -> None:
        """默认空实现。"""

    def start(self) -> None:
        """默认空实现。"""

    def stop(self) -> None:
        """默认空实现。"""


# ===========================================================================
# 声明：@plugin 装饰器
# ===========================================================================

#: ``@plugin`` 的收集表。装饰时追加，``PluginManager.load_pending()`` 消费。
_pending: list[Plugin] = []


def _drain_pending() -> list[Plugin]:
    """取出并清空待收集表（返回值是快照，调用方可以放心遍历）。"""
    out = list(_pending)
    _pending.clear()
    return out


def _resolve_lifecycle(
        result: Any,
) -> tuple[Callable[[], None] | None, Callable[[], None] | None]:
    """把工厂返回值归一成 ``(start, stop)``。

    支持四种形态：

    * ``None`` —— 无 start / stop；
    * ``(start, stop)``（或只有 ``(start,)``）—— 直接取；
    * 对象带 ``.start`` / ``.stop`` 属性 —— 取方法；
    * 其他 —— 无 start / stop（只 log debug，不报错）。
    """
    if result is None:
        return None, None

    if isinstance(result, tuple):
        items = cast("tuple[Any, ...]", result)
        if len(items) == 2:
            start, stop = items
        elif len(items) == 1:
            start, stop = items[0], None
        else:
            logger.debug(
                "plugin factory returned a %d-tuple, expected (start, stop); "
                "ignoring lifecycle", len(items),
            )
            return None, None
        return _as_callback(start), _as_callback(stop)

    start = getattr(result, "start", None)
    stop = getattr(result, "stop", None)
    if start is None and stop is None:
        logger.debug(
            "plugin factory returned %r without .start/.stop; "
            "no lifecycle to run", type(result).__name__,
        )
        return None, None
    return _as_callback(start), _as_callback(stop)


def _as_callback(value: Any) -> Callable[[], None] | None:
    """callable 原样返回，其余（含 ``None``）当作「没有这个方法」。"""
    if callable(value):
        return value  # type: ignore[no-any-return]
    return None


class _DecoratedPlugin:
    """``@plugin`` 的适配器：把工厂函数包成 :class:`Plugin`。

    工厂在本对象 ``setup`` 时才执行（不是在装饰时），保证「import 只做声明，
    重依赖留到 setup」这条顶层轻量契约成立。
    """

    def __init__(
            self,
            name: str,
            factory: PluginFactory,
            *,
            version: str = "0.0.0",
            requires: tuple[str, ...] = (),
    ) -> None:
        self.name = name
        self.version = version
        self.requires = tuple(requires)
        self._factory = factory
        self._start: Callable[[], None] | None = None
        self._stop: Callable[[], None] | None = None

    def setup(self, ctx: PluginContext) -> None:
        result = self._factory(ctx.container)
        self._start, self._stop = _resolve_lifecycle(result)

    def start(self) -> None:
        if self._start is not None:
            self._start()

    def stop(self) -> None:
        if self._stop is not None:
            self._stop()

    def __repr__(self) -> str:
        factory = getattr(self._factory, "__qualname__", repr(self._factory))
        return f"<_DecoratedPlugin {self.name!r} factory={factory}>"


# noinspection overloads
@overload
def plugin(fn: PluginFactory, /) -> PluginFactory: ...


# noinspection overloads
@overload
def plugin(
        name: str,
        /,
        *,
        version: str = "0.0.0",
        requires: tuple[str, ...] = (),
) -> Callable[[PluginFactory], PluginFactory]: ...


def plugin(
        arg0: str | PluginFactory,
        *,
        version: str = "0.0.0",
        requires: tuple[str, ...] = (),
) -> Callable[[PluginFactory], PluginFactory] | PluginFactory:
    """声明插件：``@plugin`` / ``@plugin("name")``。

    两种形态：

    .. code-block:: python

        @plugin                       # 裸用：名字取函数名
        def my_plugin(container):
            ...

        @plugin("my-plugin", version="1.2.0", requires=("base",))
        def my_plugin(container):
            ...

    装饰**返回原函数**（保持可调用，方便直接 import 后调用），真正的注册动作
    是「把 :class:`_DecoratedPlugin` 追加到 ``_pending``」。所以发现层只需要
    import 模块，由 ``PluginManager.load_pending()`` 统一收编。

    Args:
        arg0: 插件名（带名形态）或工厂函数（裸用形态）。
        version: 版本号，仅用于展示 / 排障。
        requires: 依赖的插件名；未注册会在 ``setup_all`` 的拓扑排序阶段报错。

    Raises:
        TypeError: ``arg0`` 既不是 ``str`` 也不是 callable。
    """
    if isinstance(arg0, str):
        name = arg0

        def decorator(fn: PluginFactory) -> PluginFactory:
            _pending.append(_DecoratedPlugin(
                name, fn, version=version, requires=tuple(requires),
            ))
            return fn

        return decorator

    if callable(arg0):
        fn = arg0
        _pending.append(_DecoratedPlugin(
            getattr(fn, "__name__", "<anonymous>"),
            fn,
            version=version,
            requires=tuple(requires),
        ))
        return fn

    raise TypeError(f"unsupported plugin() argument: {arg0!r}")


# ===========================================================================
# 上下文
# ===========================================================================

@dataclass
class PluginContext:
    """交给插件的上下文：最小 API，不做权限围墙。

    字段 ``container`` / ``app`` 是**真实对象**，插件可以直接操作；``provide``
    / ``on`` 只是「顺手记一笔」，让 ``PluginManager`` 在 setup 失败时能撤销这
    个插件自己新增的东西。

    Attributes:
        name: 本插件名。
        container: 依赖容器（``app.container``）。
        app: 应用实例（:class:`CoreApplication`）。
    """

    name: str
    container: Container
    app: CoreApplication
    _registered_services: list[str] = field(default_factory=list[str])
    _registered_hooks: list[tuple[str, Callable[..., Any]]] = field(
        default_factory=list[tuple[str, Callable[..., Any]]],
    )
    _slot_candidates: list[tuple[str, str]] = field(default_factory=list)

    # ---------------- 注册 ----------------
    def provide(
            self,
            name: str,
            factory: Any,
            *,
            singleton: bool = True,
            returns: Any = None,
    ) -> None:
        """向容器注册一个依赖，并记为「本插件新增」。

        工厂签名统一为 ``Callable[[Container], Any]``——永远收容器。零参工厂
        同样合法（``Container`` 的 ``_wants_container`` 不注入它）。
        """
        self.container.register(name, factory, singleton=singleton, returns=returns)
        if name not in self._registered_services:
            self._registered_services.append(name)

    def on(self, event: str) -> Callable[[SimpleHook], SimpleHook]:
        """注册 hook 的装饰器形态。

        ``event`` 为 ``"loop"`` 时自动把零参回调包成 ``hook(app)``：插件作者
        不需要写 ``def fn(app)``，要 app 就 ``ctx.app``。

        .. code-block:: python

            @ctx.on("init")
            def init() -> None:
                ...

            @ctx.on("loop")          # 仍然是零参
            def loop() -> None:
                ...
        """

        def decorator(fn: SimpleHook) -> SimpleHook:
            self.register(event, _wrap_hook(event, fn))
            return fn

        return decorator

    def register(
            self,
            event: str,
            hook: Callable[..., Any],
    ) -> None:
        """直接注册一个 hook（已经是 hook 的最终形态，不再包装）。"""
        register(_as_hook_name(event), hook)  # type: ignore[arg-type]
        self._registered_hooks.append((event, hook))

    # ---------------- 取用 ----------------
    def require(self, name: str) -> Any:
        """强依赖获取，未注册抛 ``MissingDependencyError``。"""
        return self.container.require(name)

    def get(self, name: str, default: Any = None) -> Any:
        """宽松获取，未注册返回 ``default``。"""
        return self.container.get(name, default)

    @property
    def log(self) -> logging.Logger:
        """本插件的 logger（命名空间 ``albuswall.plugins.<name>``）。"""
        return logging.getLogger(f"{_PLUGIN_LOGGER_ROOT}.{self.name}")

    # ---------------- 回滚 ----------------
    def _rollback(self) -> None:
        """撤销本插件通过 ``provide`` / ``on`` / ``register`` 新增的东西。

        仅供 :class:`PluginManager` 在 setup 失败时调用；幂等（记录会被消费）。
        **只删自己新增的**：已存在的同名依赖被覆盖后不会「复原」，容器
        last-wins 语义也不追踪已解析引用。
        """
        while self._registered_hooks:
            event, hook = self._registered_hooks.pop()
            try:
                unregister(_as_hook_name(event), hook)  # type: ignore[arg-type]
            except Exception:  # noinspection PyBroadException
                logger.exception(
                    "plugin %r failed to unregister hook for %r", self.name, event)

        while self._registered_services:
            name = self._registered_services.pop()
            try:
                self.container.unregister(name)
            except Exception:  # noinspection PyBroadException
                logger.exception(
                    "plugin %r failed to unregister dependency %r", self.name, name)


        while self._slot_candidates:
            slot_name, cand = self._slot_candidates.pop()
            try:
                self.app.slots.get(slot_name).revoke(cand)
            except Exception:
                logger.exception(
                    "plugin %r failed to revoke slot %r/%r",
                    self.name, slot_name, cand)

    def provide_slot(
            self,
            slot_name: str,
            name: str,
            factory: Callable[[Container], Any],
            *,
            priority: int = 0,
    ) -> None:
        """向应用登记一个槽位候选。

        ``host_only`` 槽位会抛 ``SlotHostOnlyError`` —— 这是给插件作者
        的明确边界，不是遗漏。
        """
        self.app.provide_slot(
            slot_name, name, factory,
            priority=priority, source=f"plugin:{self.name}")
        self._slot_candidates.append((slot_name, name))


def _wrap_hook(event: str, fn: SimpleHook) -> Callable[..., Any]:
    """``loop`` hook 是 ``hook(app)``，其余是零参；``on`` 负责抹平差异。"""
    if event != "loop":
        return fn

    def wrapped(_app: object) -> None:
        fn()

    return wrapped


def _as_hook_name(event: str) -> HookNames:
    """把 ``str`` 收窄成 :data:`~albuswall.core.types.HookNames`。

    不做白名单校验：``hooks_map`` 是 dict，未知事件名会以 ``KeyError`` 暴露，
    这比在这里维护第二份事件列表更不容易漂移。
    """
    return event  # type: ignore[return-value]


# ===========================================================================
# 管理器（实例级）
# ===========================================================================

class PluginManager:
    """插件管理器：收集 → 拓扑排序 → ``setup`` / ``start`` / ``stop``。

    实例级（挂在 ``app.plugins`` 上），不是全局单例。

    三阶段，失败语义各不相同：

    * ``setup_all`` 失败：撤销**本插件**的容器注册 + hooks，逆序停掉已 setup
      的插件，然后**原样抛出**；
    * ``start_all`` 失败：``_started`` 里已经记下该插件，异常**直接传播**，
      由主机侧的 ``finally_`` → ``stop_all`` 收拾残局；
    * ``stop_all`` 失败：**吞掉**并 ``logger.exception``，继续停下一个。
    """

    def __init__(self, app: CoreApplication) -> None:
        self._app = app
        self._declare: dict[str, Plugin] = {}
        self._order: list[str] = []
        self._started: list[str] = []
        self._contexts: dict[str, PluginContext] = {}
        self._setup_done = False
        self._start_done = False
        #: setup 耗时超过该值（毫秒）打 warning；实例属性，可随时改。
        self.setup_slow_threshold_ms: float = 200.0

    # ---------------- 状态 ----------------
    @property
    def setup_done(self) -> bool:
        """是否已经跑过 ``setup_all``。"""
        return self._setup_done

    @property
    def started(self) -> bool:
        """是否已经跑完 ``start_all``（哪怕一个插件都没 start 也算）。"""
        return self._start_done

    # ---------------- 收集 ----------------
    def add(self, plugin: Plugin) -> None:
        """登记一个插件。

        只检查「名字非空」和「不重名」；``requires`` 是否可满足留给
        :meth:`_topo_order`（那时才知道全集，且能区分「缺失」与「成环」）。
        """
        name = getattr(plugin, "name", None)
        if not name:
            raise PluginError(f"plugin {plugin!r} has an empty name")
        if name in self._declare:
            raise DuplicatePluginError(f"plugin {name!r} is already registered")
        self._declare[name] = plugin
        logger.debug("plugin %r collected", name)

    def extend(self, plugins: Iterable[Plugin]) -> None:
        """批量登记（遇到重名照常抛 ``DuplicatePluginError``）。"""
        for plugin_ in plugins:
            self.add(plugin_)

    def get(self, name: str) -> Plugin:
        """按名字取插件；未注册抛 ``KeyError``。"""
        return self._declare[name]

    def __contains__(self, name: object) -> bool:
        return name in self._declare

    def __iter__(self) -> Iterator[str]:
        """按**声明顺序**迭代插件名（不是拓扑序）。"""
        return iter(self._declare)

    def __len__(self) -> int:
        return len(self._declare)

    # ---------------- 发现层入口 ----------------
    def load_pending(self) -> int:
        """把模块级 ``_pending`` 收进管理器，返回新收了几个。

        ``_pending`` 会被清空（``_drain_pending``），所以重复调用不会重复收。
        """
        added = 0
        for plugin_ in _drain_pending():
            self.add(plugin_)
            added += 1
        if added:
            logger.debug("collected %d plugin(s) from @plugin declarations", added)
        return added

    def load_builtin(self, package: str) -> int:
        """import 第一方插件包并收编其中的 ``@plugin``。"""
        from albuswall.plugin.discovery import discover_package

        discover_package(package)
        return self.load_pending()

    def load_entry_points(self, group: str = ENTRY_POINT_GROUP) -> int:
        """加载第三方 ``entry_points`` 声明的插件。"""
        from albuswall.plugin.discovery import discover_entry_points

        discover_entry_points(group)
        return self.load_pending()

    def load_directory(self, path: Path | str) -> int:
        """加载用户插件目录下的 ``.py``。"""
        from albuswall.plugin.discovery import discover_directory

        discover_directory(Path(path))
        return self.load_pending()

    # ---------------- 生命周期 ----------------
    def setup_all(
            self,
            enabled: Iterable[str] | None = None,
            disabled: Iterable[str] | None = None,
    ) -> None:
        """拓扑序 ``setup``；失败回滚后原样抛出。

        Args:
            enabled: 白名单。名单里有未注册的名字 -> ``KeyError``。
            disabled: 黑名单。名单里有未注册的名字只记 debug。
                两者互斥，同时给 -> ``ValueError``。``dict`` 按 keys 处理。

        Raises:
            PluginError: 已经 setup 过（重复调用）。
            ValueError: ``enabled`` 与 ``disabled`` 同时给出。
            KeyError: ``enabled`` 里有未注册的名字。
            MissingPluginError: 某个插件 ``requires`` 了未注册的插件。
            PluginCycleError: ``requires`` 成环。
            Exception: 插件 ``setup`` 自己抛的异常（原样传播）。
        """
        if self._setup_done:
            raise PluginError("plugins are already set up")
        if enabled is not None and disabled is not None:
            raise ValueError("enabled and disabled cannot both be specified")

        allow = self._resolve_allow(enabled, disabled)
        self._order = self._topo_order()

        attempted: list[str] = []
        try:
            for name in self._order:
                if allow is not None and name not in allow:
                    logger.debug("plugin %r skipped", name)
                    continue
                plugin_ = self._declare[name]
                for dep in tuple(getattr(plugin_, "requires", ()) or ()):
                    if dep not in self._contexts:
                        logger.warning(
                            "plugin %r requires %r, which is not set up "
                            "(filtered out by enabled/disabled); "
                            "continuing anyway", name, dep,
                        )
                ctx = PluginContext(
                    name=name,
                    container=self._app.container,
                    app=self._app,
                )
                self._contexts[name] = ctx
                attempted.append(name)

                started_at = time.perf_counter()
                try:
                    plugin_.setup(ctx)
                except Exception:
                    logger.exception("plugin %r failed during setup", name)
                    # 失败者自己也要撤销，但不调它的 stop（从没 start 过）
                    attempted.pop()
                    self._contexts.pop(name, None)
                    ctx._rollback()
                    raise
                elapsed_ms = (time.perf_counter() - started_at) * 1000.0
                if elapsed_ms > self.setup_slow_threshold_ms:
                    logger.warning(
                        "plugin %r setup took %.1f ms (threshold %.0f ms); "
                        "move heavy imports into the factory body or lazily",
                        name, elapsed_ms, self.setup_slow_threshold_ms,
                    )
                else:
                    logger.debug("plugin %r set up in %.1f ms", name, elapsed_ms)
        except BaseException:
            self._rollback_setup(attempted)
            raise

        self._setup_done = True

    def start_all(self) -> None:
        """按拓扑序 ``start``；异常原样传播（由 ``stop_all`` 收尾）。

        Raises:
            PluginError: 还没 ``setup_all`` 就调了本方法。
            Exception: 插件 ``start`` 自己抛的异常（原样传播）。
        """
        if not self._setup_done:
            raise PluginError("plugins must be set up before start_all()")

        for name in self._order:
            if name not in self._contexts:
                continue  # setup 阶段被 enabled/disabled 过滤掉
            plugin_ = self._declare[name]
            logger.debug("starting plugin %r", name)
            # 先登记再启动：start 抛异常时，stop_all 仍然会清理它
            self._started.append(name)
            plugin_.start()
        self._start_done = True

    def stop_all(self) -> None:
        """逆序 ``stop``；每个独立 try/except，绝不向外抛。"""
        for name in reversed(list(self._started)):
            self._safe_stop(name)
        self._started.clear()

    # ---------------- 内部 ----------------
    def _topo_order(self) -> list[str]:
        """按 ``requires`` 拓扑排序；依赖优先，同层保持声明顺序。

        Raises:
            MissingPluginError: ``requires`` 里出现未注册的名字。
            PluginCycleError: 依赖成环。
        """
        ordered: list[str] = []
        state: dict[str, int] = {}  # 0 = 访问中，1 = 已完成

        def visit(name: str, stack: list[str]) -> None:
            mark = state.get(name)
            if mark == 1:
                return
            if mark == 0:
                cycle = " -> ".join([*stack, name])
                raise PluginCycleError(
                    f"plugin dependency cycle detected: {cycle}")
            plugin_ = self._declare.get(name)
            if plugin_ is None:
                raise MissingPluginError(
                    f"required plugin {name!r} is not registered "
                    f"(required by {stack[-1]!r})" if stack
                    else f"required plugin {name!r} is not registered"
                )
            state[name] = 0
            stack.append(name)
            for dep in tuple(getattr(plugin_, "requires", ()) or ()):
                if dep not in self._declare:
                    raise MissingPluginError(
                        f"plugin {name!r} requires unknown plugin {dep!r}")
                visit(dep, stack)
            stack.pop()
            state[name] = 1
            ordered.append(name)

        for name in self._declare:
            visit(name, [])
        return ordered

    def _resolve_allow(
            self,
            enabled: Iterable[str] | None,
            disabled: Iterable[str] | None,
    ) -> set[str] | None:
        """把启用/禁用名单解析成允许集合（``None`` 表示全开）。

        语义照抄旧 ``plugin/manager.py``：白名单里的未知名报错（写错名字说明
        配置有 bug），黑名单里的未知名只 debug（插件卸掉后残留配置是常态）。
        ``set(dict)`` 自动取 keys，所以直接传配置段视图也可以。
        """
        declared = self._declare.keys()

        if enabled is not None:
            allow = set(enabled)
            unknown = allow - declared
            if unknown:
                raise KeyError(
                    f"unknown plugins in enabled list: {sorted(unknown)}")
            return allow

        if disabled is not None:
            deny = set(disabled)
            unknown = deny - declared
            if unknown:
                logger.debug(
                    "disabled list contains unknown plugins, ignored: %s",
                    sorted(unknown),
                )
            return set(declared) - deny

        return None

    def _rollback_setup(self, attempted: list[str]) -> None:
        """setup 失败后的统一收尾：逆序 stop + 撤销注册，然后清空状态。"""
        for name in reversed(attempted):
            self._safe_stop(name)
            ctx = self._contexts.pop(name, None)
            if ctx is not None:
                ctx._rollback()
        self._setup_done = False

    def _safe_stop(self, name: str) -> None:
        """调 ``stop`` 并吞掉异常（stop 阶段不允许把进程弄崩）。"""
        plugin_ = self._declare.get(name)
        try:
            if plugin_ is not None:
                plugin_.stop()
        except Exception:  # noinspection PyBroadException
            logger.exception("plugin %r failed during stop", name)
        finally:
            if name in self._started:
                self._started.remove(name)

    # ---------------- 调试 ----------------
    def __repr__(self) -> str:
        state = "setup" if self._setup_done else "declared"
        return (
            f"<PluginManager [{', '.join(self._declare)}] "
            f"{state}, started={len(self._started)}>"
        )
