"""左侧导航（锚点跳转 + 虚线焦点框）——从 ``view.py`` 拆出来的交互层。

只负责两件事：

1. **锚点按钮列表**：分组 / 叶子各一个可选中按钮，层级用缩进表达；
2. **跳转的两步语义**（设计如此，不是 bug）：

   * **第一次**跳到某个锚点：只在目标组件上画一圈虚线焦点框，**不给键盘
     焦点**；
   * **再次**跳到**同一个**锚点：如果焦点框在跳转前已经显示，就把真正的
     键盘焦点交给目标组件，并隐藏虚线框（改用控件自己的 ``:focus`` 样式）；
   * 跳到别的锚点：重新从第一步开始。

   目标不可聚焦（只读 / 未绑定 / 未声明）时保持虚线框，不强行 ``setFocus``。

导航本身不认识设置树、不碰 binder；"展开父链""滚动到可见""焦点落点"由宿主
（:class:`~albuswall.ui.setting.view.SettingView`）通过 :class:`NavHost` 提供。
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from PySide6.QtCore import QObject, QPoint, QRect, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QButtonGroup,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

__all__ = ["NavHost", "SettingsNav"]


@runtime_checkable
class NavHost(Protocol):
    """导航需要宿主提供的能力（视图实现这四个方法即可）。"""

    def expand_chain(self, key: str) -> None:
        """展开某个锚点的父分组链（让目标出现在可视区里）。"""

    def reveal(self, key: str) -> None:
        """把目标滚动到可视区域。"""

    def focus_target(self, key: str) -> QWidget | None:
        """该锚点的键盘焦点落点（叶子的编辑控件 / 分组框）。"""


class _JumpFrame(QWidget):
    """「跳转目标」的虚线焦点框（**不抢键盘焦点**时由导航自己画）。

    它不参与布局、不吃鼠标事件，只是盖在目标控件上的一圈虚线。真正的键盘
    焦点在第二次跳转时才给出；那时隐藏本框，改由控件自己的 ``:focus`` 样式
    显示。
    """

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.hide()

    def paintEvent(self, _event) -> None:  # noqa: N802  Qt 覆写
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pen = QPen(QColor(0, 122, 255, 220), 2)
        pen.setStyle(Qt.PenStyle.DashLine)
        pen.setDashPattern([4, 3])
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(self.rect().adjusted(1, 1, -2, -2), 8, 8)


class SettingsNav(QObject):
    """左侧锚点导航（按钮 + 跳转焦点框 + 两步焦点语义）。"""

    def __init__(
            self,
            parent: QWidget,
            *,
            sidebar_parent: QWidget,
            sidebar_layout: QVBoxLayout,
            right_scroll: QScrollArea,
            right_content: QWidget,
            host: NavHost,
    ) -> None:
        super().__init__(parent)
        self._sidebar_parent = sidebar_parent
        self._sidebar_layout = sidebar_layout
        self._right_scroll = right_scroll
        self._right_content = right_content
        self._host = host

        self._anchors: dict[str, QWidget] = {}
        self._buttons: dict[str, QPushButton] = {}
        self._visible: dict[str, bool] = {}
        self._current: str | None = None

        self._jump_key: str | None = None
        self._jump_focus_given = False
        self._frame = _JumpFrame(right_content)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.position_frame)

        self._group = QButtonGroup(self)
        self._group.setExclusive(True)

    # ------------------------------------------------------------------
    # 构建 / 清理
    # ------------------------------------------------------------------
    def clear(self) -> None:
        """清空按钮与全部锚点（装新树时用）。"""
        self._timer.stop()
        self._hide_frame()
        self._group.deleteLater()
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        for button in self._buttons.values():
            button.setParent(None)
            button.deleteLater()
        self._anchors.clear()
        self._buttons.clear()
        self._visible.clear()
        self._current = None
        self._jump_key = None
        self._jump_focus_given = False

    def add(
            self,
            key: str,
            widget: QWidget,
            text: str,
            *,
            depth: int,
            leaf: bool,
    ) -> None:
        """登记一个锚点（分组或叶子）并建出对应按钮。"""
        self._anchors[key] = widget
        self._visible[key] = True

        button = QPushButton(self._sidebar_parent)
        button.setObjectName("SettingAnchorJumpButton")
        button.setCheckable(True)
        button.setProperty("leafAnchor", leaf)
        button.setProperty("groupAnchor", not leaf)
        button.setText(("    " * depth) + text)
        button.setToolTip(key)
        button.clicked.connect(lambda _checked=False, k=key: self.select(k))
        self._group.addButton(button)
        self._sidebar_layout.insertWidget(
            max(0, self._sidebar_layout.count() - 1), button,
        )
        self._buttons[key] = button

    def set_visible(self, visibility: Mapping[str, bool]) -> None:
        """按映射批量切按钮可见性（同值不触发布局失效）。"""
        for key, shown in visibility.items():
            button = self._buttons.get(key)
            if button is None:
                continue
            self._visible[key] = shown
            if button.isHidden() == shown:
                button.setVisible(shown)

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------
    @property
    def buttons(self) -> Mapping[str, QPushButton]:
        return MappingProxyType(dict(self._buttons))

    @property
    def visible_anchors(self) -> dict[str, QWidget]:
        """当前可见的 ``key → 锚点控件``（``view.anchor_map`` 的来源）。"""
        return {
            key: widget
            for key, widget in self._anchors.items()
            if self._visible.get(key, True)
        }

    @property
    def current(self) -> str | None:
        return self._current

    @property
    def jump_target(self) -> str | None:
        return self._jump_key

    @property
    def jump_visible(self) -> bool:
        return not self._frame.isHidden()

    @property
    def jump_focus_given(self) -> bool:
        return self._jump_focus_given

    def is_visible(self, key: str) -> bool:
        return self._visible.get(key, False)

    def __contains__(self, key: object) -> bool:
        return key in self._anchors

    # ------------------------------------------------------------------
    # 跳转（两步语义）
    # ------------------------------------------------------------------
    def select(self, key: str) -> bool:
        """点锚点：展开父链 → 画虚线焦点框；第二次点同一个才给键盘焦点。"""
        if key not in self._anchors or not self._visible.get(key, False):
            return False
        already_shown = self._jump_key == key
        self._host.expand_chain(key)
        self.highlight(key)
        self._jump_key = key
        if already_shown:
            self._jump_focus_given = self._focus_target()
            if self._jump_focus_given:
                self._hide_frame()
            else:
                self.schedule_frame()
        else:
            self._jump_focus_given = False
            self.schedule_frame()
        self._host.reveal(key)
        QTimer.singleShot(0, self.schedule_frame)
        return True

    def highlight(self, key: str | None) -> None:
        """把某个锚点按钮设为选中态（不做跳转）。"""
        self._current = key
        self._group.setExclusive(False)
        for anchor_key, button in self._buttons.items():
            button.setChecked(anchor_key == key)
        self._group.setExclusive(True)

    # ------------------------------------------------------------------
    # 虚线焦点框
    # ------------------------------------------------------------------
    def schedule_frame(self) -> None:
        self._timer.start(0)

    def position_frame(self) -> None:
        key = self._jump_key
        if key is None or self._jump_focus_given:
            self._hide_frame()
            return
        target = self._host.focus_target(key)
        if target is None or not target.isVisibleTo(self._right_content):
            self._frame.hide()
            return
        try:
            origin = target.mapTo(self._right_content, QPoint(0, 0))
            size = target.size()
        except RuntimeError:  # 重建后旧 widget 已销毁
            self._frame.hide()
            return
        self._frame.setGeometry(
            QRect(
                origin.x() - 3, origin.y() - 3,
                size.width() + 6, size.height() + 6,
            ),
        )
        self._frame.raise_()
        self._frame.show()

    def _hide_frame(self) -> None:
        self._frame.hide()

    def _focus_target(self) -> bool:
        key = self._jump_key
        if key is None:
            return False
        target = self._host.focus_target(key)
        if target is None or not target.isEnabled():
            return False
        if target.focusPolicy() == Qt.FocusPolicy.NoFocus:
            return False
        if not target.isVisibleTo(self._right_content):
            return False
        target.setFocus(Qt.FocusReason.OtherFocusReason)
        return True

    def __iter__(self) -> Iterator[str]:
        return iter(self._anchors)
