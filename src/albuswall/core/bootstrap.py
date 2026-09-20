#
""""""

import logging
import traceback
import warnings
from typing import Dict, Tuple, Callable, Any, List, overload
from pprint import pformat


# noinspection overloads
class Container:
    logger = logging.getLogger(__name__)

    def __init__(self):
        self._factories: Dict[str, Tuple[Callable[[], Any], bool]] = {}
        self._instances: Dict[str, Any] = {}
        self._boot_callbacks: List[Callable[[], Any]] = []
        self._final_callbacks: List[Callable[[], Any]] = []

        self._finalized = False

    @overload
    def register(self, name: str, factory: Callable[[], Any], singleton=True):
        ...

    @overload
    def register(self, func: Callable):
        ...

    def register(self, arg1, arg2=None, singleton=True):
        if arg2 is None:
            self._factories[arg1.__name__] = (arg1, singleton)
            return arg1
        self._factories[arg1] = (arg2, singleton)
        return None

    def get(self, name: str):
        if name in self._instances:
            return self._instances[name]
        factory, singleton = self._factories[name]
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
            'factories': {name: (func.__name__, singleton) for name, (func, singleton) in self._factories.items()},
            'instances': self._instances,
            'boot_callbacks': [cb.__name__ for cb in self._boot_callbacks],
        })

    # alias
    reg = register
    on = on_boot
    final = on_final
