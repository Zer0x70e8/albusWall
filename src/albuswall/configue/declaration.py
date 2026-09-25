#
"""ConfigDeclaration and ConfigField."""

import typing
import warnings
from pathlib import Path
from typing import (
    Tuple, Sequence, TypedDict,
    Any, Optional, Union, Dict,
    TypeVar, Type,
    TYPE_CHECKING, Generic,
    cast,
)

if TYPE_CHECKING:
    from albuswall.configue import Configue

_config: Optional["Configue"] = None
_Missing = object()
T = TypeVar("T")


# noinspection bad-return
def get_config() -> "Configue":
    global _config
    from albuswall.core import Application
    if _config is None:
        _config = Application.instance().configure
    return _config


# ── 从 bootstrap.py 搬过来 ─────────────────────────────
def _resolve_annotation(ann: Any) -> Any:
    """将字符串注解解析为类型对象，失败时回退为 str。"""
    if not isinstance(ann, str):
        return ann

    import builtins
    namespace: dict[str, Any] = {}
    namespace.update(vars(builtins))
    namespace.update(vars(typing))
    namespace.update({
        "Path": Path,
        "Any": Any,
        "Optional": typing.Optional,
        "List": typing.List,
        "Tuple": typing.Tuple,
        "Dict": typing.Dict,
        "Union": typing.Union,
    })
    try:
        return eval(ann, namespace)
    except Exception as e:
        warnings.warn(
            f"Failed to resolve annotation string '{ann}': {e}, "
            f"falling back to str"
        )
        return str


class ConfigMeta(TypedDict):
    path: Sequence[str]
    default: Any
    type: Optional[Union[type, str]]


class ConfigDeclaration:
    _registry: Dict[str, ConfigMeta] = {}

    _config_getter = staticmethod(get_config)

    # ── __init__ 删掉（不再自动 load，因为 load 需要 resolver） ──

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        cls._registry = {}
        cls._config_declaration = cls  # 反向写入

    @property
    def registry(self) -> Dict[str, ConfigMeta]:
        return self._registry

    @classmethod
    def load(cls, resolver) -> None:
        """把 registry 里的声明注册到 Resolver：类型 + 缺失时注入默认值。"""
        for key, meta in cls._registry.items():
            path = meta["path"]
            if len(path) == 2:
                section, field = path[0], path[1]
            else:
                section, field = "__default__", path[0]

            converter = _resolve_annotation(meta["type"])
            resolver.register_type(section, field, converter)

            if meta["default"] is not _Missing and (section, field) not in resolver:
                resolver.inject_raw(section, field, meta["default"])

    @classmethod
    def register(cls, owner_cls, attr_name, config_value, annotation):
        key = (
            f"{owner_cls.__module__}"
            f".{owner_cls.__qualname__}"
            f".{attr_name}"
        )
        cls._registry[key] = {
            "path": config_value.path,
            "default": config_value.default,
            "type": annotation,
        }

    reg = register


# noinspection SpellCheckingInspection
class ConfigField(Generic[T]):
    _declaration_cls: Type[ConfigDeclaration] = ConfigDeclaration

    def __init__(self, *path: str, default=_Missing):
        self._path: Tuple[str, ...] = path
        self._default = default
        self._cached_value = _Missing
        self._cached_exception: Optional[Exception] = None

    def _resolve_declaration(self, owner=None) -> Type[ConfigDeclaration]:
        if owner is not None:
            decl_cls = getattr(owner, "_config_declaration", None)
            if isinstance(decl_cls, type) and issubclass(decl_cls, ConfigDeclaration):
                return decl_cls
        return self._declaration_cls

    def __get__(self, instance, owner) -> T:
        if instance is None:
            return cast(T, self)
        if self._cached_exception is not None:
            raise self._cached_exception
        if self._cached_value is not _Missing:
            return self._cached_value

        resolver = get_config().resolver
        section, key = self._split_path()
        try:
            value = resolver.get_typed(section, key)
        except KeyError:
            if self._default is not _Missing:
                return self._default
            exc = self._build_error()
            self._cached_exception = exc
            raise exc

        self._cached_value = value
        return value

    def __set__(self, instance, value):
        resolver = get_config().resolver
        section, key = self._split_path()
        resolver.set(section, key, value)
        self._cached_value = _Missing
        self._cached_exception = None

    def _split_path(self) -> tuple[str, str]:
        if len(self._path) == 2:
            return self._path[0], self._path[1]
        return "__default__", self._path[0]

    def __set_name__(self, owner: type, name: str):
        annotation = owner.__annotations__.get(name, None)
        decl_cls = self._resolve_declaration(owner)
        decl_cls.register(owner, name, self, annotation)

    @property
    def path(self):
        return self._path

    @property
    def default(self):
        return self._default

    def _build_error(self) -> Exception:
        path_str = ".".join(str(part) for part in self._path)
        return ValueError(f"name '{path_str}' is not defined")

    def return_default(self):
        if self._default is _Missing:
            path_str = ".".join(str(part) for part in self._path)
            raise ValueError(f"name '{path_str}' is not defined")
        return self._default


def build_type_map_and_defaults(declaration) -> tuple[dict, dict]:
    """从 Declaration.registry 构建 type_map / defaults。

    静态配置已改走 Resolver，不再用它。
    动态 preference 仍走 TypedConfigParser，用它准备 parser 输入。
    """
    from albuswall.configue.utils.deep_merge import deep_merge_dicts

    type_map: dict[str, dict] = {}
    defaults: dict[str, dict] = {}

    for meta in declaration.registry.values():
        path = meta["path"]
        if len(path) != 2:
            continue
        section, field = path
        converter = _resolve_annotation(meta["type"])  # 本模块函数
        type_map.setdefault(section, {})[field] = converter
        if meta["default"] is not _Missing:  # 本模块 _Missing
            defaults.setdefault(section, {})[field] = meta["default"]

    for meta in declaration.registry.values():
        path = meta["path"]
        if len(path) != 1:
            continue
        field = path[0]
        converter = _resolve_annotation(meta["type"])
        default = meta["default"]

        is_dict_type = (
                converter is dict
                or (hasattr(converter, "__origin__") and converter.__origin__ is dict)
                or isinstance(default, dict)
        )
        if is_dict_type:
            section = field
            if default is not _Missing and isinstance(default, dict):
                existing = defaults.get(section, {})
                defaults[section] = deep_merge_dicts(default, existing)
            continue

        type_map.setdefault("__default__", {})[field] = converter
        if default is not _Missing:
            defaults.setdefault("__default__", {})[field] = default

    return type_map, defaults


def promote_default_section(config_dict: dict) -> dict:
    """把 __default__ 节提升到顶层。"""
    from albuswall.configue.utils.deep_merge import deep_merge_dicts
    if "__default__" in config_dict:
        default_section = config_dict.pop("__default__")
        return deep_merge_dicts(config_dict, default_section)
    return config_dict


def needs_default_section(file: Path) -> bool:
    """文件第一个有效行不是节头时返回 True。"""
    if not file.exists():
        return False
    with file.open("r", encoding="utf-8-sig") as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith(("#", ";")):
                continue
            return not stripped.startswith("[")
    return False
