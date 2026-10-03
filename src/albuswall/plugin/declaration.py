#
""""""

from typing import overload, Callable, Any, TYPE_CHECKING

from .manager import PluginManager

if TYPE_CHECKING:
    # noinspection unused-imports
    from albuswall.core import Container

_PluginFn = Callable[["Container"], Any]
_Decorator = Callable[[_PluginFn], _PluginFn]


# noinspection overloads
@overload
def declare(func: _PluginFn) -> _PluginFn: ...


# noinspection overloads
@overload
def declare(name: str) -> _Decorator: ...


def declare(arg0: str | _PluginFn):
    if isinstance(arg0, str):
        def w(func: _PluginFn) -> _PluginFn:
            # noinspection bad-argument-type
            PluginManager.registry(arg0, func)
            return func

        return w
    elif callable(arg0):
        PluginManager.registry(arg0.__name__, arg0)
        return arg0
    raise ValueError(f"unsupported declare() argument: {arg0!r}")


def ui_loader(name: str) -> _Decorator:
    """Declare a UI loader.

    Parallel to `@declare`, but registered in PluginManager's UI table,
    and triggered by `albuswall.ui.bootstrap.registry_ui` after configuration is ready.

    Typical usage:

        @declare(PLUGIN_ID)
        def setup(container):
            from ._config import WindowPresenterConfs  # noqa: F401

        @ui_loader("builtin")
        def load(container):
            if container.get("_config").static.ui.no_builtin:
                return None
            from .application import Application
            return Application

    The callback's return value is returned as-is by `activate_ui` (usually a UI class,
    or None to skip); `registry_ui` mainly still relies boot `_REGISTRY` populated by
    `@register_ui` to get the class.
    """
    def w(func: _PluginFn) -> _PluginFn:
        PluginManager.registry_ui(name, func)
        return func
    return w
