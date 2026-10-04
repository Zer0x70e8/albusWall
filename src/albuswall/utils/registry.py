#
"""消除 ``__init__`` 中大量 ``container.reg`` + ``lambda`` 样板代码的工具。

本模块**不依赖** ``albuswall.core``。
它对容器的唯一要求是：调用方提供一个签名等价于

    container.reg(name, factory, singleton=True, returns=None)

的注册函数，本模块只负责「遍历 getter 列表 + 转交 register」，
完全不接触容器对象本身。

用法::

    from albuswall.utils.registry import build_getters, register_all

    REPOSITORIES = {
        "ingest_source_repo": IngestSourceRepository,
        "import_repo": ImportRepository,
        "view_repo": ViewRepository,
        "thumbnail_repo": ThumbnailRepository,
        "asset_repo": AssetRepository,
    }

    _ARG_MAP = {
        # "asset": lambda t, c: t(c.get("db"), c.get("fs")),
    }

    def registry_repository(container):
        register_all(
            build_getters(container, REPOSITORIES, arg_map=_ARG_MAP),
            container.reg,
        )
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

__all__ = [
    "Getter",
    "snake_case",
    "make_getter_name",
    "build_getters",
    "register_all",
]


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Getter:
    """一条待注册项。

    - ``field``   : 容器里的键，如 ``"ingest_source_repo"``
    - ``name``    : 生成的工厂函数名，如 ``"ingest_source_repository_getter"``
    - ``factory`` : **无参**可调用，``() -> instance``（已经绑定过容器）
    - ``returns`` : 期望的返回类型，仅作为元数据传给 register
    """
    field: str
    name: str
    factory: Callable[[], Any]
    returns: type


# --------------------------------------------------------------------------- #
# 命名工具
# --------------------------------------------------------------------------- #

_CAMEL_BOUNDARY_1 = re.compile(r"(.)([A-Z][a-z]+)")
_CAMEL_BOUNDARY_2 = re.compile(r"([a-z0-9])([A-Z])")


def snake_case(name: str) -> str:
    """``"IngestSourceRepository"`` -> ``"ingest_source_repository"``。"""
    s = _CAMEL_BOUNDARY_1.sub(r"\1_\2", name)
    return _CAMEL_BOUNDARY_2.sub(r"\1_\2", s).lower()


def make_getter_name(cls: type, suffix: str = "getter") -> str:
    """根据类名生成 getter 的函数名。"""
    return f"{snake_case(cls.__name__)}_{suffix}"


# --------------------------------------------------------------------------- #
# 默认工厂
# --------------------------------------------------------------------------- #

def _default_factory(type_: Any, container: Any) -> Any:
    """默认工厂：用 container 里已注册的 ``db`` 构造。"""
    return type_(container.get("db"))


# --------------------------------------------------------------------------- #
# 构建 Getter 列表
# --------------------------------------------------------------------------- #

def build_getters(
    container: Any,
    repositories: Mapping[str, type],
    *,
    arg_map: Mapping[str, Callable[[Any, Any], Any]] | None = None,
    default_factory: Callable[[Any, Any], Any] | None = None,
    suffix: str = "getter",
) -> list[Getter]:
    """为 ``repositories`` 里每个字段产出一条 :class:`Getter`。

    ``container`` 仅用于构造实例（调用它的 ``get``）；
    本函数不做任何注册，纯产出数据。
    """
    arg_map = dict(arg_map or {})
    factory_default = default_factory or _default_factory

    getters: list[Getter] = []

    for field_name, type_ in repositories.items():
        factory = arg_map.get(field_name, factory_default)

        # 用默认参数绑定循环变量，避免闭包 capture 到最后一个值
        def _build(_factory=factory, _type=type_):
            return _factory(_type, container)

        fn_name = make_getter_name(type_, suffix)
        _build.__name__ = fn_name
        _build.__qualname__ = fn_name
        # noinspection string-conversion-without-dunder-method
        _build.__doc__ = (
            f"构造并返回 :class:`{getattr(type_, '__name__', type_)}` 实例。"
        )

        getters.append(
            Getter(
                field=field_name,
                name=fn_name,
                factory=_build,
                returns=type_,
            )
        )

    return getters


# --------------------------------------------------------------------------- #
# 交给外部注册
# --------------------------------------------------------------------------- #

def register_all(
    getters: Iterable[Getter],
    register: Callable[..., Any],
) -> None:
    """逐条把 ``Getter`` 交给 ``register``。

    ``register`` 需等价于 ``container.reg``：

        (name, factory, singleton=True, returns=None) -> Any

    本函数不持有、也不引用容器；容器只通过 ``register`` 这个回调
    与工具发生联系。
    """
    for g in getters:
        register(g.field, g.factory, returns=g.returns)

# lambda getters, register: [register(g.field, g.factory, returns=g.returns) for g in getters]
