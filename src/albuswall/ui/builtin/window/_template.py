#
"""Template for a new widget. Delete this line after copying."""

from __future__ import annotations

from typing import Mapping

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QVBoxLayout, QWidget


class XxxWidget(QWidget):
    # ---- 对外信号：只声明“被操作了”，不含业务 ----
    action_requested = Signal()
    item_selected = Signal(str)  # 参数类型必须写清楚

    # ---- 子控件声明（类型注解，方便 IDE 和你自己） ----
    main_layout: QVBoxLayout

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._setup_ui()

    def _setup_ui(self) -> None:
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(0)

    def config(self, config: Mapping):
        """用来更新配置"""
        ...
