#
"""Configuration Unified Engine"""

import logging
import weakref
from itertools import chain
from typing import Optional, cast

import albuswall

from .utils.namespace import Namespace, FrozenNamespace


class _Configue:
    __slots__ = ("static", "dynamic")

    logger = logging.getLogger(f"{albuswall.__name__}.configue")

    def __init__(self):
        self.static = StaticConfig()
        self.dynamic = DynamicConfig()

    def __str__(self):
        return "\n".join((
            f"{type(self).__name__}(",
            f"\t{type(self.static).__name__}: (",
            *chain.from_iterable(
                [f"\t\t{k}: {v}"] if not isinstance(v, Namespace)
                else [f"\t\t{k}:"] + [f"\t\t\t{k_}: {v_}" for k_, v_ in v.items()]
                for k, v in self.static.items()
            ),
            "\t\t),",
            f"\t{type(self.dynamic).__name__}: (",
            *chain.from_iterable(
                [f"\t\t{k}: {v}"] if not isinstance(v, Namespace)
                else [f"\t\t{k}:"] + [f"\t\t\t{k_}: {v_}" for k_, v_ in v.items()]
                for k, v in self.dynamic.items()
            ),
            "\t\t)",
            ")"
        ))


class Configue(_Configue):
    _instance: Optional[_Configue] = None
    _is_initialized = False

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)  # type: ignore[arg-type]
        return cast(Configue, cls._instance)

    def __init__(self):
        if Configue._is_initialized:
            return
        Configue._is_initialized = True
        super().__init__()
        self.dynamic._configue_ref = weakref.ref(self)


class StaticConfig(FrozenNamespace):
    """Immutable configuration carrier loaded from a nested dictionary.

    Inherits from FrozenNamespace to provide a read-once, read-many container.
    """

    def __init__(self, **entries):
        # Initialize as unfrozen so that load() can write attributes.
        super().__init__(**entries)

    def load(self, config_dict: dict):
        """Load configuration from a doubly-nested dictionary.

        Keys become attributes; sub-dictionaries become nested FrozenNamespace
        instances. Once loaded, the instance is frozen and further attempts to
        modify it (including calling load again) will raise an AttributeError.
        """
        # FrozenNamespace raises AttributeError if already frozen,
        # so trying to set an attribute here acts as the "already loaded" guard.
        for key, value in config_dict.items():
            # Attribute setting will convert any dict value to a nested
            # FrozenNamespace automatically (via Namespace.__setattr__).
            setattr(self, key, value)

        # Seal the namespace – no more changes allowed.
        self.frozen()

    def __str__(self):
        return f"{type(self).__name__}\n" + ("\t\n".join(
            f"{k}: {v}" for k, v in self.items()
        ))


class DynamicConfig(Namespace):
    """"""

    def __init__(self, **entries):
        super().__init__(**entries)
        self._configue_ref: Optional[weakref.ReferenceType[_Configue]] \
            = None  # 由外部注入
        self._logger = None

    def __str__(self):
        return f"{type(self).__name__}\n" + ("\t\n".join(
            f"{k}: {v}" for k, v in self.items()
        ))

    @property
    def logger(self):
        if self._logger is not None:
            return self._logger
            # 回退到 Configue
        # 显式解包弱引用
        if self._configue_ref is not None:
            parent = self._configue_ref()
            if parent is not None:
                    return parent.logger.getChild("dynamic")
        raise RuntimeError("DynamicConfig not bound Configue.")

    @logger.setter
    def logger(self, value):
        self._logger = value

    def items(self):
        return ((k, v) for k, v in self.__dict__.items() if not k.startswith("_"))
