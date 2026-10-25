#
"""内置动作：把「窗口里本来就有的按钮 / 菜单」变成可按键映射的功能。

这里的每个 handler 都只做**转发**：真正被调用的还是原来按钮 / presenter
的方法，键盘路径与鼠标路径不会分叉。新增可按键功能时在这里加一条
``KeyAction`` 即可（插件也可以在自己的模块里调 ``register_action``）。

约定
====

* handler 收到 :class:`~albuswall.ui.builtin.keymap.manager.KeymapContext`
  （``window`` / ``container`` / ``presenters``），不直接闭包持有窗口；
* 返回 ``False`` 表示这次按键没消费（例如详情页没有下一张），事件继续
  往目标控件传；
* 不确定的界面元素（缺控件 / presenter 没构造出来）一律安静跳过，不抛。
"""

from __future__ import annotations

from . import navigation
from .registry import KeyAction, register_action

__all__ = ["BUILTIN_ACTION_IDS", "ensure_builtin_actions", "register_builtin_actions"]

#: 内置动作 id（测试 / 文档引用；真正的清单以 registry 为准）
BUILTIN_ACTION_IDS: tuple[str, ...] = (
    "window.escape",
    "window.settings",
    "window.albums",
    "window.sources",
    "window.back",
    "window.menu",
    "window.minimize",
    "window.toggle_maximize",
    "window.quit",
    "focus.cycle",
    "focus.cycle_previous",
    "focus.grid",
    "focus.tags",
    "ui.activate",
    "ui.activate_keypad",
    "search.focus",
    "viewer.close",
    "viewer.previous",
    "viewer.next",
    "viewer.favorite",
    "viewer.trash",
    "viewer.information",
)


# ══════════════════════════════════════════════════
# 小工具
# ══════════════════════════════════════════════════
def _overlay(ctx):
    return getattr(getattr(ctx, "window", None), "overlay", None)


def _current_overlay(ctx):
    overlay = _overlay(ctx)
    return None if overlay is None else overlay.currentWidget()


def _detail_is_open(ctx) -> bool:
    """详情页是否在 overlay 栈顶（查看器动作的生效条件）。"""
    detail = getattr(getattr(ctx, "window", None), "detail", None)
    return detail is not None and _current_overlay(ctx) is detail


def _presenter(ctx, name: str):
    presenters = getattr(ctx, "presenters", None)
    return None if presenters is None else getattr(presenters, name, None)


def _show_overlay(ctx, target_name: str) -> bool:
    window = getattr(ctx, "window", None)
    overlay = getattr(window, "overlay", None)
    target = getattr(window, target_name, None)
    if overlay is None or target is None:
        return False
    overlay.setCurrentWidget(target)
    return True


def _focus_in(ctx, *containers) -> bool:
    """当前焦点是否落在这些控件（或它们的后代）里。"""
    focus = getattr(ctx, "focus", None)
    for container in containers:
        if navigation.is_within(focus, container):
            return True
    return False


def _not_typing(ctx) -> bool:
    """焦点不在文本输入框里（避免 Alt+Left 之类抢走输入框自己的组合）。"""
    from .keys import is_text_input

    return not is_text_input(getattr(ctx, "focus", None))


def _arrow_focus_ok(ctx) -> bool:
    """查看器翻页的生效条件：详情页打开，且焦点不在用自己的方向键的控件里。

    详情页底部导航条 / 标签栏 chip 列表都有自己的 ←/→ 行为，全局键位不能抢。
    """
    if not _detail_is_open(ctx):
        return False
    detail = getattr(ctx.window, "detail", None)
    if detail is None:
        return False
    return not _focus_in(
        ctx,
        getattr(detail, "item_line_viewer", None),
        getattr(detail, "tag_bar", None),
        getattr(detail, "panel", None),
    )


# ══════════════════════════════════════════════════
# 窗口级
# ══════════════════════════════════════════════════
def _open_settings(ctx) -> bool:
    return _show_overlay(ctx, "setting")


def _open_albums(ctx) -> bool:
    return _show_overlay(ctx, "album")


def _open_sources(ctx) -> bool:
    return _show_overlay(ctx, "source")


def _open_menu(ctx) -> bool:
    """用键盘打开标题栏「更多」菜单（菜单内部由 Qt 原生方向键控制）。"""
    title_bar = getattr(getattr(ctx, "window", None), "title_bar", None)
    button = getattr(title_bar, "extra_button", None)
    opener = getattr(button, "showMenu", None)
    if not callable(opener):
        return False
    opener()
    return True


def _close_layer(ctx, *, include_search: bool) -> bool:
    """按「最上面一层」收起：搜索栏 → 信息面板 → 详情页 → 浮层。"""
    window = getattr(ctx, "window", None)
    if window is None:
        return False

    if include_search:
        title_bar = getattr(window, "title_bar", None)
        search_bar = getattr(title_bar, "floating_search_bar", None)
        if search_bar is not None:
            if search_bar.has_suggestions_open():
                return False  # 交给搜索栏自己的 Esc 处理（只收建议列表）
            if getattr(search_bar, "is_expanded", False):
                search_bar.collapse()
                # 搜索栏收起后焦点回到内容区，避免落在即将隐藏的输入框上
                navigation.focus(getattr(window, "content", None))
                return True

    overlay = _overlay(ctx)
    detail = getattr(window, "detail", None)
    if overlay is not None and detail is not None \
            and overlay.currentWidget() is detail:
        panel = getattr(detail, "panel", None)
        if panel is not None and panel.isVisible():
            detail.show_information(False)
            return True
        detail.close_requested.emit()
        return True

    if overlay is not None and not overlay.is_blank():
        overlay.show_blank()
        return True
    return False


def _escape(ctx) -> bool:
    """Esc：按「最上面一层」逐级收起。

    顺序：搜索建议（由搜索栏自己处理）→ 搜索栏 → 详情信息面板 →
    详情页 → 其它浮层。都没有可收的就返回 False，让事件继续传递。
    """
    return _close_layer(ctx, include_search=True)


def _cycle_focus(ctx) -> bool:
    return navigation.cycle(ctx, 1)


def _cycle_focus_previous(ctx) -> bool:
    return navigation.cycle(ctx, -1)


def _focus_grid(ctx) -> bool:
    content = getattr(getattr(ctx, "window", None), "content", None)
    if content is None:
        return False
    return navigation.focus(content)


def _activate_focused(ctx) -> bool:
    """回车 / 小键盘回车：焦点在按钮上时等价于点击它。"""
    return navigation.activate(getattr(ctx, "focus", None))


def _focus_tags(ctx) -> bool:
    if not _detail_is_open(ctx):
        return False
    tag_bar = getattr(ctx.window.detail, "tag_bar", None)
    editor = getattr(tag_bar, "tag_line_edit", None)
    if editor is None:
        return False
    if not navigation.focus(editor):
        return False
    editor.selectAll()
    return True


def _back(ctx) -> bool:
    """收起当前浮层，回到相册 / 网格（与 Esc 共用逐层收起逻辑）。

    已经在空白页时返回 False：让按键继续往下传（例如输入框里按
    ``Alt+Left`` 应该还给它自己的撤销逻辑）。
    """
    return _close_layer(ctx, include_search=False)


def _minimize(ctx) -> bool:
    window = getattr(ctx, "window", None)
    if window is None:
        return False
    window.showMinimized()
    return True


def _toggle_maximize(ctx) -> bool:
    window = getattr(ctx, "window", None)
    if window is None:
        return False
    try:
        if window.isMaximized():
            window.showNormal()
        else:
            window.showMaximized()
    except Exception:  # noqa: BLE001
        return False
    return True


def _quit(ctx) -> bool:
    window = getattr(ctx, "window", None)
    if window is None:
        return False
    window.close()
    return True


def _focus_search(ctx) -> bool:
    title_bar = getattr(getattr(ctx, "window", None), "title_bar", None)
    bar = getattr(title_bar, "floating_search_bar", None)
    if bar is None:
        return False
    bar.expand()
    # 已经展开时 expand() 是空操作：仍要把焦点送回输入框
    line_edit = getattr(bar, "line_edit", None)
    if line_edit is not None:
        line_edit.setFocus()
        line_edit.selectAll()
    return True


# ══════════════════════════════════════════════════
# 详情页（只在 detail 位于栈顶时生效）
# ══════════════════════════════════════════════════
def _close_detail(ctx) -> bool:
    detail = getattr(getattr(ctx, "window", None), "detail", None)
    if detail is None:
        return False
    detail.close_requested.emit()
    return True


def _viewer_navigate(ctx, offset: int) -> bool:
    viewer = _presenter(ctx, "viewer_presenter")
    if viewer is None:
        return False
    return bool(viewer.navigate_relative(offset))


def _viewer_previous(ctx) -> bool:
    return _viewer_navigate(ctx, -1)


def _viewer_next(ctx) -> bool:
    return _viewer_navigate(ctx, +1)


def _click(ctx, button_name: str) -> bool:
    """点一下详情页里的同名按钮（复用按钮已有的信号接线）。"""
    detail = getattr(getattr(ctx, "window", None), "detail", None)
    button = getattr(detail, button_name, None)
    if button is None:
        return False
    button.click()
    return True


def _viewer_favorite(ctx) -> bool:
    return _click(ctx, "favourite_button")


def _viewer_trash(ctx) -> bool:
    return _click(ctx, "trash_button")


def _viewer_information(ctx) -> bool:
    return _click(ctx, "information_button")


# ══════════════════════════════════════════════════
# 登记
# ══════════════════════════════════════════════════
def register_builtin_actions(*, replace: bool = False) -> None:
    """登记全部内置动作（幂等：重复调用只补缺失项）。"""
    from .registry import get_action

    actions = (
        KeyAction(
            id="window.escape",
            label="Close the current layer",
            description="Close the topmost layer: suggestion list, search "
                        "bar, information panel, detail view or overlay.",
            group="window",
            order=5,
            default_keys="Escape",
            handler=_escape,
            allow_in_text_input=True,
        ),
        KeyAction(
            id="window.settings",
            label="Open settings",
            description="Open the settings overlay.",
            group="window",
            order=10,
            default_keys="Ctrl+,",
            handler=_open_settings,
        ),
        KeyAction(
            id="window.albums",
            label="Open album list",
            description="Open the album overlay and focus the album list.",
            group="window",
            order=15,
            default_keys="Ctrl+Shift+A",
            handler=_open_albums,
        ),
        KeyAction(
            id="window.sources",
            label="Open import sources",
            description="Open the import source overlay.",
            group="window",
            order=20,
            default_keys="Ctrl+Shift+O",
            handler=_open_sources,
        ),
        KeyAction(
            id="window.back",
            label="Back / close overlay",
            description="Close the current overlay and return to the "
                        "album grid.",
            group="window",
            order=30,
            default_keys="Alt+Left",
            handler=_back,
            when=_not_typing,
        ),
        KeyAction(
            id="window.menu",
            label="Open the More menu",
            description="Open the title bar menu (import sources, settings, "
                        "quit) and drive it with the arrow keys.",
            group="window",
            order=45,
            default_keys="Alt+M",
            handler=_open_menu,
        ),
        KeyAction(
            id="window.toggle_maximize",
            label="Toggle maximize",
            description="Maximize or restore the main window.",
            group="window",
            order=35,
            default_keys="",
            handler=_toggle_maximize,
        ),
        KeyAction(
            id="window.minimize",
            label="Minimize window",
            description="Minimize the main window.",
            group="window",
            order=40,
            default_keys="",
            handler=_minimize,
        ),
        KeyAction(
            id="window.quit",
            label="Quit albuswall",
            description="Close the application.",
            group="window",
            order=50,
            default_keys="Ctrl+Q",
            handler=_quit,
        ),
        KeyAction(
            id="ui.activate",
            label="Activate focused control",
            description="Press the focused button (the keyboard equivalent of "
                        "clicking it).",
            group="general",
            order=10,
            default_keys="Return",
            handler=_activate_focused,
        ),
        KeyAction(
            id="ui.activate_keypad",
            label="Activate focused control (keypad Enter)",
            description="Same as Activate focused control, for the numeric "
                        "keypad Enter key.",
            group="general",
            order=20,
            default_keys="Enter",
            handler=_activate_focused,
        ),
        KeyAction(
            id="focus.cycle",
            label="Focus next region",
            description="Move the keyboard focus to the next area (title "
                        "bar, list, grid, tag bar…).",
            group="focus",
            order=10,
            default_keys="F6",
            handler=_cycle_focus,
            allow_in_text_input=True,
        ),
        KeyAction(
            id="focus.cycle_previous",
            label="Focus previous region",
            description="Move the keyboard focus to the previous area.",
            group="focus",
            order=20,
            default_keys="Shift+F6",
            handler=_cycle_focus_previous,
            allow_in_text_input=True,
        ),
        KeyAction(
            id="focus.grid",
            label="Focus the image grid",
            description="Put the keyboard focus on the thumbnail grid: arrow "
                        "keys move the cursor, Enter opens the asset.",
            group="focus",
            order=30,
            default_keys="Ctrl+Shift+G",
            handler=_focus_grid,
        ),
        KeyAction(
            id="focus.tags",
            label="Focus tag input",
            description="Focus the tag input of the detail view.",
            group="focus",
            order=40,
            default_keys="Ctrl+Shift+T",
            handler=_focus_tags,
            when=_detail_is_open,
        ),
        KeyAction(
            id="search.focus",
            label="Focus search",
            description="Expand the search bar and put the cursor in it.",
            group="search",
            order=10,
            default_keys="Ctrl+F",
            handler=_focus_search,
            allow_in_text_input=True,
        ),
        KeyAction(
            id="viewer.close",
            label="Close detail view",
            description="Close the detail view (Esc also works).",
            group="viewer",
            order=10,
            default_keys="",
            handler=_close_detail,
            when=_detail_is_open,
        ),
        KeyAction(
            id="viewer.previous",
            label="Previous image",
            description="Show the previous asset while the detail view is "
                        "open.",
            group="viewer",
            order=20,
            default_keys="Left",
            handler=_viewer_previous,
            when=_arrow_focus_ok,
        ),
        KeyAction(
            id="viewer.next",
            label="Next image",
            description="Show the next asset while the detail view is open.",
            group="viewer",
            order=30,
            default_keys="Right",
            handler=_viewer_next,
            when=_arrow_focus_ok,
        ),
        KeyAction(
            id="viewer.favorite",
            label="Toggle favourite",
            description="Toggle the favourite state of the current asset.",
            group="viewer",
            order=40,
            default_keys="F",
            handler=_viewer_favorite,
            when=_detail_is_open,
        ),
        KeyAction(
            id="viewer.information",
            label="Toggle information panel",
            description="Show or hide the metadata panel of the detail view.",
            group="viewer",
            order=50,
            default_keys="I",
            handler=_viewer_information,
            when=_detail_is_open,
        ),
        KeyAction(
            id="viewer.trash",
            label="Trash / restore asset",
            description="Move the current asset to the trash, or restore it "
                        "when it is already trashed.",
            group="viewer",
            order=60,
            default_keys="",
            handler=_viewer_trash,
            when=_detail_is_open,
        ),
    )

    for action in actions:
        existing = get_action(action.id)
        if existing is not None and not replace:
            continue
        register_action(action, replace=replace)


def ensure_builtin_actions() -> None:
    """``register_builtin_actions`` 的别名（强调它会自动防重）。"""
    register_builtin_actions()
