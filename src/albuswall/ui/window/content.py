#
"""内容层：窗口最底层的虚拟滚动视图。

纯视图组件：不带 model / delegate，也不查询数据。外部可以

* :meth:`Content.bind_model` —— 直接投一份 Qt 模型（行数 + 缩略图自动同步）；
* :meth:`Content.set_pixmap` —— 走纯索引模式，自己按需填图。
"""

from __future__ import annotations

from PySide6.QtWidgets import QWidget

from ..widgets.virtual_scroll import VirtualScrollWidget

__all__ = ["Content"]


class Content(VirtualScrollWidget):
    """内容层容器（铺满窗口，压在所有图层最底）。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
