#
"""设置页外壳：左侧导航 + 右侧内容 + 关闭按钮 + 背景模糊。

这个类**只搭骨架**，不认识任何配置项：

* 左侧 ``nav_layout``：章节跳转按钮（按钮名以 ``JumpButton`` 结尾，QSS 靠它命中）；
* 右侧 ``content_layout``：章节折叠框（由 :class:`~albuswall.ui.builtin.setting.main.Setting`
  动态填充）；
* ``close_button``：由 Window 接到「回到相册」。

内容与导航按「插入到尾部 stretch 之前」的方式追加，因此外面可以随时
清空重建（改完设置树、重新加载 schema 都能立刻生效）。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QLayout,
    QPushButton,
    QScrollArea,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

try:
    from ..i18n import Retranslatable
    from ..utils.qt_objectname_utils import set_widget_object_name
    from ..widgets.blur_overlay_label import BlurLabel
except ImportError:  # pragma: no cover - 直接按文件跑时的兜底
    from albuswall.ui.builtin.i18n import Retranslatable
    from albuswall.ui.builtin.utils.qt_objectname_utils import (
        set_widget_object_name,
    )
    from albuswall.ui.builtin.widgets.blur_overlay_label import BlurLabel

__all__ = ["Setting", "insert_before_stretch"]


def insert_before_stretch(layout: QLayout, widget: QWidget) -> int:
    """把 ``widget`` 插到尾部 stretch 之前，返回插入位置。

    布局尾部如果不是 stretch 就直接追加；这样「先加 stretch、后加内容」
    的顺序不会把内容挤到 stretch 后面去。
    """
    index = layout.count()
    if index and layout.itemAt(index - 1).spacerItem() is not None:
        index -= 1
    layout.insertWidget(index, widget)
    return index


class Setting(Retranslatable, QWidget):
    """设置页外壳：单独窗口或内嵌子控件都可以。"""

    close_button: QPushButton

    background: BlurLabel
    # 水平分割器
    splitter: QSplitter
    # 左侧导航区域
    nav_scroll: QScrollArea
    nav_widget: QWidget
    nav_layout: QVBoxLayout
    # 右侧内容区域
    content_scroll: QScrollArea
    content_widget: QWidget
    content_layout: QVBoxLayout

    def __init__(
            self,
            parent=None,
            target=None,
            auto_setup: bool = True,
            as_independent: bool = False,
    ) -> None:
        super().__init__(parent)
        self._as_independent = as_independent
        self._enable_close_button = False
        self._blur_target = target

        self._setup(parent)
        set_widget_object_name(
            self,
            parent_class_name=type(self).__name__,
            separator="",  # 去掉分隔符 → SettingSetting，与主题 QSS 对应
            overwrite=True,
        )

        if auto_setup:
            self.setup()

    # ------------------------------------------------------------------ #
    # 窗口属性
    # ------------------------------------------------------------------ #
    @property
    def enable_close_button(self) -> bool:
        return self._enable_close_button

    @enable_close_button.setter
    def enable_close_button(self, v: bool) -> None:
        self._enable_close_button = v
        if v:
            self.close_button.show()
        else:
            self.close_button.hide()

    def _setup(self, parent=None) -> None:
        self.setObjectName("SettingMain")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        if parent is not None:
            self.setWindowFlags(
                Qt.WindowType.Window
                | Qt.WindowType.WindowTitleHint  # 标题栏
                | Qt.WindowType.WindowSystemMenuHint  # 左上角系统菜单
                | Qt.WindowType.WindowMinimizeButtonHint
                | Qt.WindowType.WindowCloseButtonHint
            )
        self.retranslate_ui()

    def retranslate_ui(self) -> None:
        """重新应用外壳文案（当前只有窗口标题）。"""
        self.setWindowTitle(self.tr("Settings"))

    def setup(self) -> None:
        self._setup_ui()

    # ------------------------------------------------------------------ #
    # 骨架
    # ------------------------------------------------------------------ #
    def _setup_ui(self) -> None:
        self.close_button = QPushButton("\u2715", self)

        # 背景
        self.background = BlurLabel(parent=self, target=self._blur_target)

        # 分割器
        self.splitter = QSplitter(Qt.Orientation.Horizontal)

        # ---- 左侧导航区域 ----
        self.nav_scroll = QScrollArea()
        self.nav_scroll.setWidgetResizable(True)
        self.nav_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.nav_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.nav_scroll.setFrameShape(QScrollArea.Shape.NoFrame)

        self.nav_widget = QWidget()
        self.nav_scroll.setWidget(self.nav_widget)
        self.nav_layout = QVBoxLayout(self.nav_widget)
        self.nav_layout.setContentsMargins(4, 8, 4, 8)
        self.nav_layout.setSpacing(2)

        # ---- 右侧内容区域 ----
        self.content_scroll = QScrollArea()
        self.content_scroll.setWidgetResizable(True)
        self.content_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.content_scroll.setFrameShape(QScrollArea.Shape.NoFrame)

        self.content_widget = QWidget()
        self.content_scroll.setWidget(self.content_widget)
        self.content_layout = QVBoxLayout(self.content_widget)
        self.content_layout.setContentsMargins(16, 10, 16, 16)
        self.content_layout.setSpacing(6)

        # 加入分割器
        self.splitter.addWidget(self.nav_scroll)
        self.splitter.addWidget(self.content_scroll)
        self.splitter.setSizes([140, 480])
        self.splitter.setCollapsible(1, False)
        self.splitter.setChildrenCollapsible(False)

        # 主布局
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.close_button)
        layout.addWidget(self.splitter)

        if not self._enable_close_button:
            self.close_button.hide()

    # ------------------------------------------------------------------ #
    # 动态章节
    # ------------------------------------------------------------------ #
    def add_nav_entry(
            self,
            text: str,
            callback,
            obj_name: str = "SettingJumpButton",
    ) -> QPushButton:
        """在左侧导航追加一个跳转按钮（名字以 ``JumpButton`` 结尾更合主题）。"""
        btn = QPushButton(text, self.nav_widget)
        btn.setText(text)
        btn.setObjectName(obj_name)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.clicked.connect(lambda _checked=False: callback())
        insert_before_stretch(self.nav_layout, btn)
        return btn

    def remove_nav_entry(self, button: QPushButton) -> None:
        """摘掉一个导航按钮（重建章节时用）。"""
        self.nav_layout.removeWidget(button)
        button.setParent(None)
        button.deleteLater()

    def scroll_to_widget_top(self, target_widget: QWidget) -> None:
        """把内容区滚到 ``target_widget`` 顶部。"""
        host = self.content_scroll.widget()
        if host is None or target_widget is None:
            return
        pos = target_widget.mapTo(host, target_widget.rect().topLeft())
        self.content_scroll.verticalScrollBar().setValue(pos.y())

    def add_jump_button(
            self,
            target_widget: QWidget,
            btn_text: str,
            obj_name: str | object,
    ) -> QPushButton:
        """兼容旧接口：按控件加一个跳转按钮。"""
        name = obj_name if isinstance(obj_name, str) else type(obj_name).__name__
        return self.add_nav_entry(
            btn_text,
            lambda: self.scroll_to_widget_top(target_widget),
            name,
        )

    # ------------------------------------------------------------------ #
    # 事件
    # ------------------------------------------------------------------ #
    def resizeEvent(self, event):  # noqa: N802 - Qt 命名
        super().resizeEvent(event)
        if self.background is not None:
            self.background.setGeometry(self.geometry())
            self.background.lower()


if __name__ == "__main__":  # pragma: no cover - 手工预览用
    import sys

    from PySide6.QtWidgets import QLabel

    app = QApplication(sys.argv)
    window = Setting()

    for text in ("test\n" * 40, "test1\n" * 40, "test2\n" * 40):
        label = QLabel(text)
        window.content_layout.addWidget(label)
        window.add_jump_button(label, text.split("\n")[0] or "label", "SettingJumpButton")

    window.show()
    sys.exit(app.exec())
