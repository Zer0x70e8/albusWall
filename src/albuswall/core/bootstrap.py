#
""""""

import logging
import traceback
import warnings
from typing import Dict, Tuple, Callable, Any, List, Optional, overload
from pprint import pformat

from albuswall.utils.format import factory_repr


# noinspection overloads
class Container:
    logger = logging.getLogger(__name__)

    def __init__(self):
        # 每项: (factory, singleton, returns)
        # returns 为可选元数据，仅用于调试/日志，不参与运行逻辑
        self._factories: Dict[
            str, Tuple[Callable[[], Any], bool, Optional[Any]]
        ] = {}
        self._instances: Dict[str, Any] = {}
        self._boot_callbacks: List[Callable[[], Any]] = []
        self._final_callbacks: List[Callable[[], Any]] = []

        self._finalized = False

    @overload
    def register(self, name: str, factory: Callable[[], Any],
                 singleton: bool = True, returns: Optional[Any] = None):
        ...

    @overload
    def register(self, func: Callable):
        ...

    def register(self, arg1, arg2=None, singleton=True, returns=None):
        if arg2 is None:
            self._factories[arg1.__name__] = (arg1, singleton, returns)
            return arg1
        self._factories[arg1] = (arg2, singleton, returns)
        return None

    def annotate(self, name: str, returns: Optional[Any] = None) -> None:
        """为已注册的工厂补充返回类型注释（可选元数据）。

        - 不触发实例化，也不影响 get / register 的行为；
        - 仅用于 __str__、日志、调试等展示场景；
        - 若 name 未注册则抛 KeyError。

        典型用法::

            container.register('config', lambda: Configue(...))
            container.annotate('config', Configue)
            # 或一次清掉:
            container.annotate('config', None)
        """
        if name not in self._factories:
            raise KeyError(f"Factory {name!r} is not registered.")
        factory, singleton, _ = self._factories[name]
        self._factories[name] = (factory, singleton, returns)

    def get(self, name: str):
        if name in self._instances:
            return self._instances[name]
        factory, singleton, _ = self._factories[name]
        instance = factory()
        if singleton:
            self._instances[name] = instance
        return instance

    def on_boot(self, callback: Callable[[], Any]) -> Callable:
        self._boot_callbacks.append(callback)
        return callback

    def on_boot_insert(self, index: int, callback: Callable[[], Any]) -> None:
        self._boot_callbacks.insert(index, callback)

    def on_final(self, callback: Callable[[], Any]) -> Callable:
        self._final_callbacks.append(callback)
        return callback

    def on_final_insert(self, index: int, callback: Callable[[], Any]) -> None:
        self._final_callbacks.insert(index, callback)

    def exec(self):
        for callback in self._boot_callbacks:
            # noinspection PyBroadException
            try:
                callback()
            except RuntimeError:
                raise
            except Exception:
                # noinspection PyNoneFunctionAssignment
                [self.logger.error(i)
                 for i in ("Boot callback error: "
                           f"{traceback.format_exc()}")
                 .split("\n")]

    def finally_(self):
        if self._finalized:
            warnings.warn("Finally was triggered twice.")
            return
        self._finalized = True
        for callback in self._final_callbacks:
            # noinspection PyBroadException
            try:
                callback()
            except RuntimeError:
                raise
            except Exception:
                # noinspection PyNoneFunctionAssignment
                [self.logger.error(i)
                 for i in ("Boot callback error: "
                           f"{traceback.format_exc()}")
                 .split("\n")]

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
            'boot_callbacks': [
                cb.__name__ for cb in self._boot_callbacks
            ],
        })

    # alias
    reg = register
    boot = on_boot
    final = on_final
