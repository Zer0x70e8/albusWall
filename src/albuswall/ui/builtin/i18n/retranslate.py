#
"""``QWidget`` 的语言切换混入。

Qt 的标准做法是：在顶层窗口的 ``changeEvent`` 里收到 ``LanguageChange``，
再调用表单自己的 ``retranslateUi()``。本项目的文案分散在几十个嵌套控件
里，所以约定改成「**每个控件自己负责自己的文案**」：

.. code-block:: python

    from ..i18n import Retranslatable

    class Foo(Retranslatable, QWidget):
        def __init__(self, parent=None):
            super().__init__(parent)
            self._setup_ui()      # 只建控件，不写用户可见文案
            self.retranslate_ui()  # 首次应用文案

        def retranslate_ui(self) -> None:
            self.title.setText(self.tr("Hello"))

两条刷新路径都会走到 ``retranslate_ui()``：

* Qt 原生路径 —— 顶层窗口收到 ``QEvent.LanguageChange``，本混入转发；
* 主动路径 —— :class:`~albuswall.ui.builtin.i18n.manager.TranslationManager`
  遍历全部控件逐个调用（嵌套控件唯一可靠的刷新方式）。

约束：``retranslate_ui()`` 必须**幂等**、只改文案（不要重建控件 / 丢用户
输入），因为它会被反复调用。
"""

from __future__ import annotations

from PySide6.QtCore import QEvent

__all__ = ["Retranslatable"]


class Retranslatable:
    """语言切换混入；必须放在 Qt 基类**之前**（``class X(Retranslatable, QWidget)``）。"""

    def retranslate_ui(self) -> None:
        """重新应用本控件的用户可见文案；默认无操作。"""

    # noinspection PyPep8Naming
    def changeEvent(self, event: QEvent) -> None:  # noqa: N802 - Qt 命名
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate_ui()
        super().changeEvent(event)
