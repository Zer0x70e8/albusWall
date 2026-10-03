#
"""Configuration Unified Engine"""

# import logging
import weakref
from itertools import chain
from typing import Optional, cast, Any, Callable, Iterator

# import albuswall
from .resolver import Resolver

from .utils.namespace import Namespace


class _Configue:
    __slots__ = ("static", "dynamic", "resolver")

    # logger = logging.getLogger(f"{albuswall.__name__}.configue")

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

    def ensure(self, key, expected_type=None, default=_Missing):
        resolver = object.__getattribute__(self, "_resolver")
        section = object.__getattribute__(self, "_section")
        node = (section, key)

        # 先决定要不要注入默认值
        if node not in resolver:
            if default is _Missing:
                raise KeyError(f"[{section}]{key}")
            resolver.inject_raw(section, key, default)

        value = resolver.get_typed(section, key)

        if expected_type is not None and value is not None \
                and not isinstance(value, expected_type):
            raise TypeError(
                f"[{section}]{key}: expected {expected_type.__name__}, "
                f"got {type(value).__name__}"
            )
        return value

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
        raise AttributeError("view is read-only; use _config.resolver.set(...)")

    def __str__(self):
        return f"[{self._section}]"


class StaticConfig(Namespace):
    __slots__ = ("_resolver", "_views")  # 注意：Namespace 用 __dict__，加 slots 要小心

    def __init__(self, resolver):
        super().__init__()
        object.__setattr__(self, "_resolver", resolver)
        object.__setattr__(self, "_views", {})

    def __getattr__(self, name):
        # 只有 __dict__ 里没有时才会走到这里
        resolver = object.__getattribute__(self, "_resolver")
        if resolver.has_section(name):
            views = object.__getattribute__(self, "_views")
            view = views.get(name)
            if view is None:
                view = _SectionView(resolver, name)
                views[name] = view
            # 直接写 __dict__，绕过未来可能的只读检查
            self.__dict__[name] = view
            return view
        # 顶级 key 懒加载
        value = resolver.get_typed("__default__", name)
        self.__dict__[name] = value  # 缓存
        return value

    # noinspection method-overriding
    def __setattr__(self, name, value):
        raise AttributeError("StaticConfig is read-only; use _config.resolver.set(...)")


# class StaticConfig:
#     __slots__ = ("_resolver", "_views")
#
#     def __init__(self, resolver):
#         object.__setattr__(self, "_resolver", resolver)
#         object.__setattr__(self, "_views", {})
#
#     def __getattr__(self, name):
#         resolver = object.__getattribute__(self, "_resolver")
#         if resolver.has_section(name):
#             views = object.__getattribute__(self, "_views")
#             view = views.get(name)
#             if view is None:
#                 view = _SectionView(resolver, name)
#                 views[name] = view
#             return view
#         # 顶级 key（__default__）
#         return resolver.get_typed("__default__", name)
#
#     def __setattr__(self, name, value):
#         raise AttributeError("StaticConfig is read-only; use _config.resolver.set(...)")
#
#     def items(self):
#         resolver = self._resolver
#         for sec in resolver.sections():
#             yield sec, self.__getattr__(sec)
#         for key in resolver.keys_of("__default__"):
#             yield key, resolver.get_typed("__default__", key)

class DynamicConfig(Namespace):
    """动态配置根——State 子树的挂载点。

    本身不参与状态管理，只提供：
      - mount / mount_lazy / unmount 挂载 API
      - 从根路径访问所有子树（业务层唯一入口）
      - 内部字段过滤（_lazy 等不出现在视图里）

    懒加载：`mount_lazy(name, factory)` 后，第一次 `_config.dynamic.<name>`
    才真正调用 factory；从没访问过的子树既不会读盘，也不会在
    to_dict() / items() 里出现。
    """

    _INTERNAL_ATTRS = frozenset({"_lazy"})

    def __init__(self, **entries: Any) -> None:
        super().__init__()
        object.__setattr__(self, "_lazy", {})
        # self._configue_ref: Optional[weakref.ReferenceType[_Configue]] \
        #     = None  # 由外部注入
        # self._logger = None
        for key, value in entries.items():
            setattr(self, key, value)

    # ── 写路径：只做 dict 提升，不做 state 管理 ─────
    # noinspection method-overriding
    def __setattr__(self, name: str, value: Any) -> None:
        if name in self._INTERNAL_ATTRS or name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        if isinstance(value, dict):
            # 提升为普通 Namespace，而不是 DynamicConfig——
            # 避免嵌套子树继承 _lazy 语义
            value = Namespace(**value)
        object.__setattr__(self, name, value)

    # ── 挂载 API ───────────────────────────────────
    def mount(self, name: str, node: Namespace) -> Namespace:
        """直接挂载一个已构造好的 Namespace（通常是 StatefulNamespace）。"""
        if not isinstance(node, Namespace):
            raise TypeError(
                f"mount expected Namespace, got {type(node).__name__}"
            )
        if name in self._INTERNAL_ATTRS or name.startswith("_"):
            raise ValueError(f"mount: name {name!r} is reserved")
        if name in self.__dict__ or name in self.__dict__["_lazy"]:
            raise KeyError(f"{name!r} already mounted")
        object.__setattr__(self, name, node)
        return node

    def mount_lazy(self, name: str, factory: Callable[[], Namespace]) -> None:
        """挂载懒加载工厂。第一次 `_config.dynamic.<name>` 时调用 factory。"""
        if name in self._INTERNAL_ATTRS or name.startswith("_"):
            raise ValueError(f"mount_lazy: name {name!r} is reserved")
        if name in self.__dict__:
            raise KeyError(f"{name!r} already mounted")
        lazy = object.__getattribute__(self, "_lazy")
        if name in lazy:
            raise KeyError(f"{name!r} already mounted (lazy)")
        lazy[name] = factory

    def unmount(self, name: str) -> Optional[Namespace]:
        """卸载已挂载的节点。懒加载未触发的节点直接丢弃工厂，返回 None。"""
        if name in self.__dict__ and not name.startswith("_"):
            return self.__dict__.pop(name)
        lazy = object.__getattribute__(self, "_lazy")
        if name in lazy:
            lazy.pop(name)
            return None
        raise KeyError(name)

    # ── 懒加载 ─────────────────────────────────────
    def __getattr__(self, name: str) -> Any:
        # 只有在 __dict__ / 类属性里都找不到时才会走到这里
        lazy = self.__dict__.get("_lazy")
        if lazy and name in lazy:
            node = lazy.pop(name)()
            self.__dict__[name] = node  # 缓存到 __dict__，后续直接命中
            return node
        raise AttributeError(
            f"{type(self).__name__!r} object has no attribute {name!r}"
        )

    # ── 视图（屏蔽内部字段）───────────────────────
    def _public_items(self) -> Iterator[tuple[str, Any]]:
        for k, v in self.__dict__.items():
            if k in self._INTERNAL_ATTRS or k.startswith("_"):
                continue
            yield k, v

    def keys(self) -> Iterator[str]:
        return (k for k, _ in self._public_items())

    def values(self) -> Iterator[Any]:
        return (v for _, v in self._public_items())

    def items(self) -> Iterator[tuple[str, Any]]:
        return self._public_items()

    def __iter__(self) -> Iterator[str]:
        return self.keys()

    def __len__(self) -> int:
        return sum(1 for _ in self._public_items())

    def __contains__(self, key: str) -> bool:
        if key in self._INTERNAL_ATTRS or key.startswith("_"):
            return False
        if key in self.__dict__:
            return True
        return key in self.__dict__.get("_lazy", {})

    def to_dict(self) -> dict[str, Any]:
        # 未触发的懒加载节点不序列化——它们本来就是"还没加载"的语义
        out: dict[str, Any] = {}
        for k, v in self._public_items():
            if isinstance(v, Namespace):
                out[k] = v.to_dict()
            else:
                out[k] = v
        return out

    def __repr__(self) -> str:
        loaded = ", ".join(self._public_items().__iter__().__next__()[0] for _ in (0,)) if False else ""
        lazy = list(self.__dict__.get("_lazy", {}))
        return (
            f"{type(self).__name__}("
            f"loaded={[k for k, _ in self._public_items()]}, "
            f"lazy={lazy})"
        )

    def __str__(self) -> str:
        return f"{type(self).__name__}\n" + "\t\n".join(
            f"{k}: {v}" for k, v in self.items()
        )

    # @property
    # def logger(self):
    #     if self._logger is not None:
    #         return self._logger
    #         # 回退到 Configue
    #     # 显式解包弱引用
    #     if self._configue_ref is not None:
    #         parent = self._configue_ref()
    #         if parent is not None:
    #             return parent.logger.getChild("dynamic")
    #     raise RuntimeError("DynamicConfig not bound Configue.")
    #
    # @logger.setter
    # def logger(self, value):
    #     self._logger = value
