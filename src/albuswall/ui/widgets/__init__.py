# -*- coding: utf-8 -*-
"""AlbusWall UI widgets.

Flat public surface: callers never need to know about the physical
``widget`` / ``view`` / ``layout`` split.

    from albuswall.ui.widgets import FormView, TileView, CloseButton
    from albuswall.ui.widgets import RowControl, RowSpec   # 表单四轴

Import cost stays minimal: every public name maps to the **exact** module that
defines it, so importing ``TileView`` only imports ``view/tile_view.py``
(and its own dependencies) — never the whole package. The sub-packages are
exposed as well, for callers that want to be explicit about the layer:

    from albuswall.ui.widgets.layout import Grid12Layout
    from albuswall.ui.widgets import view
    from albuswall.ui.widgets.widget.form_widget import FormView

表单控件是一个独立子包（``widget.form_widget``）：``FormView`` 与四轴枚举 /
预设（``RowControl`` / ``RowAccess`` / ``ResetPosition`` / ``ValidationMode`` /
``RowSpec``）在顶层同时也直接可达。

Add a new class: put it in the matching sub-package, list it in that
sub-package's ``_LAZY`` / ``__all__`` **and** in ``_LAZY`` below.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._lazy import install

# ============================================================
# 公开名 -> 定义它的模块（相对本包）。刻意写死，不在 import 时扫描目录：
# 只有真正取用的那个名字对应的模块才会被 import。
# ============================================================
_LAZY: dict[str, str] = {
    # ---- layout ----
    "ColumnLayoutCalculator": "layout.column_layout_calculator",
    "Grid12Layout": "layout.grid12_layout",
    "SearchBarLayoutPlaceholder": "layout.search_bar_layout",
    # ---- view ----
    "ColumnListView": "view.column_list_view",
    "ImageViewer": "view.image_viewer",
    "SquareGridView": "view.square_grid",
    "TagChipView": "view.tag_chip_view",
    "TileView": "view.tile_view",
    # ---- widget (primitives) ----
    "CloseButton": "widget.action_buttons",
    "FoldButton": "widget.action_buttons",
    "MaximizeButton": "widget.action_buttons",
    "MinimizeButton": "widget.action_buttons",
    "PinButton": "widget.action_buttons",
    "AnchoredPopup": "widget.anchored_popup",
    "BlurLabel": "widget.blur_overlay_label",
    "CollapsibleGroupBox": "widget.collapsible_group_box",
    "FloatingSearchBar": "widget.floating_search_bar",
    "SearchSuggestionPopup": "widget.floating_search_bar",
    "ImageLoader": "widget.image_loader",
    "PixmapCache": "widget.image_loader",
    "get_cache": "widget.image_loader",
    "get_loader": "widget.image_loader",
    "PassthroughStack": "widget.passthrough_stack_widget",
    "TagChipDelegate": "widget.tag_chip_delegate",
    "MarqueeLabel": "widget.marquee_label",
    "TagLabel": "widget.tag_label",
    # ---- form (widget.form_widget 子包) ----
    "FormView": "widget.form_widget",
    "RowHandle": "widget.form_widget",
    "ValidationError": "widget.form_widget",
    "RowControl": "widget.form_widget",
    "RowAccess": "widget.form_widget",
    "BrowseKind": "widget.form_widget",
    "ResetPosition": "widget.form_widget",
    "ValidationMode": "widget.form_widget",
    "RowSpec": "widget.form_widget",
}

#: 顶层子包（按名字导入也行：``from albuswall.ui.widgets import view``）
_SUBPACKAGES: tuple[str, ...] = ("layout", "view", "widget")

__all__ = [
    "AnchoredPopup",
    "BlurLabel",
    "BrowseKind",
    "CloseButton",
    "CollapsibleGroupBox",
    "ColumnLayoutCalculator",
    "ColumnListView",
    "FloatingSearchBar",
    "FoldButton",
    "FormView",
    "Grid12Layout",
    "ImageLoader",
    "ImageViewer",
    "MarqueeLabel",
    "MaximizeButton",
    "MinimizeButton",
    "PassthroughStack",
    "PinButton",
    "PixmapCache",
    "ResetPosition",
    "RowAccess",
    "RowControl",
    "RowHandle",
    "RowSpec",
    "SearchBarLayoutPlaceholder",
    "SearchSuggestionPopup",
    "SquareGridView",
    "TagChipDelegate",
    "TagChipView",
    "TagLabel",
    "TileView",
    "ValidationError",
    "ValidationMode",
    "get_cache",
    "get_loader",
    "layout",
    "view",
    "widget",
]

# ============================================================
# 仅类型检查器 / IDE 可见，运行时不会执行
# ============================================================
if TYPE_CHECKING:  # pragma: no cover
    from . import layout, view, widget
    from .layout import (
        ColumnLayoutCalculator,
        Grid12Layout,
        SearchBarLayoutPlaceholder,
    )
    from .view import (
        ColumnListView,
        ImageViewer,
        SquareGridView,
        TagChipView,
        TileView,
    )
    from .widget import (
        AnchoredPopup,
        BlurLabel,
        CloseButton,
        CollapsibleGroupBox,
        FloatingSearchBar,
        FoldButton,
        ImageLoader,
        MarqueeLabel,
        MaximizeButton,
        MinimizeButton,
        PassthroughStack,
        PinButton,
        PixmapCache,
        SearchSuggestionPopup,
        TagChipDelegate,
        TagLabel,
        get_cache,
        get_loader,
    )
    from .widget.form_widget import (
        BrowseKind,
        FormView,
        ResetPosition,
        RowAccess,
        RowControl,
        RowHandle,
        RowSpec,
        ValidationError,
        ValidationMode,
    )

install(__name__, globals(), _LAZY, _SUBPACKAGES)
