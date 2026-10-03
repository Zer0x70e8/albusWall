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
    from .bootstrap import setup_dynamic_config, setup_static_config
    from .declaration import ConfigDeclaration, ConfigMeta, get_config

    # state.py 里全是运行期实现，直接 import 供类型检查器用即可；
    # StateField 是泛型描述符，IDE 对 __get__ 的返回类型解析得动，
    # 不需要像 ConfigField 那样写存根。
    from .state import (
        ObservableNamespace, StatefulNamespace, StateField,
        ObservableContext, StateContext,
    )


    class ConfigField(Generic[T]):
        """配置项描述符，自动根据解析配置。

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
    # config
    "Configue", "Namespace", "FrozenNamespace",
    "ConfigDeclaration", "ConfigField", "ConfigMeta", "get_config",
    # utils
    "get_user_config_dir", "get_user_data_dir",
    "setup_static_config", "setup_dynamic_config",
    "parse_section_file",
    # state
    "ObservableNamespace", "StatefulNamespace", "StateField",
    "ObservableContext", "StateContext",
]


def __getattr__(name):
    # ── bootstrap ─────────────────────────────────
    if name == "setup_static_config":
        from .bootstrap import setup_static_config
        return setup_static_config
    if name == "setup_dynamic_config":
        from .bootstrap import setup_dynamic_config
        return setup_dynamic_config
    if name == "setup_config":
        # 兼容旧 API：只跑静态阶段
        from .bootstrap import setup_static_config
        return setup_static_config

    # ── declaration ───────────────────────────────
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

    # ── state ─────────────────────────────────────
    if name in (
            "ObservableNamespace", "StatefulNamespace", "StateField",
            "ObservableContext", "StateContext",
    ):
        from . import state
        return getattr(state, name)

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
