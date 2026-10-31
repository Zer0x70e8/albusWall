#
"""``albuswall.ui.window`` —— 主窗口与各视图页（纯视图层）。

本包只负责「搭图层 / 同步几何 / 装配子控件」：不查询数据、不调度服务、
不安装快捷键、不认识任何 model / delegate / DTO。业务动作一律由外部
（插件 / presenter）通过子控件暴露的信号接线。
"""

from __future__ import annotations

from .album import Album
from .content import Content
from .detail import Detail
from .menu import Menu
from .panel import InformationPanel
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
    "TagBar",
    "TitleBar",
    "Window",
]
