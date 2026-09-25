#
"""Configuration Unified Engine"""

import logging
import weakref
from itertools import chain
from typing import Optional, cast

import albuswall
from .resolver import Resolver

from .utils.namespace import Namespace


class _Configue:
    __slots__ = ("static", "dynamic", "resolver")

    logger = logging.getLogger(f"{albuswall.__name__}.configue")

    def __init__(self):
        self.resolver = Resolver()
        self.static = StaticConfig(self.resolver)
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


# class StaticConfig(FrozenNamespace):
#     """Immutable configuration carrier loaded from a nested dictionary.
#
#     Inherits from FrozenNamespace to provide a read-once, read-many container.
#     """
#
#     def __init__(self, **entries):
#         # Initialize as unfrozen so that load() can write attributes.
#         super().__init__(**entries)
#
#     def load(self, config_dict: dict):
#         """Load configuration from a doubly-nested dictionary.
#
#         Keys become attributes; sub-dictionaries become nested FrozenNamespace
#         instances. Once loaded, the instance is frozen and further attempts to
#         modify it (including calling load again) will raise an AttributeError.
#         """
#         # FrozenNamespace raises AttributeError if already frozen,
#         # so trying to set an attribute here acts as the "already loaded" guard.
#         for key, value in config_dict.items():
#             # Attribute setting will convert any dict value to a nested
#             # FrozenNamespace automatically (via Namespace.__setattr__).
#             setattr(self, key, value)
#
#         # Seal the namespace – no more changes allowed.
#         self.frozen()
#
#     def __str__(self):
#         return f"{type(self).__name__}\n" + ("\t\n".join(
#             f"{k}: {v}" for k, v in self.items()
#         ))

_Missing = object()

class _SectionView:
    __slots__ = ("_resolver", "_section")

    def __init__(self, resolver, section):
        object.__setattr__(self, "_resolver", resolver)
        object.__setattr__(self, "_section", section)

    def __getattr__(self, key):
        return self._resolver.get_typed(self._section, key)

    def __getitem__(self, key):
        return self._resolver.get_typed(self._section, key)

    # 旧 API 兼容
    # noinspection unused-parameter
    def ensure(self, key, expected_type=None, default=_Missing):
        """确保 key 存在并返回类型化值。

        - 已存在：走 resolver.get_typed（缓存 + 类型转换）
        - 不存在且有 default：注入 raw 后再取
        - 不存在且无 default：KeyError

        expected_type 只是签名兼容，真正的类型转换由 resolver 负责
        （因为类型早已通过 ConfigDeclaration.register 注册进 resolver 了）。
        """
        resolver = object.__getattribute__(self, "_resolver")
        section = object.__getattribute__(self, "_section")
        node = (section, key)

        value = resolver.get_typed(section, key)
        if expected_type is not None and value is not None and not isinstance(value, expected_type):
            raise TypeError(
                f"[{section}]{key}: expected {expected_type.__name__}, "
                f"got {type(value).__name__}"
            )
        if node not in resolver:
            if default is _Missing:
                raise KeyError(f"[{section}]{key}")
            resolver.inject_raw(section, key, default)  # 不要 str()
        return resolver.get_typed(section, key)

    def get(self, key, default=None):
        try:
            return self._resolver.get_typed(self._section, key)
        except KeyError:
            return default

    def __contains__(self, key):
        return (self._section, key) in self._resolver

    def keys(self):
        return self._resolver.keys_of(self._section)

    def items(self):
        for k in self.keys():
            yield k, self._resolver.get_typed(self._section, k)

    def __setattr__(self, k, v):
        raise AttributeError("view is read-only; use config.resolver.set(...)")

    def __str__(self):
        return f"[{self._section}]"


class StaticConfig:
    __slots__ = ("_resolver", "_views")

    def __init__(self, resolver):
        object.__setattr__(self, "_resolver", resolver)
        object.__setattr__(self, "_views", {})

    def __getattr__(self, name):
        resolver = object.__getattribute__(self, "_resolver")
        if resolver.has_section(name):
            views = object.__getattribute__(self, "_views")
            view = views.get(name)
            if view is None:
                view = _SectionView(resolver, name)
                views[name] = view
            return view
        # 顶级 key（__default__）
        return resolver.get_typed("__default__", name)

    def __setattr__(self, name, value):
        raise AttributeError("StaticConfig is read-only; use config.resolver.set(...)")

    def items(self):
        resolver = self._resolver
        for sec in resolver.sections():
            yield sec, self.__getattr__(sec)
        for key in resolver.keys_of("__default__"):
            yield key, resolver.get_typed("__default__", key)


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
