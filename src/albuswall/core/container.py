#
""""""

from __future__ import annotations

import inspect
import threading
from collections.abc import Callable, Iterator
from pprint import pformat
from typing import Any

from albuswall.core.errors import MissingDependencyError
from albuswall.utils.format import factory_repr

__all__ = ["Container"]

_UNSET: Any = object()


def _wants_container(factory: Callable[..., Any]) -> bool:
    """判断工厂是否要注入 container。

    * ``type``（类）：永远零参构造，不注入。
    * 其他 callable：只要存在必填位置参数，就注入 ``container``。
    """
    if isinstance(factory, type):
        return False
    try:
        sig = inspect.signature(factory)
    except (TypeError, ValueError):
        return False
    for param in sig.parameters.values():
        if param.kind is inspect.Parameter.VAR_POSITIONAL:
            return True
        if (
                param.kind in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
                and param.default is inspect.Parameter.empty
        ):
            return True
    return False


class Container:
    """轻量依赖注入容器。

    两种注册方式：

    1. 装饰器（推荐）::

        @container.register("config", returns=Config)
        def make_config(c: Container) -> Config:
            return Config(c.require("path"))

        @container.register("logger", singleton=True, returns=Logger)
        def make_logger(c: Container) -> Logger:
            return build_logger(c.require("config"))

    2. 直接调用::

        container.register("config", make_config, returns=Config)
        container.register("service", Service, singleton=False)
        container.set("answer", 42)
        container["answer"] = 42
        container("answer", 42)

    调试::

        print(container)      # pformat：factories(含 returns/singleton) + instances
        repr(container)       # 简洁：<Container [a, b, c]>
        container.annotate("svc", returns=Service)   # 事后补元数据，不触发构造

    获取（**注意 get / require 的语义差异**）::

        container.get("x")              # dict 语义：未注册 -> None
        container.get("x", default)     # dict 语义：未注册 -> default
        container.require("x")          # 未注册 -> MissingDependencyError
        container["x"]                  # 同 require（缺键报错，符合 dict []）
        container("x")                  # 同 require

    语义约定：

    * ``callable``（函数、类、带 ``__call__`` 的实例）视为**工厂**：
      - 类：零参构造
      - 其他 callable：若存在必填位置参数，调用时注入 ``self``
    * 非 callable 视为**常量值**：第一次 ``require`` 即返回，等价单例。
    * ``singleton=True``（默认）时缓存工厂结果；``False`` 时每次重新构造。
    * ``returns`` 仅用于调试展示（见 ``str(container)``），不参与解析。
    """

    def __init__(self) -> None:
        self._factories: dict[str, Callable[..., Any]] = {}
        self._singleton_names: set[str] = set()
        self._singleton_cache: dict[str, Any] = {}
        self._returns: dict[str, Any] = {}  # 返回类型/元数据（仅调试用）
        self._lock = threading.RLock()

    # ---------------- 注册 ----------------
    def register(
            self,
            name: str,
            factory: Any = _UNSET,
            *,
            singleton: bool = True,
            returns: Any = None,
    ) -> Any:
        """注册依赖。无 ``factory`` 时返回装饰器。

        参数
        ----
        name : str
            依赖名。
        factory : callable | Any, optional
            工厂或常量。省略时返回装饰器。
        singleton : bool, default True
            是否缓存工厂结果。
        returns : Any, optional
            返回类型/元数据提示。仅用于 ``str(container)`` 展示，
            不参与依赖解析。
        """
        if factory is _UNSET:
            def decorator(fn: Any) -> Any:
                self._put(name, fn, singleton=singleton, returns=returns)
                return fn

            return decorator
        self._put(name, factory, singleton=singleton, returns=returns)
        return None

    def set(self, name: str, value: Any) -> None:
        """注册常量值（强制单例）。"""
        with self._lock:
            self._factories[name] = lambda _c, v=value: v
            self._singleton_names.add(name)
            self._singleton_cache[name] = value
            self._returns[name] = type(value)

    def annotate(self, name: str, returns: Any = None) -> None:
        """补充/修正返回类型元数据，不触发构造。

        传 ``returns=None`` 表示清除该条目已有的元数据。
        未注册的 name 抛 ``MissingDependencyError``。
        """
        with self._lock:
            if name not in self._factories:
                raise MissingDependencyError(name)
            if returns is None:
                self._returns.pop(name, None)
            else:
                self._returns[name] = returns

    def _put(
            self,
            name: str,
            obj: Any,
            *,
            singleton: bool,
            returns: Any = None,
    ) -> None:
        with self._lock:
            self._singleton_cache.pop(name, None)  # 旧缓存失效
            self._returns.pop(name, None)
            if callable(obj):
                self._factories[name] = obj
                if singleton:
                    self._singleton_names.add(name)
                else:
                    self._singleton_names.discard(name)
                if returns is not None:
                    self._returns[name] = returns
            else:
                self._factories[name] = lambda _c, v=obj: v
                self._singleton_names.add(name)
                self._singleton_cache[name] = obj
                # 常量：没有显式 returns 时自动记录其类型
                self._returns[name] = returns if returns is not None else type(obj)

    def unregister(self, name: str) -> None:
        with self._lock:
            self._factories.pop(name, None)
            self._singleton_names.discard(name)
            self._singleton_cache.pop(name, None)
            self._returns.pop(name, None)

    # ---------------- 获取 ----------------
    def _invoke(self, factory: Callable[..., Any]) -> Any:
        """调用工厂：按签名决定是否注入 ``container``。"""
        if _wants_container(factory):
            return factory(self)
        return factory()

    def _resolve(self, name: str) -> Any:
        """内部统一解析：缓存 → 工厂。未注册抛 MissingDependencyError。"""
        with self._lock:
            if name in self._singleton_cache:
                return self._singleton_cache[name]
            factory = self._factories.get(name, _UNSET)
            if factory is _UNSET:
                raise MissingDependencyError(name)
            value = self._invoke(factory)
            if name in self._singleton_names:
                self._singleton_cache[name] = value
            return value

    def get(self, name: str, default: Any = None) -> Any:
        """``dict.get`` 语义：未注册返回 ``default``（默认 ``None``）。"""
        try:
            return self._resolve(name)
        except MissingDependencyError:
            return default

    def require(self, name: str) -> Any:
        """强依赖获取：未注册抛 ``MissingDependencyError``。"""
        return self._resolve(name)

    def has_instance(self, name):
        if name in self._singleton_cache:
            return True
        return False

    # ---------------- 语法糖 ----------------
    def __call__(self, name: str, value: Any = _UNSET) -> Any:
        """``container(name)`` -> require；``container(name, value)`` -> 注册值。"""
        if value is _UNSET:
            return self.require(name)
        self.set(name, value)
        return None

    def __getitem__(self, name: str) -> Any:
        return self.require(name)

    def __setitem__(self, name: str, value: Any) -> None:
        self.set(name, value)

    def __contains__(self, name: object) -> bool:
        with self._lock:
            return name in self._factories

    def __iter__(self) -> Iterator[str]:
        with self._lock:
            return iter(list(self._factories))

    def __len__(self) -> int:
        with self._lock:
            return len(self._factories)

    def keys(self) -> list[str]:
        with self._lock:
            return list(self._factories)

    # ---------------- 生命周期 ----------------
    def clear(self) -> None:
        with self._lock:
            self._factories.clear()
            self._singleton_names.clear()
            self._singleton_cache.clear()
            self._returns.clear()

    # ---------------- 调试 ----------------
    def __repr__(self) -> str:
        with self._lock:
            names = ", ".join(sorted(self._factories))
        return f"<Container [{names}]>"

    def __str__(self) -> str:
        """详细的调试视图：工厂（含 returns/singleton 标记）与已实例化对象。"""
        with self._lock:
            factories = {
                name: (
                    factory_repr(factory, self._returns.get(name)),
                    name in self._singleton_names,
                )
                for name, factory in self._factories.items()
            }
            instances = {
                name: f"{type(inst).__module__}.{type(inst).__qualname__}"
                for name, inst in self._singleton_cache.items()
            }
        return pformat({
            "factories": factories,
            "instances": instances,
        })

    # alias
    reg = register
