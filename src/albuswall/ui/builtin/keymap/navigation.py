#
"""焦点区域导航：``F6`` / ``Shift+F6`` 在界面主区域之间跳转。

纯键盘操作里最麻烦的不是「一个键触发一个动作」，而是**焦点现在在哪、
Tab 出去会掉到哪里**。这里把「主区域」显式列出来，用同一个规则遍历：

* 浮层打开时，只在**当前浮层**的内部区域之间循环（相册列表 / 源列表 /
  设置导航 / 详情导航…），再加上标题栏；
* 浮层关闭时，在「标题栏 → 内容网格」之间循环；
* 列表 / 视图的**内部**移动交给控件自己的方向键（QListView / 网格都支持），
  本模块只管区域级跳转。

谁用：``actions.py`` 的 ``focus.cycle`` / ``focus.cycle_previous``。
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QWidget

__all__ = [
    "activate",
    "cycle",
    "focus",
    "focus_widget",
    "is_within",
    "regions",
]


def focus_widget() -> Optional[QWidget]:
    """当前键盘焦点控件（没有焦点时为 None）。"""
    return QApplication.focusWidget()


def focus(widget: Optional[QWidget]) -> bool:
    """把键盘焦点交给 ``widget``；不可用时返回 False。"""
    if not _usable(widget):
        return False
    widget.setFocus(Qt.FocusReason.OtherFocusReason)
    return True


def activate(widget: Optional[QWidget]) -> bool:
    """「回车激活」：焦点在按钮上就点它；其它控件返回 False（让事件继续传）。

    Qt 的 QAbstractButton 默认只认 Space，Enter 在非对话框窗口里不触发点击；
    纯键盘操作时用户按回车的期望是「执行当前按钮」，这里补上这一层。
    """
    from PySide6.QtWidgets import QAbstractButton

    if isinstance(widget, QAbstractButton) and widget is not None:
        try:
            if widget.isEnabled():
                widget.click()
                return True
        except RuntimeError:
            return False
    return False


def is_within(widget: Optional[QWidget], ancestor: Optional[QWidget]) -> bool:
    """``widget`` 是否等于 ``ancestor`` 或它的后代。"""
    if widget is None or ancestor is None:
        return False
    node: Optional[QWidget] = widget
    while node is not None:
        if node is ancestor:
            return True
        try:
            node = node.parentWidget()
        except RuntimeError:  # 底层 C++ 对象已销毁
            return False
    return False


def _usable(widget: Optional[QWidget]) -> bool:
    """可接收键盘焦点的可见控件。"""
    if widget is None:
        return False
    try:
        return (
                widget.isVisible()
                and widget.isEnabled()
                and widget.focusPolicy() != Qt.FocusPolicy.NoFocus
        )
    except RuntimeError:
        return False


def _first_button(widget: Any) -> Optional[QWidget]:
    """设置页左侧导航按钮（``Setting.focus_first_navigation`` 的返回值不给对象）。"""
    finder = getattr(widget, "first_navigation_button", None)
    if callable(finder):
        return finder()
    return None


def _first_section(widget: Any) -> Optional[QWidget]:
    finder = getattr(widget, "first_section", None)
    if callable(finder):
        return finder()
    return None


def regions(ctx: Any) -> list[QWidget]:
    """当前上下文里可以按 F6 跳转的主区域（按视觉顺序）。"""
    window = getattr(ctx, "window", None)
    if window is None:
        return []

    overlay = getattr(window, "overlay", None)
    current = overlay.currentWidget() if overlay is not None else None

    candidates: list[Optional[QWidget]] = []

    title_bar = getattr(window, "title_bar", None)
    if title_bar is not None:
        candidates.append(getattr(title_bar, "album_button", None))

    if current is getattr(window, "album", None):
        candidates.append(getattr(window.album, "view", None))
        candidates.append(getattr(window.album, "close_button", None))
    elif current is getattr(window, "source", None):
        candidates.append(getattr(window.source, "view", None))
        detail_container = getattr(window.source, "detail_container", None)
        if detail_container is not None and detail_container.isVisible():
            candidates.append(getattr(window.source, "detail_widget", None))
        candidates.append(getattr(window.source, "close_button", None))
    elif current is getattr(window, "setting", None):
        candidates.append(_first_button(window.setting))
        candidates.append(_first_section(window.setting))
        candidates.append(getattr(window.setting, "close_button", None))
    elif current is getattr(window, "detail", None):
        detail = window.detail
        candidates.append(getattr(detail, "item_line_viewer", None))
        tag_bar = getattr(detail, "tag_bar", None)
        candidates.append(getattr(tag_bar, "tag_line_edit", None))
        candidates.append(getattr(tag_bar, "tags_view", None))
        panel = getattr(detail, "panel", None)
        if panel is not None and panel.isVisible():
            candidates.append(getattr(panel, "close_button", None))
        candidates.append(getattr(detail, "favourite_button", None))
        candidates.append(getattr(detail, "close_button", None))
    else:
        candidates.append(getattr(window, "content", None))

    ordered: list[QWidget] = []
    seen: set[int] = set()
    for widget in candidates:
        if not _usable(widget):
            continue
        if id(widget) in seen:
            continue
        seen.add(id(widget))
        ordered.append(widget)
    return ordered


def cycle(ctx: Any, step: int = 1) -> bool:
    """把焦点移到下一个（``step=1``）/ 上一个（``step=-1``）主区域。

    Returns:
        True: 已移动焦点；False: 当前上下文没有可跳转区域。
    """
    items = regions(ctx)
    if not items:
        return False

    focus = focus_widget()
    index = -1
    for position, region in enumerate(items):
        if is_within(focus, region):
            index = position
            break

    target = items[(index + step) % len(items)]
    target.setFocus(Qt.FocusReason.TabFocusReason)
    return True
