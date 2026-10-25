"""快速预览 / 幻灯片 / 播放：显式模式的按键入口。

``Space`` 不是宏，而是**上下文绑定**：

===========================  =======================================
网格 / 列表                  ``QuickPreview.open(current)``
快速预览模式                 ``QuickPreview.close()``
普通详情页                   可播放 → 播放 / 暂停；否则无操作
幻灯片                       暂停 / 继续
===========================  =======================================

``K`` 或点击视频区域 = 播放 / 暂停，不清除快速预览模式。
幻灯片用 ``S`` / ``F5`` 显式进入，``Esc`` 先退幻灯片再退查看层。
"""
from __future__ import annotations

from ..keymap import shortcut
from . import common

__all__ = ["PREVIEW_ACTION_IDS"]

PREVIEW_ACTION_IDS: tuple[str, ...] = (
    "preview.space", "preview.slideshow", "preview.play_pause",
)


@shortcut("preview.space", label="Quick preview / exit / play",
          description="Context-aware Space: open the quick preview from the "
                      "grid, close it inside the preview, and play / pause in "
                      "a normal detail view or slideshow.",
          keys="Space", component="window.preview", order=10)
def _space(ctx) -> bool:
    presenters = common.presenters(ctx)
    if presenters is None:
        return False
    if common.slideshow(ctx):
        return bool(presenters.toggle_playback())
    if common.quick_preview(ctx):
        return bool(presenters.exit_quick_preview())
    if getattr(presenters, "detail_visible", False):
        # 普通详情页：视频播放 / 暂停；目前只有图片 → “否则无操作”
        return bool(presenters.toggle_playback())
    return bool(presenters.enter_quick_preview())


@shortcut("preview.slideshow", label="Slideshow",
          description="Enter or leave the explicit slideshow mode on top of "
                      "the quick preview.",
          keys=("S", "F5"), component="window.preview", order=15)
def _slideshow(ctx) -> bool:
    presenters = common.presenters(ctx)
    if presenters is None:
        return False
    if common.slideshow(ctx):
        return bool(presenters.toggle_playback())
    return bool(presenters.enter_slideshow())


@shortcut("preview.play_pause", label="Play / pause",
          description="Play or pause the current slideshow / video without "
                      "leaving the quick preview.",
          keys="K", component="window.preview", order=20)
def _play_pause(ctx) -> bool:
    presenters = common.presenters(ctx)
    return bool(presenters is not None and presenters.toggle_playback())
