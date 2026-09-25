#
""""""

from typing import TYPE_CHECKING, Generic, TypeVar

from .configue import Configue
from .utils import (
    Namespace, FrozenNamespace,
    get_user_config_dir, get_user_data_dir,
    parse_section_file,
)

T = TypeVar("T")

if TYPE_CHECKING:
    from .bootstrap import setup_config
    from .declaration import ConfigDeclaration, ConfigMeta, get_config


    class ConfigField(Generic[T]):
        """仅给类型检查器看的存根。

        用法：
            x = ConfigField[int]("a", "b")       # x: int
            x = ConfigField("a", default=0)      # 由 default 推出 T=int
            x: int = ConfigField("a")            # 由变量注解推出
        """

        def __new__(
                cls,
                *path: str,
                default: T = ...,  # type: ignore[assignment]
        ) -> T: ...  # type: ignore[misc]

__all__ = [
    "Configue", "Namespace", "FrozenNamespace", "get_user_config_dir",
    "get_user_data_dir", "setup_config", "parse_section_file",
    "ConfigDeclaration", "ConfigField", "ConfigMeta", "get_config",
]


def __getattr__(name):
    if name == "setup_config":
        from .bootstrap import setup_config
        return setup_config
    if name == "ConfigDeclaration":
        from .declaration import ConfigDeclaration
        return ConfigDeclaration
    if name == "ConfigMeta":
        from .declaration import ConfigMeta
        return ConfigMeta
    if name == "ConfigField":
        from .declaration import ConfigField
        return ConfigField
    if name == "get_config":
        from .declaration import get_config
        return get_config

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
