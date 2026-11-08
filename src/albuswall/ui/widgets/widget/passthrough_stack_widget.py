#
"""事件穿透的 ``QStackedWidget``（``PassthroughStack``）：空白页不拦鼠标。
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QStackedWidget, QWidget


class _PassthroughPage(QWidget):
    """默认空白页：不接受焦点，也不拦截鼠标等交互事件。

    ``NoFocus`` 只挡住「Tab / 点击」这一条焦点入口。**光标不会被它留住**：
    ``QStackedLayout`` 在旧页持有焦点时会把焦点显式转给新页（程序化
    ``setFocus`` 无视焦点策略），所以真正的防线是
    :meth:`PassthroughStack._release_focus` —— 切页前先把焦点从旧页交出去。
    """

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
        self._release_focus()
        super().setCurrentIndex(0)  # 直接走底层
        self._sync_interaction_state()

    def setCurrentIndex(self, index: int) -> None:
        if index == 0:
            # 统一入口：不管走 setCurrentIndex(0) 还是 show_blank，行为一致
            self.show_blank()
            return
        if index < 0 or index >= self.count():  # ← 越界就忽略
            return
        self._release_focus()
        super().setCurrentIndex(index)
        self._sync_interaction_state()

    def setCurrentWidget(self, widget) -> None:
        if widget is self._blank_page:
            self.show_blank()
            return
        if self.indexOf(widget) < 0:  # ← 不在栈里就别调
            return
        self._release_focus()
        super().setCurrentWidget(widget)
        self._sync_interaction_state()

    def _release_focus(self) -> None:
        """切页之前把焦点从当前页交出去（不做落点决策）。

        ``QStackedLayout`` 的换页逻辑是「旧页持有焦点 → 把焦点转给新页」，而
        程序化 ``setFocus`` **无视** ``NoFocus`` 策略。于是一收起浮层，焦点就
        被塞进空白占位页 —— 一个导航层不认识的控件（键盘光标「凭空消失」）。

        这里先 ``clearFocus()``：焦点不再在旧页里，Qt 那一步转移就不会发生，
        占位页也就永远拿不到焦点。**落到哪里不由这里决定**：焦点暂时为空，
        由调用方按业务语义交给下一位（关页 → 回到内容网格 → 收掉浮层）。
        """
        holder = self.focusWidget()
        if holder is not None:
            holder.clearFocus()

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
