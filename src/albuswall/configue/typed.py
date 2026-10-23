#
"""TypedDict 视图：把静态配置声明映射成 typed dict。

配置声明（``ConfigDeclaration`` 子类里的 ``ConfigField``）是字段 path / 类型 /
默认值的唯一事实来源，``TypedDict`` 只负责「形状 + 类型」。``load_typed``
把两者对上：

    class WindowConfig(TypedDict):
        qss: Optional[Path]

    conf = load_typed(WindowConfig, ConfigDeclaration)

取值规则（每个键独立判定）：

1. 在给出的声明里按 **属性名** 找到该字段的 ``ConfigMeta``；找不到 →
   ``LookupError``（typed dict 与声明脱节属于编码错误，不静默兜底）；
2. ``(section, key) in resolver`` → 取 ``resolver.get_typed``（INI 里的值，
   或声明注入的默认值，类型已按注册的转换器转好）；
3. 否则回落到声明的 ``default``（``_Missing`` 视为 ``None``）；
4. 按 typed dict 的注解做一次轻量收敛（``str`` → ``Path`` 这类；不做完整
   校验），读取路径上的异常只 warning，仍用声明默认值——配置读不出来
   不应该把 UI 拖垮。

声明来源有两种给法（可混用）：

* ``ConfigDeclaration`` 子类 → 用**它自己的**注册表（偏好层优先就是这么写的）；
* 任意**声明所有者类**（带 ``ConfigField`` 的普通类，如
  ``ThumbnailConfig`` / ``ImportConfig``）→ 用**它名下登记的字段**。

第二种是服务层需要的：``batch_size`` 这类名字在多个模块里都存在，若按
全局注册表查就会串到别的 section。按 owner 过滤后，一个 typed dict 只
可能绑定到一个类的字段。

``__default__`` 的处理与 ``ConfigField._split_path`` 保持一致：一元素 path
（例如 ``ConfigField("debug", default=False)``）落到 ``__default__`` 节。
"""

from __future__ import annotations

import types
from pathlib import Path
from typing import Any, Sequence, Union, get_args, get_origin

from albuswall.log import getLogger

from .declaration import ConfigDeclaration, ConfigMeta, _Missing
from .utils.annotation import resolve_annotation

__all__ = [
    "class_metas",
    "declaration_metas",
    "field_annotations",
    "load_typed",
    "split_path",
]

_logger = getLogger("albuswall.configue.typed")

# Optional[X] 既可能来自 typing.Union，也可能是 PEP 604 的 X | None
_UNION_ORIGINS = (Union, getattr(types, "UnionType", Union))


# ══════════════════════════════════════════════════
# 声明 ↔ 注解
# ══════════════════════════════════════════════════
def split_path(path: Sequence[str]) -> tuple[str, str]:
    """把声明的 path 拆成 ``(section, key)``，与 ``ConfigField`` 同语义。"""
    if len(path) == 2:
        return str(path[0]), str(path[1])
    return "__default__", str(path[0])


def class_metas(owner: Any) -> dict[str, ConfigMeta]:
    """取**某个类名下**登记的字段，返回 ``{字段属性名: ConfigMeta}``。

    注册表的键是 ``module.qualname.attr``，这里按 ``module.qualname.`` 前缀
    过滤，只留下该类自己声明的 ``ConfigField``。这样 ``ThumbnailConfig``
    的 ``batch_size`` 不会跟 ``ImportConfig`` 的撞在一起。
    """
    if not isinstance(owner, type):
        raise TypeError(
            f"{owner!r} is not a class; pass the class that declares "
            f"ConfigFields (not an instance)"
        )
    prefix = f"{owner.__module__}.{owner.__qualname__}."
    return {
        key[len(prefix):]: meta
        for key, meta in ConfigDeclaration._registry.items()
        if key.startswith(prefix)
    }


def declaration_metas(*declarations: Any) -> dict[str, ConfigMeta]:
    """合并多份声明，返回 ``{字段属性名: ConfigMeta}``。

    每个参数可以是 ``ConfigDeclaration`` 子类（用它自己的注册表），也可以是
    任意带 ``ConfigField`` 的**声明所有者类**（按 ``class_metas`` 取它名下的
    字段）。

    同名属性按传入顺序取第一个（先声明的组优先），这样调用方可以用
    ``(PreferenceDeclaration, ConfigDeclaration)`` 表达「偏好层优先」。
    """
    metas: dict[str, ConfigMeta] = {}
    for declaration in declarations:
        registry = getattr(declaration, "_registry", None)
        if registry is not None:
            entries = [
                (key.rsplit(".", 1)[-1], meta)
                for key, meta in registry.items()
            ]
        else:
            entries = list(class_metas(declaration).items())
            if not entries:
                raise TypeError(
                    f"{declaration!r} declares no ConfigField; "
                    f"did you forget to import the module that defines it?"
                )
        for name, meta in entries:
            metas.setdefault(name, meta)
    return metas


def field_annotations(td: Any) -> dict[str, Any]:
    """取 typed dict 的键与注解；字符串注解就地解析（失败按 ``Any`` 处理）。"""
    resolved: dict[str, Any] = {}
    for name, raw in getattr(td, "__annotations__", {}).items():
        annotation = (
            resolve_annotation(raw, warn=False) if isinstance(raw, str) else raw
        )
        resolved[name] = Any if annotation is None else annotation
    return resolved


# ══════════════════════════════════════════════════
# 取值 + 轻量收敛
# ══════════════════════════════════════════════════
def _default_of(meta: ConfigMeta) -> Any:
    default = meta["default"]
    return None if default is _Missing else default


def _leaf_types(annotation: Any) -> tuple[Any, ...]:
    """展开 ``Optional`` / ``Union``，返回不重复的非 None 分支。"""
    if annotation is None or annotation is Any:
        return ()
    origin = get_origin(annotation)
    if origin in _UNION_ORIGINS:
        leaves: list[Any] = []
        for arg in get_args(annotation):
            if arg is type(None):
                continue
            for leaf in _leaf_types(arg):
                if leaf not in leaves:
                    leaves.append(leaf)
        return tuple(leaves)
    return (annotation,)


def _coerce(value: Any, annotation: Any) -> Any:
    """按注解做最小收敛：目前只处理 ``str`` → ``Path``。

    ``None`` 永远放行（``Optional`` 语义），其余类型信任 ``Resolver`` 的
    转换器，不再二次校验——这里的目标是「值可用」，不是「值合法」。
    """
    if value is None or not isinstance(value, str):
        return value
    if Path in _leaf_types(annotation):
        return Path(value)
    return value


def load_typed(
        td: Any,
        *declarations: Any,
        resolver: Any = None,
) -> dict[str, Any]:
    """按 typed dict 的形状构造配置快照。

    Args:
        td: ``TypedDict`` 类（只要 ``__annotations__`` 就够）。
        *declarations: ``ConfigDeclaration`` 类**或**带 ``ConfigField`` 的
            声明所有者类，决定默认值 / 类型来自哪里；不传等价于
            ``(ConfigDeclaration,)``（即全部静态字段声明）。
        resolver: 取值用的 ``Resolver``；默认取当前 ``Configue`` 的。

    Returns:
        普通 dict（键与 typed dict 完全一致，缺失键已用声明默认值补齐）。

    失败语义：typed dict 里有键找不到声明 → ``LookupError``；
    单个键读取 / 转换失败 → 记 warning 并回落到声明默认值。
    """
    if not declarations:
        declarations = (ConfigDeclaration,)
    metas = declaration_metas(*declarations)

    if resolver is None:
        from .declaration import get_config

        resolver = get_config().resolver

    values: dict[str, Any] = {}
    for name, annotation in field_annotations(td).items():
        meta = metas.get(name)
        if meta is None:
            names = ", ".join(
                getattr(d, "__name__", repr(d)) for d in declarations
            )
            raise LookupError(
                f"{getattr(td, '__name__', repr(td))}.{name}: "
                f"no matching config declaration in {names}"
            )

        section, key = split_path(meta["path"])
        default = _default_of(meta)
        if (section, key) in resolver:
            try:
                value = resolver.get_typed(section, key)
            except Exception as exc:  # noqa: BLE001  读取失败不拖垮调用方
                _logger.warning(
                    "typed config: read [%s]%s failed (%s); "
                    "falling back to declared default",
                    section, key, exc,
                )
                value = default
        else:
            value = default

        values[name] = _coerce(value, annotation)
    return values
