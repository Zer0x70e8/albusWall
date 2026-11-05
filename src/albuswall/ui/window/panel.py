#
"""详情页全屏信息面板（纯展示）。

布局顺序：**顶部弹簧 → 关闭横条 → 内容视图**；宽高全部交给布局与样式表，
本类不手动控尺寸，也不实现动画（动画由外部注入）。

内容区是一份「键值行」模型 + 委托：外部可以

* :meth:`InformationPanel.set_metadata` —— 直接喂一个 ``Mapping`` / dataclass /
  DTO，面板内部包一层 :class:`~albuswall.ui.model.MetadataModel`；
* :meth:`InformationPanel.set_model` —— 自己维护模型时直接注入，委托可省。

面板本身不解释任何字段语义，字段顺序 / 标签 / 格式化全在模型里。
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemDelegate,
    QGridLayout,
    QListView,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..delegate import MetadataDelegate
from ..i18n import Retranslatable
from ..model import MetadataModel
from ..widgets import BlurLabel

__all__ = ["InformationPanel"]


class InformationPanel(Retranslatable, QWidget):
    """详情页全屏信息面板。"""

    # ---- 对外信号：只声明“被操作了”，不含业务 ----
    close_requested = Signal()

    # ---- 子控件声明（类型注解） ----
    background: BlurLabel
    content: QWidget
    close_button: QPushButton
    view: QListView
    delegate: MetadataDelegate

    # 外部注入的动画对象（需实现 start()）
    _popup_animation: Any

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)

        self._popup_animation: Any = None
        self._owns_model = False

        self._setup_ui()
        self.retranslate_ui()

    # ------------------------------------------------------------------ #
    # 布局
    # ------------------------------------------------------------------ #
    def _setup_ui(self) -> None:
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
        self.close_button = QPushButton("", self.content)
        self.close_button.setObjectName("DetailInformationCloseButton")
        self.close_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_button.clicked.connect(self.close_requested.emit)
        root.addWidget(self.close_button)

        # ③ 键值区（模型 + 委托，纯展示）
        self.view = QListView(self.content)
        self.view.setObjectName("DetailInformationView")
        self.view.setEditTriggers(QListView.EditTrigger.NoEditTriggers)
        self.view.setSelectionMode(QListView.SelectionMode.NoSelection)
        self.view.setVerticalScrollMode(
            QListView.ScrollMode.ScrollPerPixel)
        self.view.setUniformItemSizes(True)

        self.delegate = MetadataDelegate(self.view)
        self.view.setItemDelegate(self.delegate)
        self.set_metadata(None)

        root.addWidget(self.view)

    # ------------------------------------------------------------------ #
    # 文案
    # ------------------------------------------------------------------ #
    def retranslate_ui(self) -> None:
        """重新应用关闭按钮文案；键值标签由模型自己重新翻译。"""
        self.close_button.setText(self.tr("Close"))

    def changeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        """语言切换：模型里的标签也一起重建（不是 widget，自己收不到事件）。"""
        super().changeEvent(event)
        model = getattr(self, "_model", None)
        if self._owns_model and hasattr(model, "retranslate"):
            model.retranslate()

    # ------------------------------------------------------------------ #
    # 弹出动画接口（外部注入，本类不实现）
    # ------------------------------------------------------------------ #
    def set_popup_animation(self, animation: Any = None) -> None:
        """外部注入弹出 / 收起动画对象（需实现 ``start()``）。"""
        self._popup_animation = animation

    def play_popup_animation(self) -> None:
        """播放外部注入的弹出动画；未注入时为空操作。"""
        if self._popup_animation is not None:
            self._popup_animation.start()

    # ------------------------------------------------------------------ #
    # 数据接口
    # ------------------------------------------------------------------ #
    def set_model(
            self,
            model: Any,
            delegate: QAbstractItemDelegate | None = None,
    ) -> None:
        """直接注入键值行模型（委托可省，缺省用 :class:`MetadataDelegate`）。

        注入后 :meth:`set_metadata` 会转调模型的 ``set_metadata``，
        因此两种用法可以混着来。
        """
        if delegate is None:
            delegate = self.delegate
        self._model = model
        self._owns_model = isinstance(model, MetadataModel)
        self.view.setModel(model)
        self.view.setItemDelegate(delegate)

    def set_metadata(self, metadata: Any | None) -> None:
        """装载键值数据。

        :param metadata: ``Mapping``、dataclass、DTO 均可（模型按字段名取）；
            ``None`` 表示清空。
        """
        model = getattr(self, "_model", None)
        if model is not None and hasattr(model, "set_metadata"):
            model.set_metadata(metadata)
            return
        model = MetadataModel(parent=self)
        self.set_model(model)
        model.set_metadata(metadata)

    def metadata_model(self) -> Any | None:
        """当前键值行模型。"""
        return getattr(self, "_model", None)

    def clear(self) -> None:
        """清空键值区。"""
        model = getattr(self, "_model", None)
        if isinstance(model, MetadataModel):
            model.clear()
        elif model is not None and hasattr(model, "clear"):
            model.clear()

    # 兼容旧名字
    clear_metadata = clear
