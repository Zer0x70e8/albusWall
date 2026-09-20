#
""""""

import logging
from typing import Callable, Iterable, TYPE_CHECKING, Any, Dict, Tuple

if TYPE_CHECKING:
    from albuswall.core import Container

_logger = logging.getLogger(__name__)


# noinspection string-conversion-without-dunder-method
class PluginManager:
    _instance: "PluginManager | None" = None
    declare: Dict[str, Callable[["Container"], Any]] = {}
    # ui_name -> (loader, owner_plugin_name)
    declare_ui: Dict[str, Tuple[Callable[["Container"], Any], str]] = {}

    # module name -> plugin name declared by that module (used for @ui_loader ownership checks)
    _module_owner: Dict[str, str] = {}
    # The set of plugins allowed to be enabled, determined after activate();
    # None means all are allowed.
    _enabled: set[str] | None = None
    _activated: bool = False

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    # ---------- Declaration phase ----------
    # noinspection bad-index
    @classmethod
    def registry(cls, name: str, call_: Callable[["Container"], Any]) -> None:
        if cls._activated:
            raise RuntimeError(
                f"cannot register plugin {name!r} after activation")
        if name in cls.declare:
            raise ValueError(f"duplicate plugin: {name!r}")
        cls.declare[name] = call_

        module_name = getattr(call_, "__module__", None)
        if module_name is None:
            raise RuntimeError(
                f"declaration object {call_!r} for plugin {name!r} has no __module__, "
                f"cannot establish module ownership; make sure you passed a module-level function."
            )
        cls._module_owner[module_name] = name

    # noinspection bad-argument-type,bad-index
    @classmethod
    def registry_ui(cls, name: str,
                    call_: Callable[["Container"], Any]) -> None:
        if cls._activated:
            raise RuntimeError(
                f"cannot register ui loader {name!r} after activation")
        if name in cls.declare_ui:
            raise ValueError(f"duplicate ui loader: {name!r}")

        module_name = getattr(call_, "__module__", None)
        owner = cls._module_owner.get(module_name) if module_name else None
        if owner is None:
            raise RuntimeError(
                f"@ui_loader({name!r}) is defined in module {module_name!r}, "
                f"but that module has not declared any plugin first with @declare; "
                f"a UI loader must belong to a plugin to be controlled by enable/disable."
            )
        cls.declare_ui[name] = (call_, owner)

    # ---------- Activation phase ----------
    @classmethod
    def activate(cls,
                 container: "Container",
                 enabled: Iterable[str] | None = None,
                 disabled: Iterable[str] | None = None) -> None:
        """Activate plugins in declaration order.

        Three mutually exclusive modes:

        - neither given  -> activate all
        - enabled=[...]  -> whitelist: activate only those in the list; undeclared names in the list cause an error
        - disabled=[...] -> blacklist: activate those outside the list; undeclared names in the list are only logged
        """
        if cls._activated:
            raise RuntimeError("plugins already activated")
        if enabled is not None and disabled is not None:
            raise ValueError("enabled and disabled cannot both be specified")

        allow = cls._resolve_allow(enabled, disabled)
        cls._enabled = allow
        cls._activated = True

        for name, factory in cls.declare.items():
            if allow is not None and name not in allow:
                _logger.debug("plugin %r skipped", name)
                continue
            # noinspection broad-exception
            try:
                factory(container)
            except Exception:
                _logger.exception(
                    "plugin %r failed during activation", name)

    @classmethod
    def _resolve_allow(cls,
                       enabled: Iterable[str] | None,
                       disabled: Iterable[str] | None,
                       ) -> set[str] | None:
        declared = cls.declare.keys()

        if enabled is not None:
            allow = set(enabled)
            unknown = allow - declared
            if unknown:
                raise KeyError(
                    f"unknown plugins in enabled list: {sorted(unknown)}")
            return allow

        if disabled is not None:
            deny = set(disabled)
            unknown = deny - declared
            if unknown:
                _logger.debug(
                    "disabled list contains unknown plugins, ignored: %s",
                    sorted(unknown))
            return set(declared) - deny

        return None

    # ---------- UI activation phase ----------
    @classmethod
    def activate_ui(cls, container: "Container", name: str) -> Any:
        """Run a UI's load callback.

        - UI name not declared -> KeyError
        - the plugin owning the UI is filtered out by enabled/disabled -> return None (log debug)
        """
        entry = cls.declare_ui.get(name)
        if entry is None:
            raise KeyError(f"unknown ui: {name!r}")
        loader, owner = entry

        if cls._enabled is not None and owner not in cls._enabled:
            _logger.debug("ui %r skipped (owning plugin %r disabled)",
                          name, owner)
            return None

        _logger.debug("activating ui loader %r (owner=%r)", name, owner)
        return loader(container)

    @classmethod
    def list_ui(cls) -> list[str]:
        return list(cls.declare_ui.keys())

    @classmethod
    def instance(cls) -> "PluginManager":
        return cls()

    # noinspection method-overriding
    @classmethod
    def __str__(cls):
        if cls._enabled is None:
            enabled_repr = "<all>"
        else:
            enabled_repr = sorted(cls._enabled)
        return "\n".join((
            f"{cls.__name__}",
            f"\tenabled={enabled_repr}",
            "\tdeclare(",
            *[f"\t\t{k}: {type(v).__name__}({v})"
              for k, v in cls.declare.items()],
            "\tdeclare_ui(",
            *[f"\t\t{k}: {type(v).__name__}({v}) owner={owner!r}"
              for k, (v, owner) in cls.declare_ui.items()],
            ")",
        ))
