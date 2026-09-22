#
""""""

import importlib
import importlib.metadata as md
import importlib.util
import logging
import pkgutil
import sys
import types
from pathlib import Path

_logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "albuswall.plugins"


def discover_package(package: str) -> None:
    """Built-in plugins: scan all submodules in the package and import them."""
    try:
        pkg = importlib.import_module(package)
    except ModuleNotFoundError:
        _logger.debug("plugin package %r  is not available, skipping.", package)
        return

    if not hasattr(pkg, "__path__"):
        _logger.warning("%r is not a package, cannot scan", package)
        return

    for _, mod_name, _ in pkgutil.iter_modules(pkg.__path__):
        if mod_name.startswith("_"):
            continue
        _import_quietly(f"{package}.{mod_name}")


# noinspection broad-exception
def discover_entry_points(group: str = ENTRY_POINT_GROUP) -> None:
    """Third-party pip packages: declare plugin modules via entry_points.

    Declare in pyproject.toml like this:

        [project.entry-points."albuswall.plugins"]
        foo = "albuswall_foo"

    The target can be a module (import it -> triggers the @declare inside),
    or a callable (call it directly -> it declares itself internally).
    """
    try:
        eps = md.entry_points(group=group)
    except TypeError:
        # Python 3.9 and earlier: entry_points() returns a dict
        # noinspection unresolved-references
        eps = md.entry_points().get(group, [])

    for ep in eps:
        try:
            target = ep.load()
        except Exception:
            _logger.exception("failed to load entry_point %r, skipped", ep.name)
            continue

        # Case 1: target is a module -> module-level @declare already ran on load
        # Case 2: target is a callable -> explicitly call it so it registers itself
        if isinstance(target, types.ModuleType):
            # Module: module-level @declare already ran on import, no action needed
            pass
        elif callable(target):
            # Function/class: explicitly call it so it registers itself
            try:
                target()
            except Exception:
                _logger.exception("failed to execute entry_point %r, skipped", ep.name)
        else:
            _logger.warning("entry_point %r points to an unsupported object %r", ep.name, type(target))


def discover_directory(path: Path,
                       prefix: str = "_albuswall_user_plugin") -> None:
    """User scripts: scan .py files in the directory and load them."""
    if not path.is_dir():
        _logger.debug("plugin dir %s does not exist, skipping", path)
        return

    for py in sorted(path.glob("*.py")):
        if py.name.startswith("_"):
            continue
        mod_name = f"{prefix}.{py.stem}"
        if mod_name in sys.modules:
            continue
        _import_from_path(mod_name, py)


def discover_all(
    builtin_package: str,
    user_dir: Path,
    entry_group: str = ENTRY_POINT_GROUP,
) -> None:
    """Aggregate all sources in order; must be called before activate."""
    discover_package(builtin_package)
    discover_entry_points(entry_group)
    discover_directory(user_dir)


# ---------- Internal ----------

# noinspection broad-exception
def _import_quietly(full_name: str) -> None:
    try:
        importlib.import_module(full_name)
        _logger.debug("loaded plugin module %r", full_name)
    except Exception:
        _logger.exception("failed to load plugin module %r, skipped", full_name)


# noinspection broad-exception
def _import_from_path(mod_name: str, path: Path) -> None:
    try:
        spec = importlib.util.spec_from_file_location(mod_name, path)
        if spec is None or spec.loader is None:
            _logger.warning("cannot construct spec for %s, skipping", path)
            return
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)
        _logger.debug("loaded user plugin %r from %s", mod_name, path)
    except Exception:
        sys.modules.pop(mod_name, None)
        _logger.exception("failed to load user plugin %s, skipped", path)
