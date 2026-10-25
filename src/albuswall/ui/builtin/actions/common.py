"""快捷键动作层的共用小工具（Qt 侧，只做只读查询）。

动作模块只依赖这里和 ``ui.builtin.library``，不反向依赖 ``shortcuts.py``，
避免注册期循环 import。
"""
from __future__ import annotations

from typing import Any

__all__ = [
    "album", "detail", "detail_is_open", "focus_in", "grid", "overlay",
    "presenters", "quick_preview", "slideshow", "viewer_presenter", "window",
]


def window(ctx) -> Any:
    return getattr(ctx, "window", None)


def presenters(ctx) -> Any:
    return getattr(ctx, "presenters", None)


def overlay(ctx) -> Any:
    return getattr(window(ctx), "overlay", None)


def current_overlay(ctx) -> Any:
    layer = overlay(ctx)
    return None if layer is None else layer.currentWidget()


def detail(ctx) -> Any:
    return getattr(window(ctx), "detail", None)


def grid(ctx) -> Any:
    return getattr(window(ctx), "content", None)


def detail_is_open(ctx) -> bool:
    """详情页是否在浮层栈顶。"""
    target = detail(ctx)
    return target is not None and current_overlay(ctx) is target


def quick_preview(ctx) -> bool:
    """是否处于快速预览模式。"""
    return bool(getattr(presenters(ctx), "is_quick_preview", False))


def slideshow(ctx) -> bool:
    """是否处于幻灯片模式。"""
    return bool(getattr(presenters(ctx), "is_slideshow", False))


def viewer_presenter(ctx) -> Any:
    return getattr(presenters(ctx), "viewer_presenter", None)


def album(ctx) -> Any:
    return getattr(presenters(ctx), "album_presenter", None)


def focus_in(ctx, *widgets) -> bool:
    """焦点是否落在给定控件（含子控件）里。"""
    from ..keymap import focus

    current = getattr(ctx, "focus", None)
    return any(focus.is_within(current, widget) for widget in widgets)
