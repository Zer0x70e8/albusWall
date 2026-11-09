# -*- coding: utf-8 -*-
"""Primitive / reusable widgets (buttons, popups, overlays, labels, delegates).

Anything that is *not* a data view and *not* a layout helper belongs here.
Names are resolved lazily (see :mod:`albuswall.ui.widgets._lazy`), so asking
for ``BlurLabel`` does not import the other primitives.

``form_widget`` 是本层的子包（表单控件自成一体），按子包方式暴露。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .._lazy import install

# ============================================================
# 公开名 -> 定义它的模块（相对本包）
# ============================================================
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
    # labels
    "MarqueeLabel": "marquee_label",
    "TagLabel": "tag_label",
}

#: 子包（按名字导入也行：``from albuswall.ui.widgets.widget import form_widget``）
_SUBPACKAGES: tuple[str, ...] = ("form_widget",)

__all__ = [
    "AnchoredPopup",
    "BlurLabel",
    "CloseButton",
    "CollapsibleGroupBox",
    "FloatingSearchBar",
    "FoldButton",
    "ImageLoader",
    "MarqueeLabel",
    "MaximizeButton",
    "MinimizeButton",
    "PassthroughStack",
    "PinButton",
    "PixmapCache",
    "SearchSuggestionPopup",
    "TagChipDelegate",
    "TagLabel",
    "form_widget",
    "get_cache",
    "get_loader",
]

if TYPE_CHECKING:  # pragma: no cover - 仅供 IDE / 类型检查器
    from . import form_widget
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
    from .marquee_label import MarqueeLabel
    from .passthrough_stack_widget import PassthroughStack
    from .tag_chip_delegate import TagChipDelegate
    from .tag_label import TagLabel

install(__name__, globals(), _LAZY, _SUBPACKAGES)
