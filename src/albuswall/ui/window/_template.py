#
"""Template for a new widget. Delete this line after copying."""

from __future__ import annotations

from collections.abc import Mapping

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QVBoxLayout, QWidget

__all__ = ["XxxWidget"]


class XxxWidget(QWidget):
    """一句话说明本控件干什么（纯视图：不查数据、不含业务）。"""

    # ---- 对外信号：只声明“被操作了”，不含业务 ----
    action_requested = Signal()
    item_selected = Signal(str)  # 参数类型必须写清楚

    # ---- 子控件声明（类型注解） ----
    main_layout: QVBoxLayout

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._setup_ui()

    # ------------------------------------------------------------------ #
    # 布局
    # ------------------------------------------------------------------ #
    def _setup_ui(self) -> None:
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(0)

    # ------------------------------------------------------------------ #
    # 配置
    # ------------------------------------------------------------------ #
    def config(self, config: Mapping) -> None:
        """用外部配置刷新外观；不认识的键忽略。"""
