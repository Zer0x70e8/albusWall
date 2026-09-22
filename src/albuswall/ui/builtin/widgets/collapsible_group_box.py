#
""""""

from PySide6.QtWidgets import (
    QGroupBox, QStyle, QStyleOptionGroupBox,
    QSizePolicy, QWidget, QVBoxLayout
)
from PySide6.QtCore import Property, Qt

try:
    from ..anims.collapse_animation import CollapseAnimation
except ImportError:
    from albuswall.ui.builtin.anims.collapse_animation import CollapseAnimation


# noinspection PyPep8Naming,PyShadowingNames
class CollapsibleGroupBox(QGroupBox):
    """可折叠的组合框，支持注入动画策略。不设置自身样式表，完全由外部控制外观。"""

    def __init__(self, title, parent=None, animation: CollapseAnimation | None = None):
        super().__init__(title, parent)

        self.setObjectName("collapsibleGroupBox")  # 提供唯一选择器，方便外部指定样式

        self._simble = ["▼", "▶"]
        self._is_simble_inverted = False

        # 禁用内置复选框，自己维护折叠状态
        self.setCheckable(False)
        self._checked = True          # 初始展开
        self._base_title = title
        self._update_title()

        # 内容容器
        self._content = QWidget(self)
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 6, 0, 0)
        main_layout.addWidget(self._content)
        self._main_layout = main_layout

        self._main_layout.setAlignment(self._content, Qt.AlignmentFlag.AlignTop)

        # 动画策略（默认无动画）
        self._animation = animation if animation else CollapseAnimation()

        # 响应自己的 toggled 信号（由 setChecked 手动发射）
        self.toggled.connect(self._on_toggled)

    @Property(str)
    def simble_on_extend(self) -> str:
        return self._simble[0]

    @simble_on_extend.setter
    def simble_on_extend(self, value: str):
        if isinstance(value, str):
            self._simble[0] = value
            self._update_title()  # 更新标题箭头

    @Property(str)
    def simble_on_collapsible(self) -> str:
        return self._simble[1]

    @simble_on_collapsible.setter
    def simble_on_collapsible(self, value: str):
        if isinstance(value, str):
            self._simble[1] = value
            self._update_title()

    @Property(bool)
    def is_simble_inverted(self):
        return self._is_simble_inverted

    @is_simble_inverted.setter
    def is_simble_inverted(self, v):
        if not isinstance(v, bool):
            return
        self._is_simble_inverted = v

    def _update_title(self):
        """根据展开/折叠状态更新标题箭头"""
        if self._is_simble_inverted:
            symbol = self._simble[1] if self._checked else self._simble[0]
        else:
            symbol = self._simble[0] if self._checked else self._simble[1]
        self.setTitle(f"{symbol} {self._base_title}")

    # ---------- 自定义的 isChecked / setChecked ----------
    def isChecked(self) -> bool:
        return self._checked

    def setChecked(self, checked: bool):
        """若状态改变则更新标题并发射 toggled 信号"""
        if checked != self._checked:
            self._checked = checked
            self._update_title()
            self.toggled.emit(checked)

    # ---------- 点击标题区域切换状态 ----------
    def mousePressEvent(self, event):
        opt = QStyleOptionGroupBox()
        self.initStyleOption(opt)
        if self.style().subControlRect(
                QStyle.ComplexControl.CC_GroupBox,
                opt,
                QStyle.SubControl.SC_GroupBoxLabel,
                self
        ).contains(event.position().toPoint()):
            self.setChecked(not self._checked)
            return
        super().mousePressEvent(event)

    # ---------- 访问内容容器 ----------
    def contentWidget(self):
        """返回内部内容容器，将你的布局设置到这个 QWidget 上"""
        return self._content

    # ---------- 动画回调 ----------
    def _on_toggled(self, checked):
        self._animation.stop()

        if checked:  # 展开

            self.setProperty("collapsed", False)
            self._refresh_style()

            self._main_layout.setContentsMargins(0, 6, 0, 0)
            self._animation.animate_expand(
                self._content, self, on_finished=self._on_expand_finished
            )
        else:        # 折叠
            self._animation.animate_collapse(
                self._content, self, on_finished=self._on_collapse_finished
            )

        self.updateGeometry()
        self._activate_parent_layout()

    def _on_expand_finished(self):
        # self.setProperty("collapsed", False)
        # self._refresh_style()

        self._content.setMaximumHeight(16777215)
        self.setSizePolicy(
            self.sizePolicy().horizontalPolicy(),
            QSizePolicy.Policy.Preferred
        )
        self.updateGeometry()
        self._activate_parent_layout()

    def _on_collapse_finished(self):
        self.setProperty("collapsed", True)
        self._refresh_style()

        self._content.hide()
        self._content.setMaximumHeight(0)
        self._main_layout.setContentsMargins(0, 0, 0, 0)
        self.setSizePolicy(
            self.sizePolicy().horizontalPolicy(),
            QSizePolicy.Policy.Fixed
        )
        self.setMinimumHeight(0)
        self.setMaximumHeight(16777215)
        self.adjustSize()
        self.updateGeometry()
        self._activate_parent_layout()

    def _refresh_style(self):
        """强制 Qt 重新应用样式表，使动态属性 [collapsed] 生效"""
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def _activate_parent_layout(self):
        """安全地激活父级布局，若父级或布局不存在则静默跳过。"""
        parent = self.parentWidget()
        if parent is not None:
            layout = parent.layout()
            if layout is not None:
                layout.activate()


if __name__ == "__main__":
    import sys
    from PySide6.QtWidgets import QApplication, QLabel, QCheckBox
    from albuswall.ui.builtin.anims.collapse_animation import HeightSlideAnimation

    app = QApplication(sys.argv)
    win = QWidget()
    main_layout = QVBoxLayout(win)

    # ---------- 统一样式表（仅在此定义，组件内部不设置）----------
    win.setStyleSheet("""
        QGroupBox#collapsibleGroupBox {
            border: 1px solid #aaa;
            border-radius: 4px;
            margin-top: 1.2em;
            font-weight: bold;
        }
        QGroupBox#collapsibleGroupBox::title {
            subcontrol-origin: margin;
            left: 20px;
            padding: 0 5px;
            top: 0.8em;
        }
        QGroupBox#collapsibleGroupBox[collapsed="true"] {
            border: none;
            border-top: 1px solid #aaa;
            background: transparent;
        }
        QGroupBox#collapsibleGroupBox[collapsed="true"]::title {
            background: palette(window);
        }
    """)

    # 使用滑动动画版
    animated_box = CollapsibleGroupBox(
        "带动画的设置",
        animation=HeightSlideAnimation(600)
    )
    content = QVBoxLayout(animated_box.contentWidget())
    content.addWidget(QLabel("平滑展开/折叠的内容"))
    content.addWidget(QCheckBox("选项 A"))

    # 使用无动画版
    static_box = CollapsibleGroupBox("无动画的设置")
    content2 = QVBoxLayout(static_box.contentWidget())
    content2.addWidget(QLabel("直接切换的内容"))

    main_layout.addWidget(animated_box)
    main_layout.addWidget(static_box)
    main_layout.addStretch()
    win.show()
    sys.exit(app.exec())
