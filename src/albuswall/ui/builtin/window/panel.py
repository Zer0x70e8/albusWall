#
""""""

from __future__ import annotations

from typing import Mapping, Optional, Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFormLayout,
    QGridLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
    QListView,
)

from ..widgets import BlurLabel


class InformationPanel(QWidget):
    """详情页全屏信息面板。

    契约（与 ``Detail`` / ``PresenterManager`` 一致）：

    - 父级是 ``Detail``，铺满整个详情页，不参与 ``Detail`` 主布局；
    - 背景用与 ``Detail`` 同款 ``BlurLabel``（组合，不继承）；
    - 布局顺序：**顶部弹簧 → 关闭横条 → 内容视图**；
    - 不手动控制尺寸，宽高全部交给布局与样式表；
    - 弹出 / 收起动画由外部注入，本类只保留接口，不实现动画。
    """

    close_requested = Signal()

    # ---- 子控件（外部可读写，与 Detail 保持同一风格） ----
    background: BlurLabel
    content: QWidget
    close_button: QPushButton
    view: QListView

    # 外部注入的弹出 / 收起动画对象
    _popup_animation: Optional[Any]

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)

        self._popup_animation = None

        self.setup_ui()
        self.setup_key()

    def setup_ui(self) -> None:
        self.setObjectName("DetailInformationPanel")

        # ---------- 网格：背景 + 内容叠在同一格 ----------
        stack = QGridLayout(self)
        stack.setContentsMargins(0, 0, 0, 0)
        stack.setSpacing(0)

        # 背景：先加 → 自动在底层
        self.background = BlurLabel(self, draw_label_content=False)
        self.background.target_widget = self.parent()
        self.background.blur_radius = 8
        self.background.setObjectName("DetailInformationBackground")
        stack.addWidget(self.background, 0, 0)

        # 内容：后加 → 自动在顶层
        self.content = QWidget(self)
        self.content.setObjectName("DetailInformationContent")
        stack.addWidget(self.content, 0, 0)

        # ---------- 内容布局 ----------
        root = QVBoxLayout(self.content)
        root.setContentsMargins(24, 0, 24, 24)
        root.setSpacing(8)

        # ① 顶部弹簧
        root.addStretch(1)

        # ② 关闭横条（整宽大按钮，尺寸由样式表决定）
        self.close_button = QPushButton(self.tr("Close"), self.content)
        self.close_button.setObjectName("DetailInformationCloseButton")
        self.close_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_button.clicked.connect(self.close_requested.emit)
        root.addWidget(self.close_button)

        # ③ 内容视图（无滚动组件，纯 QWidget 承载表单）
        self.view = QListView(self.content)
        self.view.setObjectName("DetailInformationView")

        self._form = QFormLayout(self.view)
        self._form.setContentsMargins(0, 0, 0, 0)
        self._form.setSpacing(8)
        self._form.setLabelAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        self._form.setFormAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )

        root.addWidget(self.view)

    def setup_key(self) -> None:
        pass

    # ------------------------------------------------------------------ #
    # 弹出动画接口（预留，不实现具体动画）
    # ------------------------------------------------------------------ #
    def set_popup_animation(self, animation: Optional[Any]) -> None:
        """外部注入弹出 / 收起动画对象（需实现 ``start()``）。"""
        self._popup_animation = animation

    def play_popup_animation(self) -> None:
        """播放外部注入的弹出动画；未注入时为空操作。"""
        if self._popup_animation is not None:
            self._popup_animation.start()

    # ------------------------------------------------------------------ #
    # 元信息
    # ------------------------------------------------------------------ #
    def clear(self) -> None:
        while self._form.rowCount():
            self._form.removeRow(0)

    def set_metadata(self, metadata: Mapping[str, object] | None) -> None:
        """用元信息 dict 刷新键值区。"""
        self.clear()
        if not metadata:
            return

        for key, value in metadata.items():
            if value is None or value == "":
                continue

            key_label = QLabel(str(key), self.view)
            key_label.setObjectName("DetailInformationKey")
            key_label.setAlignment(
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
            )

            value_label = QLabel(str(value), self.view)
            value_label.setObjectName("DetailInformationValue")
            value_label.setWordWrap(True)
            value_label.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )

            self._form.addRow(key_label, value_label)

    # alias
    clear_metadata = clear
