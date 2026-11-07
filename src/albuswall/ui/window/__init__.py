#
"""``albuswall.ui.window`` —— 主窗口与各视图页（纯视图层）。

本包只负责「搭图层 / 同步几何 / 装配子控件」：不查询数据、不调度服务、
不安装快捷键、不认识任何 model / delegate / DTO。业务动作一律由外部
（插件 / presenter）通过子控件暴露的信号接线。

唯一的例外是 :mod:`albuswall.ui.window.settings`：它提供**配置使用接口**
（``[window]`` 状态的声明 / 读取 / 最终更新入口 + 快捷键配置的只读视图），
让 presenter 与命令层不必各自去摸 ``albuswall.configue`` 的私有表。它是纯
配置语义、不碰 Qt 控件，因此并不违反「视图层不认识业务」这条线。
"""

from __future__ import annotations

from . import settings
from .album import Album
from .content import Content
from .detail import Detail
from .menu import Menu
from .panel import InformationPanel
from .settings import (
    OVERLAY_PAGES,
    SHORTCUT_SECTION,
    StateSnapshot,
    WindowState,
    format_geometry,
    parse_geometry,
    shortcut_config_info,
    store_window_state,
    update_settings,
    window_state,
)
from .settings_page import SettingsPage
from .source import IngestSource
from .tagbar import TagBar
from .titlebar import TitleBar
from .window import Window

__all__ = [
    "Album",
    "Content",
    "Detail",
    "InformationPanel",
    "IngestSource",
    "Menu",
    "OVERLAY_PAGES",
    "SettingsPage",
    "SHORTCUT_SECTION",
    "StateSnapshot",
    "TagBar",
    "TitleBar",
    "Window",
    "WindowState",
    "format_geometry",
    "parse_geometry",
    "settings",
    "shortcut_config_info",
    "store_window_state",
    "update_settings",
    "window_state",
]
