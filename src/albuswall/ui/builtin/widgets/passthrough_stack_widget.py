#
""""""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QStackedWidget, QWidget


class _PassthroughPage(QWidget):
    """默认空白页：不接受焦点，也不拦截鼠标等交互事件。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        # 不参与 Tab 焦点链 / 不获取键盘焦点
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        # 鼠标（含 hover、滚轮）事件直接穿透，命中测试时被跳过
        self.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents,
            True)


class PassthroughStack(QStackedWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._blank_page = _PassthroughPage(self)
        self.addWidget(self._blank_page)
        self.setCurrentWidget(self._blank_page)

    @property
    def blank_page(self) -> QWidget:
        """返回默认空白页。"""
        return self._blank_page

    def is_blank(self) -> bool:
        return self.currentWidget() is self._blank_page

    def show_blank(self) -> None:
        super().setCurrentIndex(0)  # 直接走底层
        self._sync_interaction_state()

    def setCurrentIndex(self, index: int) -> None:
        if index == 0:
            # 统一入口：不管走 setCurrentIndex(0) 还是 show_blank，行为一致
            self.show_blank()
            return
        super().setCurrentIndex(index)
        self._sync_interaction_state()

    def setCurrentWidget(self, widget) -> None:
        if widget is self._blank_page:
            self.show_blank()
            return
        super().setCurrentWidget(widget)
        self._sync_interaction_state()

    def _sync_interaction_state(self) -> None:
        blank = self.is_blank()

        # 空白页时整个 Overlay 对鼠标透明，事件会落到它下面的控件上；
        # 切到真实页面时恢复正常，页面里的控件照常接收事件。
        self.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents,
            blank
        )

        if not blank:
            page = self.currentWidget()
            if (
                    page is not None
                    and page.isVisible()
                    and page.focusPolicy() != Qt.FocusPolicy.NoFocus
            ):
                page.setFocus()
