#
"""键盘「激活 / 确定」键的统一层：空格与回车等价。

问题
====

窗口里同一个意思（确定 / 打开 / 激活当前项）过去有两个键位来源，取决于
控件自身：

* **空格** —— ``QAbstractButton``（按钮 / 复选框 / 工具按钮 …）原生只在
  空格上触发点击；
* **回车 / 小键盘回车** —— ``QAbstractItemView``（列表 / 网格 / 树）与
  :class:`~albuswall.ui.widgets.virtual_scroll.VirtualScrollWidget` 只在
  回车 / 双击上「激活」，而普通窗口里的 ``QPushButton`` 对回车毫无反应。

于是「有的地方用空格确定、有的地方用回车确定」。本模块用一个**应用级事件
过滤器**把两者收敛成同一类「激活键」，只在一处实现：

1. ``QAbstractButton`` 收到回车 → 改写成一对空格的按下 / 松开（原生行为：
   按下动画 + ``clicked`` + 带菜单的按钮弹出菜单）。**对话框里的按钮不动**：
   那里回车是 Qt 的「默认按钮」语义，接管会改变「焦点在取消按钮上按回车
   触发确定」这类既有约定；
2. ``QAbstractItemView`` 收到空格 → 发 ``activated(currentIndex())``（与
   回车同一条出口）；
3. 带菜单的按钮（``QPushButton`` / ``QToolButton``）收到 ``Menu`` 键或
   ``Shift+F10`` → 打开自己的菜单（与标签树 / 导入源卡片的约定一致）。

不接管的按键
============

* 带 ``Ctrl`` / ``Alt`` / ``Meta`` 的组合：留给其它消费者（快捷键、多选等）；
* 自动重复（按住不放）：与 Qt 原生按钮一致，不重复点击；
* 文本框 / 可编辑控件：空格与回车属于输入内容。本过滤器只认
  ``QAbstractButton`` 与 ``QAbstractItemView``，自然不碰它们。

内容视图是**唯一**的例外：空格是「快速预览」、回车是「详情」，两件事分开
（见 :mod:`albuswall.ui.widgets.virtual_scroll.basic` 的 ``unit_previewed``
与 :mod:`albuswall.ui.window.detail` 的预览模式）。内容网格不是
``QAbstractItemView``，所以本过滤器不会把空格改写成「激活」。

用法
====

界面 boot（:func:`albuswall.ui.bootstrap.setup_ui`）调用一次
:func:`install_activation_key_filter`；重复调用是幂等的（同一应用只装一个）。
"""

from __future__ import annotations

import weakref

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractItemView,
    QApplication,
    QDialog,
    QGroupBox,
    QWidget,
)

__all__ = [
    "ACTIVATION_KEYS",
    "ActivationKeyFilter",
    "install_activation_key_filter",
    "is_activation_key",
]

#: 「确定 / 激活」的按键集合：空格、回车、小键盘回车。
ACTIVATION_KEYS = (
    Qt.Key.Key_Space,
    Qt.Key.Key_Return,
    Qt.Key.Key_Enter,
)

#: 出现这些修饰键时不接管（保留给其它消费者）。
_BLOCKING_MODIFIERS = (
    Qt.KeyboardModifier.ControlModifier
    | Qt.KeyboardModifier.AltModifier
    | Qt.KeyboardModifier.MetaModifier
)

#: ``id(app)`` 不安全，用弱引用表记住「这个应用已经装过过滤器」。
_INSTALLED: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def is_activation_key(event: QKeyEvent) -> bool:
    """``event`` 是否是「确定 / 激活」键（空格 / 回车 / 小键盘回车）。"""
    return event.key() in ACTIVATION_KEYS


class ActivationKeyFilter(QObject):
    """把空格与回车统一成「激活 / 确定」的应用级事件过滤器。

    父对象是 ``QApplication``（跟着应用的生命周期走），由
    :func:`install_activation_key_filter` 安装。
    """

    def eventFilter(self, watched, event):  # noqa: N802 - Qt 命名
        if event.type() != QEvent.Type.KeyPress:
            return False
        if not isinstance(event, QKeyEvent) or event.isAutoRepeat():
            return False
        if event.modifiers() & _BLOCKING_MODIFIERS:
            return False

        if isinstance(watched, QAbstractButton):
            return self._filter_button(watched, event)
        if isinstance(watched, QGroupBox):
            return self._filter_group_box(watched, event)
        if isinstance(watched, QAbstractItemView):
            return self._filter_item_view(watched, event)
        return False

    # ------------------------------------------------------------------ #
    # 按钮：回车 → 空格；Menu 键 → 打开菜单
    # ------------------------------------------------------------------ #
    def _filter_button(self, button: QAbstractButton, event: QKeyEvent) -> bool:
        key = event.key()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            # 对话框里回车是「默认按钮」语义（焦点可以在别的按钮上），保持
            # Qt 原样；普通窗口里的按钮回车什么也不做，这里补成空格。
            if isinstance(button.window(), QDialog):
                return False
            self._send_space(button)
            return True
        if key == Qt.Key.Key_Menu or (
                key == Qt.Key.Key_F10
                and event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            return self._open_menu(button)
        return False

    @staticmethod
    def _filter_group_box(group: QGroupBox, event: QKeyEvent) -> bool:
        """可勾选分组：回车 = 空格（Qt 自带的只有空格切换勾选）。"""
        if event.key() not in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            return False
        if not group.isCheckable():
            return False
        # 对话框里的回车是「默认按钮」语义，别抢
        if isinstance(group.window(), QDialog):
            return False
        ActivationKeyFilter._send_space(group)
        return True

    @staticmethod
    def _send_space(widget: QWidget) -> None:
        """给控件发一对合成的空格按下 / 松开（= 用户真按了一次空格）。"""
        modifiers = Qt.KeyboardModifier.NoModifier
        for event_type in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
            QApplication.sendEvent(
                widget, QKeyEvent(event_type, Qt.Key.Key_Space, modifiers))

    @staticmethod
    def _open_menu(button: QAbstractButton) -> bool:
        """``Menu`` 键 / ``Shift+F10``：打开按钮关联的菜单（没有就放行）。"""
        show_menu = getattr(button, "showMenu", None)
        menu_getter = getattr(button, "menu", None)
        if not callable(show_menu) or not callable(menu_getter):
            return False
        if menu_getter() is None:
            return False
        show_menu()
        return True

    # ------------------------------------------------------------------ #
    # 列表 / 网格 / 树：空格 → activated
    # ------------------------------------------------------------------ #
    @staticmethod
    def _filter_item_view(view: QAbstractItemView, event: QKeyEvent) -> bool:
        if event.key() != Qt.Key.Key_Space:
            return False
        if view.state() == QAbstractItemView.State.EditingState:
            return False
        index = view.currentIndex()
        if not index.isValid():
            return False
        # 与回车同一条出口：Qt 自己在回车 / 双击上发 activated。
        view.activated.emit(index)
        return True


def install_activation_key_filter(app: QApplication) -> ActivationKeyFilter:
    """给 ``app`` 装上激活键过滤器（幂等）；返回那个过滤器对象。"""
    watcher = _INSTALLED.get(app)
    if watcher is not None:
        return watcher
    watcher = ActivationKeyFilter(app)
    app.installEventFilter(watcher)
    _INSTALLED[app] = watcher
    return watcher
