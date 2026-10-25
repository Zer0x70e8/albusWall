"""详情页五键之外的补充：编辑 / 共享 / 标签输入聚焦。

对应 ``docs/keymap.md`` 的「详情页五键 + 标签栏」：

===========  ==========================================
``F``        收藏（已有，见 ``shortcuts.py``）
``I``        信息面板（已有）
``E``        编辑子页（进入前清除快速预览模式）
``Delete``   移到回收站（全局 ``asset.trash``）
``Shift+S``  共享（避免和幻灯片 ``S`` 冲突）
``Left/Right`` 上一张 / 下一张（已有）
``T``        聚焦标签输入（输入框聚焦时不抢）
``Ctrl+Shift+T`` 全局聚焦标签输入（已有）
===========  ==========================================
"""
from __future__ import annotations

from albuswall.ui.builtin import library

from ..keymap import shortcut
from . import common

__all__ = ["DETAIL_ACTION_IDS"]

DETAIL_ACTION_IDS: tuple[str, ...] = (
    "viewer.edit", "viewer.share", "viewer.tag_focus",
)


def _tag_line_edit(ctx):
    detail = common.detail(ctx)
    return getattr(getattr(detail, "tag_bar", None), "tag_line_edit", None)


@shortcut("viewer.edit", label="Edit asset",
          description="Open the edit sub-page of the detail view. Leaves the "
                      "quick preview mode first.",
          keys="E", component="window.detail", order=55,
          when=common.detail_is_open)
def _edit(ctx) -> bool:
    presenters = common.presenters(ctx)
    clearer = getattr(presenters, "clear_quick_preview", None)
    if callable(clearer):
        clearer()
    detail = common.detail(ctx)
    button = getattr(detail, "edit_button", None)
    if button is None:
        return False
    button.click()
    return True


@shortcut("viewer.share", label="Share asset",
          description="Share the current asset. TODO: no share target / "
                      "export pipeline yet.",
          keys=("Shift+S", "Ctrl+Shift+S"), component="window.detail",
          order=60, when=common.detail_is_open)
def _share(ctx) -> bool:
    return library.todo(ctx, "share asset")


@shortcut("viewer.tag_focus", label="Focus the tag input",
          description="Put the keyboard cursor in the tag input of the "
                      "detail view. While the input has focus, T types "
                      "normally.",
          keys="T", component="window.detail.tagbar", order=20,
          when=common.detail_is_open)
def _tag_focus(ctx) -> bool:
    editor = _tag_line_edit(ctx)
    if editor is None:
        return False
    from ..keymap import focus  # 延迟 import：动作包在无 Qt 环境也能导入

    return focus.focus(editor)
