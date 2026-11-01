#
"""``albuswall.ui.anims`` —— 纯 Qt 的动画策略组件（不含业务）。"""

from .collapse_animation import CollapseAnimation, HeightSlideAnimation
from .search_bar_anim import GeometryAnimator
from .search_bar_anim_protocol import SearchAnimator

__all__ = [
    "CollapseAnimation",
    "GeometryAnimator",
    "HeightSlideAnimation",
    "SearchAnimator",
]
