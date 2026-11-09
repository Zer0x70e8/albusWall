# -*- coding: utf-8 -*-
"""View widgets: composite controls that render data (tile, list, grid, ...).

表单控件不在本层：它已升格为独立子包 ``widget.form_widget``。

Names are resolved lazily (see :mod:`albuswall.ui.widgets._lazy`), so asking
for ``TileView`` does not import the other view modules.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .._lazy import install

_LAZY: dict[str, str] = {
    "ColumnListView": "column_list_view",
    "ImageViewer": "image_viewer",
    "SquareGridView": "square_grid",
    "TagChipView": "tag_chip_view",
    "TileView": "tile_view",
}

__all__ = [
    "ColumnListView",
    "ImageViewer",
    "SquareGridView",
    "TagChipView",
    "TileView",
]

if TYPE_CHECKING:  # pragma: no cover - 仅供 IDE / 类型检查器
    from .column_list_view import ColumnListView
    from .image_viewer import ImageViewer
    from .square_grid import SquareGridView
    from .tag_chip_view import TagChipView
    from .tile_view import TileView

install(__name__, globals(), _LAZY)
