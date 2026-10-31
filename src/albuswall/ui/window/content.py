#
"""内容层：窗口最底层的虚拟滚动视图。

纯视图组件：不带 model / delegate，也不查询数据；外部把 model 塞给
自身（:class:`VirtualScrollWidget`）即可。
"""

from __future__ import annotations

from PySide6.QtWidgets import QWidget

from ..widgets.virtual_scroll import VirtualScrollWidget

__all__ = ["Content"]


class Content(VirtualScrollWidget):
    """内容层容器（铺满窗口，压在所有图层最底）。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
