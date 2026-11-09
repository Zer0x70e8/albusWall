# -*- coding: utf-8 -*-
"""Layout helpers: pure layout objects and layout-metric value objects.

Nothing here paints or owns data; these modules only measure / place.
Names are resolved lazily (see :mod:`albuswall.ui.widgets._lazy`), so asking
for ``Grid12Layout`` does not import the other layout modules.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .._lazy import install

_LAZY: dict[str, str] = {
    "ColumnLayoutCalculator": "column_layout_calculator",
    "Grid12Layout": "grid12_layout",
    "SearchBarLayoutPlaceholder": "search_bar_layout",
}

__all__ = [
    "ColumnLayoutCalculator",
    "Grid12Layout",
    "SearchBarLayoutPlaceholder",
]

if TYPE_CHECKING:  # pragma: no cover - 仅供 IDE / 类型检查器
    from .column_layout_calculator import ColumnLayoutCalculator
    from .grid12_layout import Grid12Layout
    from .search_bar_layout import SearchBarLayoutPlaceholder

install(__name__, globals(), _LAZY)
