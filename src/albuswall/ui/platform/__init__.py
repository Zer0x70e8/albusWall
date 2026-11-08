#
"""``albuswall.ui.platform`` —— 与桌面环境相关的窗口交互补丁。

自绘边框（``FramelessWindowHint``）拿掉了系统标题栏与原生边框，Qt 就不再有
拖拽移动 / 边缘缩放；本包把这些交互自己补回来：

``window_resizer``  :class:`~albuswall.ui.platform.window_resizer.WindowResizer`
                    拖拽移动 + 边缘缩放（平台原生 ``startSystemMove`` /
                    ``startSystemResize``，不自己算几何）。
"""

from __future__ import annotations

from .window_resizer import Direction, WindowResizer

__all__ = ["Direction", "WindowResizer"]
