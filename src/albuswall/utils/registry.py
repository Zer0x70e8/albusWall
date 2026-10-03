#
"""消除 ``__init__`` 中大量 ``container.reg`` + ``lambda`` 样板代码的工具。

核心思路：**显式传入 ``{字段名: 类型}`` 映射**，不在内部解析 ``__annotations__``。
好处是：
* 注册来源一目了然，不依赖反射 / 注解求值；
* 兼容 ``from __future__ import annotations``，也不怕 TypedDict 被拆散；
* 可以在测试里传一个临时 map，不动生产代码。

用法::

    from .repository_registry import register_repositories

    REPOSITORIES = {
        "ingest_source_repo": IngestSourceRepository,
        "import_repo": ImportRepository,
        "view_repo": ViewRepository,
        "thumbnail_repo": ThumbnailRepository,
        "asset": AssetRepository,
    }

    _ARG_MAP = {
        # "asset": lambda t, c: t(c.get("db"), c.get("fs")),
    }

    def registry_repository(container: "Container"):
        register_repositories(container, REPOSITORIES, arg_map=_ARG_MAP)

生成的 getter 是**具名函数**（不是 lambda），名字按类的驼峰名蛇形化 + 后缀生成，
例如 ``IngestSourceRepository`` -> ``ingest_source_repository_getter``。
"""

from __future__ import annotations

import re
# from functools import partial
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    Mapping,
)

if TYPE_CHECKING:
    from albuswall.core import Container

__all__ = [
    "snake_case",
    "make_getter_name",
    "build_getters",
    "register_tool",
]


#
class _BoundGetter:
    __slots__ = ("_fn", "_container", "_field", "_returns")

    def __init__(self, fn, container, *, field, returns):
        self._fn = fn
        self._container = container
        self._field = field
        self._returns = returns

    def __call__(self):
        return self._fn(self._container)

    def __repr__(self):
        name = getattr(self._returns, "__name__", self._returns)
        return f"<getter {self._field} -> {name}>"


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
    """根据类名生成 getter 的函数名。

    默认后缀 ``"getter"``，也可以传 ``"get"``、``"factory"`` 等。
    """
    return f"{snake_case(cls.__name__)}_{suffix}"


# --------------------------------------------------------------------------- #
# 默认工厂
# --------------------------------------------------------------------------- #

def _default_factory(type_: Any, container: "Container") -> Any:
    """默认工厂：用 container 里已注册的 ``db`` 构造仓库。"""
    return type_(container.get("db"))


# --------------------------------------------------------------------------- #
# 生成具名 getter
# --------------------------------------------------------------------------- #

def build_getters(
        repositories: Mapping[str, type],
        *,
        arg_map: Mapping[str, Callable[[Any, "Container"], Any]] | None = None,
        default_factory: Callable[[Any, "Container"], Any] | None = None,
        suffix: str = "getter",
) -> Dict[str, Callable[["Container"], Any]]:
    """为 ``repositories`` 里的每个字段生成一个具名 getter。

    :param repositories: ``{字段名: 类型}``，显式给出，不做注解解析
    :param arg_map: ``{字段名: (type_, container) -> instance}``，
        命中的字段走这里的工厂；未命中的走 ``default_factory``
    :param default_factory: 未命中时的兜底工厂，默认 ``type_(container.get("db"))``
    :param suffix: 生成函数名的后缀，默认 ``"getter"``
    :return: ``{字段名: getter}``，getter 签名是 ``(container) -> instance``
    """
    arg_map = dict(arg_map or {})
    factory_default = default_factory or _default_factory

    getters: Dict[str, Callable[["Container"], Any]] = {}

    for field_name, type_ in repositories.items():
        factory = arg_map.get(field_name, factory_default)

        def getter(
                container: "Container",
                _factory=factory,
                _type=type_,
        ) -> Any:
            return _factory(_type, container)

        fn_name = make_getter_name(type_, suffix)
        getter.__name__ = fn_name
        getter.__qualname__ = fn_name
        # noinspection string-conversion-without-dunder-method
        getter.__doc__ = (
            f"构造并返回 :class:`{getattr(type_, '__name__', type_)}` 实例。"
        )
        getters[field_name] = getter

    return getters


# --------------------------------------------------------------------------- #
# 一次性注册到 container
# --------------------------------------------------------------------------- #

def register_tool(
        container: "Container",
        repositories: Mapping[str, type],
        *,
        arg_map: Mapping[str, Callable[[Any, "Container"], Any]] | None = None,
        default_factory: Callable[[Any, "Container"], Any] | None = None,
        suffix: str = "getter",
) -> Dict[str, Callable[[], Any]]:
    """把 ``repositories`` 里声明的所有仓库注册到 ``container``。

    返回 ``{字段名: 无参 accessor}``，可用于测试或手动调用。
    """
    getters = build_getters(
        repositories,
        arg_map=arg_map,
        default_factory=default_factory,
        suffix=suffix,
    )

    accessors: Dict[str, Callable[[], Any]] = {}
    for field_name, type_ in repositories.items():
        # bound = partial(getters[field_name], container)
        # container.reg(field_name, bound, returns=type_)
        # accessors[field_name] = bound
        accessors[field_name] = _BoundGetter(
            getters[field_name], container,
            field=field_name, returns=type_,
        )
        container.reg(field_name, accessors[field_name], returns=type_)

    return accessors
