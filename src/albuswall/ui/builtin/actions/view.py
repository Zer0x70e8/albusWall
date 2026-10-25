"""视图级动作：全屏、缩放、快捷键帮助。

缩放只在详情页生效（``Ctrl+0`` 适应窗口 / ``Ctrl+1`` 100% / +/-），
全屏对主窗口生效，帮助面板从注册中心现读键位。
"""
from __future__ import annotations

from ..keymap import shortcut
from . import common

__all__ = ["VIEW_ACTION_IDS"]

VIEW_ACTION_IDS: tuple[str, ...] = (
    "window.fullscreen", "window.help", "viewer.zoom_fit",
    "viewer.zoom_actual", "viewer.zoom_in", "viewer.zoom_out",
)


def _image_viewer(ctx):
    return getattr(common.detail(ctx), "image_viewer", None)


@shortcut("window.fullscreen", label="Toggle full screen",
          description="Enter or leave full screen.",
          keys="F11", component="window", order=60,
          allow_in_text_input=True)
def _fullscreen(ctx) -> bool:
    window = common.window(ctx)
    if window is None:
        return False
    if window.isFullScreen():
        window.showNormal()
    else:
        window.showFullScreen()
    return True


@shortcut("window.help", label="Keyboard shortcuts help",
          description="Show every bound key grouped by component.",
          keys=("?", "Ctrl+/"), component="window", order=200)
def _help(ctx) -> bool:
    from ..keymap.ui.help import show_help

    return show_help(common.window(ctx))


@shortcut("viewer.zoom_fit", label="Zoom to fit",
          description="Fit the image into the window.",
          keys="Ctrl+0", component="window.detail", order=70,
          when=common.detail_is_open)
def _zoom_fit(ctx) -> bool:
    viewer = _image_viewer(ctx)
    return bool(viewer is not None and viewer.zoom_fit())


@shortcut("viewer.zoom_actual", label="Zoom to 100%",
          description="Show the image at 1:1 pixel size.",
          keys="Ctrl+1", component="window.detail", order=75,
          when=common.detail_is_open)
def _zoom_actual(ctx) -> bool:
    viewer = _image_viewer(ctx)
    return bool(viewer is not None and viewer.zoom_actual())


@shortcut("viewer.zoom_in", label="Zoom in",
          description="Zoom the image in.",
          keys=("Ctrl++", "+"), component="window.detail",
          order=80, when=common.detail_is_open)
def _zoom_in(ctx) -> bool:
    viewer = _image_viewer(ctx)
    return bool(viewer is not None and viewer.zoom_in())


@shortcut("viewer.zoom_out", label="Zoom out",
          description="Zoom the image out.",
          keys=("Ctrl+-", "-"), component="window.detail", order=85,
          when=common.detail_is_open)
def _zoom_out(ctx) -> bool:
    viewer = _image_viewer(ctx)
    return bool(viewer is not None and viewer.zoom_out())
