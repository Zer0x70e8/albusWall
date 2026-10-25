"""选择 / 筛选 / 搜索结果动作。

**降级处理**（见 ``docs/keymap.md``）：

* ``Ctrl+A`` 全选只在「筛选结果 / 搜索结果」上下文提供，不做全局万张全选；
  真正的多选模型尚未实现 → 空接口 + TODO；
* ``Home`` / ``End`` / ``PageUp`` / ``PageDown`` 已经是网格控件的原生键，
  不再注册快捷键（否则会和 ``keymap_passthrough`` 抢键）；
* ``Shift+方向键`` 扩展选择同样等待多选模型，先注册成可绑定动作。
"""
from __future__ import annotations

from albuswall.ui.builtin import library

from ..keymap import shortcut
from . import common

__all__ = ["SELECTION_ACTION_IDS"]

SELECTION_ACTION_IDS: tuple[str, ...] = (
    "selection.select_all", "selection.extend", "search.next",
    "search.previous", "filter.clear", "filter.advanced",
)


def _has_filter(ctx) -> bool:
    return bool(getattr(common.presenters(ctx), "has_active_filter", False))


@shortcut("selection.select_all", label="Select all in results",
          description="Select every item of the current filtered / searched "
                      "result. Only available while a filter is active; the "
                      "multi-selection model is still a TODO.",
          keys="Ctrl+A", component="window.content", order=20,
          when=_has_filter)
def _select_all(ctx) -> bool:
    return library.todo(ctx, "multi-selection / Select all")


@shortcut("selection.extend", label="Extend selection",
          description="Extend the selection with the arrow keys. The "
                      "multi-selection model is still a TODO.",
          keys={
              "Shift+Left": {"direction": "left"},
              "Shift+Right": {"direction": "right"},
              "Shift+Up": {"direction": "up"},
              "Shift+Down": {"direction": "down"},
          },
          component="window.content", order=25, arguments={"direction": "right"})
def _extend(ctx, direction: str = "right") -> bool:
    return library.todo(ctx, f"extend selection ({direction})")


def _search_step(ctx, delta: int) -> bool:
    """在过滤 / 搜索结果里移动网格光标（F3 / Shift+F3）。"""
    if not _has_filter(ctx):
        return False
    grid = common.grid(ctx)
    mover = getattr(grid, "move_cursor", None)
    if not callable(mover):
        return False
    mover(delta)
    return True


@shortcut("search.next", label="Next search result",
          description="Move the cursor to the next asset of the active "
                      "filter / search result.",
          keys="F3", component="window.titlebar.search", order=20)
def _search_next(ctx) -> bool:
    return _search_step(ctx, +1)


@shortcut("search.previous", label="Previous search result",
          description="Move the cursor to the previous asset of the active "
                      "filter / search result.",
          keys="Shift+F3", component="window.titlebar.search", order=25)
def _search_previous(ctx) -> bool:
    return _search_step(ctx, -1)


@shortcut("filter.clear", label="Clear filter",
          description="Clear the active tag filter / search condition.",
          keys="Ctrl+Shift+L", component="window.titlebar.search", order=30)
def _clear_filter(ctx) -> bool:
    presenters = common.presenters(ctx)
    clearer = getattr(presenters, "clear_filter", None)
    return bool(clearer is not None and clearer())


@shortcut("filter.advanced", label="Advanced filter",
          description="Open the advanced filter panel. TODO: the filter "
                      "condition model is not implemented yet.",
          keys="Ctrl+Shift+F", component="window.titlebar.search", order=35)
def _advanced_filter(ctx) -> bool:
    return library.show_advanced_filter(ctx)
