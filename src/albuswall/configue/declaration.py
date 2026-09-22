#
""""""

from typing import (
    Tuple, Sequence, TypedDict,
    Any, Optional, Union, Dict,
    TypeVar, Type,
    TYPE_CHECKING, Generic,
    cast,
)

from albuswall.configue.utils import Namespace

if TYPE_CHECKING:
    from albuswall.configue import Configue

_config: Optional["Configue"] = None
_Missing = object()
T = TypeVar("T")


def get_config() -> "Configue":
    global _config
    from albuswall.core import Application
    if _config is None:
        _config = Application.instance().configure
    return _config  # type: ignore


class ConfigMeta(TypedDict):
    path: Sequence[str]
    default: Any
    type: Optional[Union[type, str]]


class ConfigDeclaration:
    _registered: Dict[str, ConfigMeta] = {}
    _registry: Dict[str, ConfigMeta] = {}
    _failed: Dict[str, ConfigMeta] = {}

    _config_getter = staticmethod(get_config)

    def __init__(self):
        type(self).load()

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        cls._registered = {}
        cls._registry = {}
        cls._failed = {}

    @property
    def registered(self):
        return self._registered

    @property
    def registry(self):
        return self._registry

    @classmethod
    def _get_config_namespace(cls) -> Namespace:
        """
        默认返回 Configue.static。
        派生类可覆盖它，或只覆盖 _config_getter。
        """
        return cls._config_getter().static

    @classmethod
    def load(cls):
        for key, meta in cls._registry.items():
            if cls.ensure_config_path(meta["path"], meta["default"]):
                cls._registered[key] = meta
            else:
                cls._failed[key] = meta

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

    @classmethod
    def ensure_config_path(
            cls,
            path: Sequence[str],
            default: Any = _Missing,
    ) -> bool:
        if not path:
            return False

        current: Namespace = cls._get_config_namespace()

        for part in path[:-1]:
            if not isinstance(current, Namespace):
                return False
            if current.get(part, _Missing) is _Missing:
                current[part] = Namespace()
            current = current.get(part, _Missing)

        leaf = path[-1]
        if not isinstance(current, Namespace):
            return False
        if current.get(leaf, _Missing) is not _Missing:
            return True
        if default is not _Missing:
            current[leaf] = default
            return True
        return False

    # alias
    reg = register


# noinspection SpellCheckingInspection
class ConfigField(Generic[T]):
    # 明确用 type[ConfigDeclaration]，避免 "type 上无该属性"
    _declaration_cls: Type[ConfigDeclaration] = ConfigDeclaration

    def __init__(self, *path: str, default=_Missing):
        self._path: Tuple[str, ...] = path
        self._default = default
        self._cached_value = _Missing
        self._cached_exception: Optional[Exception] = None

    # ----- 配置根解析：优先拥有类，其次 Field 自身 -----

    def _resolve_declaration(
            self, owner: Optional[type] = None
    ) -> Type[ConfigDeclaration]:
        if owner is not None:
            decl_cls = getattr(owner, "_config_declaration", None)
            # isinstance + issubclass 收窄，TypeVar 也就对上了
            if isinstance(decl_cls, type) and issubclass(decl_cls, ConfigDeclaration):
                return decl_cls
        return self._declaration_cls

    def _get_namespace(self, owner: Optional[type] = None) -> Namespace:
        decl_cls = self._resolve_declaration(owner)
        # noinspection PyProtectedMember
        return decl_cls._get_config_namespace()  # noqa: SLF001,protected-member

    def __get__(self, instance, owner) -> T:
        if instance is None:
            return cast(T, self)

        if self._cached_exception is not None:
            raise self._cached_exception

        if self._cached_value is not _Missing:
            return self._cached_value

        value = self._get_namespace(owner)
        for part in self._path:
            if not isinstance(value, Namespace):
                exc = self._build_error()
                self._cached_exception = exc
                raise exc
            value = value.get(part, _Missing)
            if value is _Missing:
                if self._default is not _Missing:
                    return self._default
                exc = self._build_error()
                self._cached_exception = exc
                raise exc

        self._cached_value = value
        return value

    def __set__(self, instance, value):
        owner = type(instance) if instance is not None else None
        current: Namespace = self._get_namespace(owner)

        for part in self._path[:-1]:
            if not isinstance(current, Namespace):
                raise TypeError(
                    f"Cannot set config value at path {self._path}: "
                    f"intermediate node '{part}' is not a Namespace"
                )
            if current.get(part, _Missing) is _Missing:
                current[part] = Namespace()
            current = current[part]

        leaf = self._path[-1]
        if not isinstance(current, Namespace):
            raise TypeError(
                f"Cannot set config value at path {self._path}: "
                f"leaf parent is not a Namespace"
            )
        current[leaf] = value

        self._cached_value = _Missing
        self._cached_exception = None

    def __set_name__(self, owner: type, name: str):
        annotation = owner.__annotations__.get(name, None)
        decl_cls = self._resolve_declaration(owner)
        # noinspection PyProtectedMember  # noqa: SLF001
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

# # 类装饰器
# def register_config(cls):
#     annotations = cls.__dict__.get('__annotations__', {})
#     for attr_name, value in cls.__dict__.items():
#         if isinstance(value, ConfigValue):
#             annotation = annotations.get(attr_name, None)
#             ConfigService.register(cls, attr_name, value, annotation)
#     return cls
