#
"""搜索栏布局占位控件（SearchBarLayoutPlaceholder）。

锚点按钮：始终保留在标题栏布局里，展开搜索时被搜索栏覆盖。
它只提供「占位 / 度量」职责，交互逻辑都在
:class:`~albuswall.ui.widgets.widget.floating_search_bar.FloatingSearchBar`。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QPushButton


class SearchBarLayoutPlaceholder(QPushButton):
    """锚点按钮。始终保留在布局中，展开时被搜索栏覆盖。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("SearchPlaceholder")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
