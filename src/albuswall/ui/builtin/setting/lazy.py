#
"""懒创建的折叠分组框。

设置页的所有分组（包括「关于」这类非配置页）都是 :class:`LazyGroupBox`：

* **折叠状态不建任何子控件** —— 构造只产出标题，展开时才调用 ``builder``；
* 折叠/展开的动画、标题箭头、样式全部沿用
  :class:`~albuswall.ui.builtin.widgets.collapsible_group_box.CollapsibleGroupBox`，
  这里只加「第一次展开时构建内容」这一件事。

为什么需要它：``setting.schema`` 现在有 60+ 个叶子，全部预先建控件会让设置页
第一次打开就卡一下；而用户通常只看其中一两个分组。
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

try:
    from ..anims.collapse_animation import CollapseAnimation
    from ..widgets.collapsible_group_box import CollapsibleGroupBox
except ImportError:  # pragma: no cover - 直接按文件跑时的兜底
    from albuswall.ui.builtin.anims.collapse_animation import CollapseAnimation
    from albuswall.ui.builtin.widgets.collapsible_group_box import (
        CollapsibleGroupBox,
    )

__all__ = ["LazyGroupBox"]

#: 内容构建回调：拿到「内容容器」，往里面塞控件即可
Builder = Callable[[QWidget], None]


class LazyGroupBox(CollapsibleGroupBox):
    """首次展开时才创建内容的分组框。

    Args:
        title: 标题（会带折叠箭头）。
        builder: 内容构建回调；``None`` 表示空内容（纯占位分组）。
        parent: 父控件。
        animation: 折叠动画策略；默认沿用基类的「无动画」。
        collapsed: 初始是否折叠（默认 True，才谈得上「懒创建」）。
        description: 分组说明；折叠时作为 tooltip，展开后显示在内容顶部。
        object_name: objectName（便于 QSS 选中）。
    """

    #: 内容刚被构建完（只发一次）
    populated = Signal()

    def __init__(
            self,
            title: str,
            builder: Builder | None = None,
            parent: QWidget | None = None,
            *,
            animation: CollapseAnimation | None = None,
            collapsed: bool = True,
            description: str = "",
            object_name: str = "SettingLazyGroupBox",
    ) -> None:
        super().__init__(title, parent=parent, animation=animation)

        self.setObjectName(object_name)
        self._builder: Builder | None = builder
        self._description = description
        self._built = False

        if description:
            self.setToolTip(description)

        if collapsed:
            self._apply_collapsed_state()

    # ------------------------------------------------------------------ #
    # 懒创建
    # ------------------------------------------------------------------ #
    @property
    def is_built(self) -> bool:
        """内容是否已经创建过。"""
        return self._built

    def set_builder(self, builder: Builder | None) -> None:
        """换掉构建回调；已经建过的分组不受影响（避免丢用户输入）。"""
        self._builder = builder

    def ensure_built(self) -> bool:
        """构建内容（幂等）。返回本次是否真的构建了。"""
        if self._built:
            return False
        # 先置位，避免 builder 内部触发的递归构建
        self._built = True

        host = self.contentWidget()
        layout = QVBoxLayout(host)
        layout.setContentsMargins(12, 4, 12, 10)
        layout.setSpacing(4)

        if self._description:
            note = QLabel(self._description, host)
            note.setObjectName("SettingGroupDescription")
            note.setWordWrap(True)
            note.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            layout.addWidget(note)

        if self._builder is not None:
            try:
                self._builder(host)
            except Exception:  # noqa: BLE001  单页构建失败不该炸整个设置页
                from albuswall.log import getLogger

                getLogger(__name__).exception(
                    "failed to build content of setting group %r", self.title()
                )
                error = QLabel(
                    self.tr("(This group failed to build; see the log for "
                            "details.)"),
                    host,
                )
                error.setObjectName("SettingGroupError")
                error.setWordWrap(True)
                layout.addWidget(error)

        layout.addStretch()

        self.populated.emit()
        return True

    def _apply_collapsed_state(self) -> None:
        """把「初始折叠」写进基类状态，但不跑折叠动画。

        基类只在 ``_on_toggled`` 里维护折叠态，直接在构造期 ``setChecked(False)``
        会触发一次针对空内容的动画；这里改成同步设置同样的内部状态。
        """
        self._checked = False
        self._update_title()
        self._content.hide()
        self._content.setMaximumHeight(0)
        self._main_layout.setContentsMargins(0, 0, 0, 0)

    # ------------------------------------------------------------------ #
    # 基类钩子
    # ------------------------------------------------------------------ #
    def _on_toggled(self, checked: bool) -> None:  # noqa: D401 - 基类钩子
        # 展开前必须先有内容，否则动画拿到的 sizeHint 是 0
        if checked:
            self.ensure_built()
        super()._on_toggled(checked)
