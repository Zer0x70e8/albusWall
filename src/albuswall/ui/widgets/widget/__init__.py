# -*- coding: utf-8 -*-
"""Primitive / reusable widgets (buttons, popups, overlays, delegates, ...).

Anything that is *not* a data view and *not* a layout helper belongs here.
Names are resolved lazily (see :mod:`albuswall.ui.widgets._lazy`), so asking
for ``BlurLabel`` does not import the other primitives.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .._lazy import install

_LAZY: dict[str, str] = {
    # buttons
    "CloseButton": "action_buttons",
    "FoldButton": "action_buttons",
    "MaximizeButton": "action_buttons",
    "MinimizeButton": "action_buttons",
    "PinButton": "action_buttons",
    # popups
    "AnchoredPopup": "anchored_popup",
    # overlays
    "BlurLabel": "blur_overlay_label",
    # containers
    "CollapsibleGroupBox": "collapsible_group_box",
    "PassthroughStack": "passthrough_stack_widget",
    # search
    "FloatingSearchBar": "floating_search_bar",
    "SearchSuggestionPopup": "floating_search_bar",
    # services
    "ImageLoader": "image_loader",
    "PixmapCache": "image_loader",
    "get_cache": "image_loader",
    "get_loader": "image_loader",
    # delegates
    "TagChipDelegate": "tag_chip_delegate",
}

__all__ = [
    # buttons
    "CloseButton",
    "FoldButton",
    "MaximizeButton",
    "MinimizeButton",
    "PinButton",
    # popups
    "AnchoredPopup",
    # overlays
    "BlurLabel",
    # containers
    "CollapsibleGroupBox",
    "PassthroughStack",
    # search
    "FloatingSearchBar",
    "SearchSuggestionPopup",
    # services
    "ImageLoader",
    "PixmapCache",
    "get_cache",
    "get_loader",
    # delegates
    "TagChipDelegate",
]

if TYPE_CHECKING:  # pragma: no cover - 仅供 IDE / 类型检查器
    from .action_buttons import (
        CloseButton,
        FoldButton,
        MaximizeButton,
        MinimizeButton,
        PinButton,
    )
    from .anchored_popup import AnchoredPopup
    from .blur_overlay_label import BlurLabel
    from .collapsible_group_box import CollapsibleGroupBox
    from .floating_search_bar import FloatingSearchBar, SearchSuggestionPopup
    from .image_loader import ImageLoader, PixmapCache, get_cache, get_loader
    from .passthrough_stack_widget import PassthroughStack
    from .tag_chip_delegate import TagChipDelegate

install(__name__, globals(), _LAZY)
