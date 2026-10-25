#
"""内置动作：把窗口里本来就有的按钮 / 菜单 / presenter 方法暴露成可绑定按键。

每个 handler 只做转发（键盘路径与鼠标路径不分叉）；缺控件 / presenter 时
安静返回 ``False``。动作全部用 ``keymap.shortcut`` 声明，导入即登记。
"""
from __future__ import annotations

from albuswall.log import getLogger

from .keymap import Scope, focus, shortcut

__all__ = ["BUILTIN_ACTION_IDS"]

_logger = getLogger(__name__)

BUILTIN_ACTION_IDS: tuple[str, ...] = (
    "window.escape", "window.settings", "window.albums", "window.sources",
    "window.back", "window.menu", "window.minimize", "window.toggle_maximize",
    "window.quit", "window.open_page", "window.open_keybindings",
    "focus.cycle", "focus.cycle_previous", "focus.grid", "focus.tags",
    "ui.activate", "search.focus",
    "viewer.close", "viewer.previous", "viewer.next", "viewer.favorite",
    "viewer.trash", "viewer.information",
)


# ---------------------------------------------------------------- 小工具 --
def _overlay(ctx):
    return getattr(getattr(ctx, "window", None), "overlay", None)


def _current_overlay(ctx):
    overlay = _overlay(ctx)
    return None if overlay is None else overlay.currentWidget()


def _detail_is_open(ctx) -> bool:
    detail = getattr(getattr(ctx, "window", None), "detail", None)
    return detail is not None and _current_overlay(ctx) is detail


def _presenter(ctx, name: str):
    presenters = getattr(ctx, "presenters", None)
    return None if presenters is None else getattr(presenters, name, None)


def _show_overlay(ctx, name: str) -> bool:
    window = getattr(ctx, "window", None)
    overlay = getattr(window, "overlay", None)
    target = getattr(window, name, None)
    if overlay is None or target is None:
        return False
    overlay.setCurrentWidget(target)
    return True


def _focus_in(ctx, *widgets) -> bool:
    current = getattr(ctx, "focus", None)
    return any(focus.is_within(current, w) for w in widgets)


def _arrow_focus_ok(ctx) -> bool:
    """翻页键只在详情页打开、且焦点不在用自己的方向键的控件里时生效。"""
    if not _detail_is_open(ctx):
        return False
    detail = getattr(ctx.window, "detail", None)
    return detail is not None and not _focus_in(
        ctx,
        getattr(detail, "item_line_viewer", None),
        getattr(detail, "tag_bar", None),
        getattr(detail, "panel", None),
    )


def _jump_to_settings_page(ctx, page_id: str) -> bool:
    setting = getattr(getattr(ctx, "window", None), "setting", None)
    opener = getattr(setting, "open_page", None)
    if not callable(opener):
        return False
    try:
        return bool(opener(page_id))
    except Exception:  # noqa: BLE001
        _logger.exception("jump to settings page %r failed", page_id)
        return False


def _close_layer(ctx, *, include_search: bool) -> bool:
    window = getattr(ctx, "window", None)
    if window is None:
        return False

    # 显式模式优先：幻灯片 → 快速预览（再往下才是详情 / 面板 / 浮层）。
    # 模式状态在 PresenterManager 里，快捷键层保持无状态。
    escape_mode = getattr(getattr(ctx, "presenters", None), "escape_layer", None)
    if callable(escape_mode):
        try:
            if escape_mode():
                return True
        except Exception:  # noqa: BLE001  模式异常不能挡住 Esc 收层
            _logger.exception("escape_layer failed")

    if include_search:
        search_bar = getattr(getattr(window, "title_bar", None),
                             "floating_search_bar", None)
        if search_bar is not None:
            if search_bar.has_suggestions_open():
                return False  # 只收建议列表这件事交给搜索栏自己
            if getattr(search_bar, "is_expanded", False):
                search_bar.collapse()
                focus.focus(getattr(window, "content", None))
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


# ---------------------------------------------------------------- 窗口 --
@shortcut("window.escape", label="Close the current layer",
          description="Close the topmost layer: search bar, information "
                      "panel, detail view or overlay.",
          keys="Escape", component="window", order=5,
          allow_in_text_input=True)
def _escape(ctx) -> bool:
    return _close_layer(ctx, include_search=True)


@shortcut("window.settings", label="Open settings",
          description="Open the settings overlay.",
          keys="Ctrl+,", component="window", order=10,
          allow_in_text_input=True)
def _open_settings(ctx) -> bool:
    return _show_overlay(ctx, "setting")


@shortcut("window.albums", label="Open album list",
          description="Open the album overlay.",
          keys="Ctrl+Shift+A", component="window", order=15,
          allow_in_text_input=True)
def _open_albums(ctx) -> bool:
    return _show_overlay(ctx, "album")


@shortcut("window.sources", label="Open import sources",
          description="Open the import source overlay.",
          keys="Ctrl+Shift+O", component="window", order=20,
          allow_in_text_input=True)
def _open_sources(ctx) -> bool:
    return _show_overlay(ctx, "source")


@shortcut("window.open_page", label="Open a page",
          description="Open an overlay or jump to a settings page. Page ids: "
                      "\"settings:<section>\", \"albums\", \"sources\", \"detail\".",
          component="window", scope=Scope.GLOBAL, order=25,
          arguments={"page": ""})
def _open_page(ctx, page: str = "") -> bool:
    """全局导航样板：同一个动作配不同 ``page`` 参数绑到不同键上。"""
    target = str(page or "").strip()
    if not target:
        return _show_overlay(ctx, "setting")
    kind, _, rest = target.partition(":")
    kind, rest = kind.strip().casefold(), rest.strip()
    overlays = {
        "albums": "album", "album": "album", "sources": "source",
        "source": "source", "import": "source", "detail": "detail",
        "viewer": "detail",
    }
    if kind in overlays:
        return _show_overlay(ctx, overlays[kind])
    section = rest if kind in ("settings", "setting", "page") else target
    if not _show_overlay(ctx, "setting"):
        return False
    if section and not _jump_to_settings_page(ctx, section):
        _logger.warning("open_page: unknown settings page %r", section)
    return True


@shortcut("window.open_keybindings", label="Open the key bindings page",
          description="Jump straight into the key bindings page.",
          keys="Ctrl+Alt+K", component="window.setting.keybindings",
          scope=Scope.GLOBAL, order=10, allow_in_text_input=True)
def _open_keybindings(ctx) -> bool:
    return _open_page(ctx, page="settings:keybindings")


@shortcut("window.back", label="Back / close overlay",
          description="Close the current overlay and return to the grid.",
          keys="Alt+Left", component="window", order=30)
def _back(ctx) -> bool:
    return _close_layer(ctx, include_search=False)


@shortcut("window.toggle_maximize", label="Toggle maximize",
          description="Maximize or restore the main window.",
          component="window", order=35)
def _toggle_maximize(ctx) -> bool:
    window = getattr(ctx, "window", None)
    if window is None:
        return False
    if window.isMaximized():
        window.showNormal()
    else:
        window.showMaximized()
    return True


@shortcut("window.minimize", label="Minimize window",
          description="Minimize the main window.",
          component="window", order=40)
def _minimize(ctx) -> bool:
    window = getattr(ctx, "window", None)
    if window is None:
        return False
    window.showMinimized()
    return True


@shortcut("window.menu", label="Open the More menu",
          description="Open the title bar menu and drive it with the arrows.",
          keys="Alt+M", component="window.titlebar", order=20)
def _open_menu(ctx) -> bool:
    button = getattr(getattr(getattr(ctx, "window", None), "title_bar", None),
                     "extra_button", None)
    opener = getattr(button, "showMenu", None)
    if not callable(opener):
        return False
    opener()
    return True


@shortcut("window.quit", label="Quit albuswall",
          description="Close the application.",
          keys="Ctrl+Q", component="window", scope=Scope.GLOBAL, order=50,
          allow_in_text_input=True)
def _quit(ctx) -> bool:
    window = getattr(ctx, "window", None)
    if window is None:
        return False
    window.close()
    return True


# ---------------------------------------------------------------- 焦点 --
@shortcut("focus.cycle", label="Focus next region",
          description="Move the keyboard focus to the next area.",
          keys="F6", component="ui", order=10, allow_in_text_input=True)
def _cycle_focus(ctx) -> bool:
    return focus.cycle(ctx, 1)


@shortcut("focus.cycle_previous", label="Focus previous region",
          description="Move the keyboard focus to the previous area.",
          keys="Shift+F6", component="ui", order=20, allow_in_text_input=True)
def _cycle_focus_previous(ctx) -> bool:
    return focus.cycle(ctx, -1)


@shortcut("focus.grid", label="Focus the image grid",
          description="Put the keyboard focus on the thumbnail grid.",
          keys="Ctrl+Shift+G", component="window.content", order=10)
def _focus_grid(ctx) -> bool:
    return focus.focus(getattr(getattr(ctx, "window", None), "content", None))


@shortcut("focus.tags", label="Focus tag input",
          description="Focus the tag input of the detail view.",
          keys="Ctrl+Shift+T", component="window.detail.tagbar", order=10,
          when=_detail_is_open)
def _focus_tags(ctx) -> bool:
    if not _detail_is_open(ctx):
        return False
    editor = getattr(getattr(ctx.window.detail, "tag_bar", None),
                     "tag_line_edit", None)
    if not focus.focus(editor):
        return False
    editor.selectAll()
    return True


@shortcut("ui.activate", label="Activate focused control",
          description="Press the focused button (keyboard equivalent of a click).",
          keys=("Return", "Enter"), component="ui", order=30)
def _activate(ctx) -> bool:
    return focus.activate(getattr(ctx, "focus", None))


# ---------------------------------------------------------------- 搜索 --
@shortcut("search.focus", label="Focus search",
          description="Expand the search bar and put the cursor in it.",
          keys="Ctrl+F", component="window.titlebar.search", order=10,
          allow_in_text_input=True)
def _focus_search(ctx) -> bool:
    bar = getattr(getattr(getattr(ctx, "window", None), "title_bar", None),
                  "floating_search_bar", None)
    if bar is None:
        return False
    bar.expand()
    line_edit = getattr(bar, "line_edit", None)
    if line_edit is not None:
        line_edit.setFocus()
        line_edit.selectAll()
    return True


# ---------------------------------------------------------------- 详情页 --
@shortcut("viewer.close", label="Close detail view",
          description="Close the detail view (Esc also works).",
          component="window.detail", order=10, when=_detail_is_open)
def _close_detail(ctx) -> bool:
    detail = getattr(getattr(ctx, "window", None), "detail", None)
    if detail is None:
        return False
    detail.close_requested.emit()
    return True


def _navigate(ctx, offset: int) -> bool:
    viewer = _presenter(ctx, "viewer_presenter")
    return bool(viewer is not None and viewer.navigate_relative(offset))


@shortcut("viewer.previous", label="Previous image",
          description="Show the previous asset while the detail view is open.",
          keys="Left", component="window.detail", order=20,
          when=_arrow_focus_ok)
def _viewer_previous(ctx) -> bool:
    return _navigate(ctx, -1)


@shortcut("viewer.next", label="Next image",
          description="Show the next asset while the detail view is open.",
          keys="Right", component="window.detail", order=30,
          when=_arrow_focus_ok)
def _viewer_next(ctx) -> bool:
    return _navigate(ctx, +1)


def _click(ctx, name: str) -> bool:
    button = getattr(getattr(getattr(ctx, "window", None), "detail", None),
                     name, None)
    if button is None:
        return False
    button.click()
    return True


@shortcut("viewer.favorite", label="Toggle favourite",
          description="Toggle the favourite state of the current asset.",
          keys="F", component="window.detail", order=40, when=_detail_is_open)
def _viewer_favorite(ctx) -> bool:
    return _click(ctx, "favourite_button")


@shortcut("viewer.information", label="Toggle information panel",
          description="Show or hide the metadata panel of the detail view.",
          keys="I", component="window.detail", order=50, when=_detail_is_open)
def _viewer_information(ctx) -> bool:
    return _click(ctx, "information_button")


@shortcut("viewer.trash", label="Trash / restore asset",
          description="Move the current asset to the trash, or restore it.",
          component="window.detail", order=60, when=_detail_is_open)
def _viewer_trash(ctx) -> bool:
    return _click(ctx, "trash_button")
