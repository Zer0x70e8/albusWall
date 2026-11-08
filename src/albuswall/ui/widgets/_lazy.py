"""PEP 562 lazy loader shared by ``albuswall.ui.widgets`` and its sub-packages.

Every ``__init__`` in this package declares an exact ``name -> module`` map
instead of importing its modules eagerly. That keeps the public surface flat
*and* the import cost minimal::

    from albuswall.ui.widgets import TileView
    # -> imports view/tile_view.py (+ its own deps), not the other 17 modules

Usage inside an ``__init__.py``::

    _LAZY = {"TileView": "view.tile_view", ...}
    __all__ = [*_LAZY]
    _lazy.install(__name__, globals(), _LAZY)

Adding a class therefore touches exactly one ``__init__`` file (the map plus
``__all__``) and nothing else.
"""

from __future__ import annotations

import importlib
from typing import Any, Mapping


def install(
    package: str,
    namespace: dict[str, Any],
    mapping: Mapping[str, str],
    subpackages: tuple[str, ...] = (),
) -> None:
    """Install ``__getattr__`` / ``__dir__`` (PEP 562) on a package module.

    :param package:     the package's ``__name__`` (e.g. ``albuswall.ui.widgets``).
    :param namespace:   the package's ``globals()`` — resolved names are cached there.
    :param mapping:     public name -> module path relative to ``package``.
    :param subpackages: sub-package names that may be imported by name as well.
    """

    def __getattr__(name: str) -> Any:
        if name in subpackages:
            module = importlib.import_module(f"{package}.{name}")
            namespace[name] = module
            return module

        target = mapping.get(name)
        if target is None:
            raise AttributeError(f"module {package!r} has no attribute {name!r}")

        # 只 import 定义该名字的那一个模块
        value = getattr(importlib.import_module(f"{package}.{target}"), name)
        namespace[name] = value  # 命中缓存后不再走 __getattr__
        return value

    def __dir__() -> list[str]:
        return sorted(set(namespace) | set(mapping) | set(subpackages))

    namespace["__getattr__"] = __getattr__
    namespace["__dir__"] = __dir__
