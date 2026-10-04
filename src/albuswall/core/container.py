#
"""最小注册表：name -> factory -> instance。

Container 只负责"注册"和"按需解析"。什么时候构造、什么时候拆除、
按什么顺序执行，由 Application 编排。Container 不认识生命周期。
"""
from pprint import pformat
from typing import Any, Callable, Dict, Optional, Tuple

from albuswall.utils.format import factory_repr

_Missing = object()


class Container:
    def __init__(self) -> None:
        # name -> (factory, singleton, returns)
        self._factories: Dict[str, Tuple[Callable[[], Any], bool, Any]] = {}
        self._instances: Dict[str, Any] = {}

    # ---------------- 注册 ----------------
    def register(
            self,
            name: str,
            factory: Callable[[], Any],
            *,
            singleton: bool = True,
            returns: Optional[Any] = None,
    ) -> Callable[[], Any]:
        if name in self._factories:
            raise KeyError(f"duplicate registration: {name!r}")
        self._factories[name] = (factory, singleton, returns)
        return factory

    def annotate(self, name: str, returns: Optional[Any] = None) -> None:
        """补充/修正返回类型元数据，不触发构造。"""
        if name not in self._factories:
            raise KeyError(f"not registered: {name!r}")
        factory, singleton, _ = self._factories[name]
        self._factories[name] = (factory, singleton, returns)

    # ---------------- 解析 ----------------
    def get(self, name: str, default: Any = _Missing) -> Any:
        if name not in self._factories:
            if default is not _Missing:
                return default
            raise KeyError(f"not registered: {name!r}")

        factory, singleton, _ = self._factories[name]
        if not singleton:
            return factory()

        if name not in self._instances:
            self._instances[name] = factory()
        return self._instances[name]

    # noinspection broad-exception
    def try_get(self, name: str, default: Any = None) -> Any:
        """永不抛异常的可选查询（工厂内部异常也会被吞掉并返回 default）。"""
        try:
            return self.get(name, default)
        except Exception:
            return default

    def require(self, name: str) -> Any:
        """按需构造。未注册抛 KeyError。

        与 get(name) 的唯一区别是语义表达：
        调用方在说"这个依赖是必须的"。没有 default 逃生口。
        """
        return self.get(name)

    def peek(self, name, default=_Missing):
        """只看已缓存的单例；不存在返回 default 或抛 KeyError。不触发构造。"""
        if name not in self._instances:
            if default is not _Missing:
                return default
            raise KeyError(f"not instantiated: {name!r}")
        return self._instances[name]

    def has_instance(self, name: str) -> bool:
        return name in self._instances

    # ---------------- 调试 ----------------
    def __contains__(self, name: str) -> bool:
        return name in self._factories

    def __str__(self):
        """Return a pretty-printed string representation of the container."""
        return pformat({
            'factories': {
                name: (factory_repr(func, returns), singleton)
                for name, (func, singleton, returns)
                in self._factories.items()
            },
            'instances': {
                name: f"{type(inst).__module__}.{type(inst).__qualname__}"
                for name, inst in self._instances.items()
            },
        })

    # alias
    reg = register
