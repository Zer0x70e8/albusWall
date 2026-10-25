"""焦点导航（Qt）：``F6`` / ``Shift+F6`` 在主区域之间循环，``Enter`` 激活按钮。"""
from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractButton, QApplication, QWidget

__all__ = ["activate", "cycle", "focus", "focus_widget", "is_within", "regions"]


def focus_widget() -> QWidget | None:
    return QApplication.focusWidget()


def _usable(widget: QWidget | None) -> bool:
    if widget is None:
        return False
    try:
        return (
            widget.isVisible()
            and widget.isEnabled()
            and widget.focusPolicy() != Qt.FocusPolicy.NoFocus
        )
    except RuntimeError:  # 底层对象已销毁
        return False


def focus(widget: QWidget | None) -> bool:
    if not _usable(widget):
        return False
    widget.setFocus(Qt.FocusReason.OtherFocusReason)
    return True


def activate(widget: QWidget | None) -> bool:
    """回车激活：焦点在按钮上就点它（Qt 原生只认 Space）。"""
    if isinstance(widget, QAbstractButton) and widget.isEnabled():
        try:
            widget.click()
            return True
        except RuntimeError:
            return False
    return False


def is_within(widget: QWidget | None, ancestor: QWidget | None) -> bool:
    if widget is None or ancestor is None:
        return False
    node: QWidget | None = widget
    while node is not None:
        if node is ancestor:
            return True
        try:
            node = node.parentWidget()
        except RuntimeError:
            return False
    return False


def _first(widget: Any, name: str) -> QWidget | None:
    finder = getattr(widget, name, None)
    return finder() if callable(finder) else None


def regions(ctx: Any) -> list[QWidget]:
    """当前上下文里可按 F6 跳转的主区域（视觉顺序）。"""
    window = getattr(ctx, "window", None)
    if window is None:
        return []
    overlay = getattr(window, "overlay", None)
    current = overlay.currentWidget() if overlay is not None else None
    candidates: list[QWidget | None] = []
    title_bar = getattr(window, "title_bar", None)
    if title_bar is not None:
        candidates.append(getattr(title_bar, "album_button", None))

    if current is getattr(window, "album", None):
        candidates += [getattr(window.album, "view", None),
                       getattr(window.album, "close_button", None)]
    elif current is getattr(window, "source", None):
        candidates.append(getattr(window.source, "view", None))
        detail = getattr(window.source, "detail_container", None)
        if detail is not None and detail.isVisible():
            candidates.append(getattr(window.source, "detail_widget", None))
        candidates.append(getattr(window.source, "close_button", None))
    elif current is getattr(window, "setting", None):
        candidates += [_first(window.setting, "first_navigation_button"),
                       _first(window.setting, "first_section"),
                       getattr(window.setting, "close_button", None)]
    elif current is getattr(window, "detail", None):
        detail = window.detail
        candidates += [getattr(detail, "item_line_viewer", None),
                       getattr(getattr(detail, "tag_bar", None), "tag_line_edit", None),
                       getattr(getattr(detail, "tag_bar", None), "tags_view", None)]
        panel = getattr(detail, "panel", None)
        if panel is not None and panel.isVisible():
            candidates.append(getattr(panel, "close_button", None))
        candidates += [getattr(detail, "favourite_button", None),
                       getattr(detail, "close_button", None)]
    else:
        candidates.append(getattr(window, "content", None))

    ordered: list[QWidget] = []
    seen: set[int] = set()
    for widget in candidates:
        if _usable(widget) and id(widget) not in seen:
            seen.add(id(widget))
            ordered.append(widget)  # type: ignore[arg-type]
    return ordered


def cycle(ctx: Any, step: int = 1) -> bool:
    """把焦点移到下一个（``1``）/ 上一个（``-1``）主区域。"""
    items = regions(ctx)
    if not items:
        return False
    current = focus_widget()
    index = -1
    for position, region in enumerate(items):
        if is_within(current, region):
            index = position
            break
    items[(index + step) % len(items)].setFocus(Qt.FocusReason.TabFocusReason)
    return True
