#
"""导航分发器（presenter）：把按键事件翻译成 nav 决策 + 受控执行。

这是 nav 层**唯一**碰 Qt 事件的部件。它不实现任何导航语义——语义全在
:class:`~albuswall.ui.nav.registry.NavRegistry`，这里只做五件事：事件分派、
焦点执行、执行校验与回滚、反向同步、（模态）暂停。

为什么是 presenter（而不是 `NavDispatcher(QObject)`）
====================================================

任务书把这一步叫「新增 ``NavDispatcher``」。本仓库里这个角色**已经存在**，
就是本类，因此这里选择**加固而不是复制**：同一个 ``QApplication`` 上装两个
应用级 ``installEventFilter`` 会让一次按键被分派两遍（顺序还要看安装顺序），
``focusChanged`` 也会被两个对象各回灌一次，光标会互相打架。

=================================  ==========================================
候选位置                            为什么不行 / 为什么就这里
=================================  ==========================================
``Application``                     被动主循环，契约上不许有装配 / 界面状态
                                    （``tests/test_ui_bootstrap.py`` 有断言）
``Window``                          纯视图层，挂上去＝视图 import nav
新增 ``NavDispatcher``              与 ``NavPresenter`` 职责完全重叠 → 双分派
**presenter（本类）**                ✅ 拿得到容器（nav / keymap 注册表）与
                                    主窗口（``page_id`` ↔ 窗口），并有
                                    ``start`` / ``stop`` 给过滤器收口
=================================  ==========================================

为什么不用 ``QShortcut`` 分方向键
=================================

``QShortcut`` 只在**有窗口上下文**时触发，且一旦启用就吃掉按键（不会「算完
发现没落点再冒泡」）；方向键要在叶域里让渡给控件（滚动网格自己翻页），而
``QShortcut`` 没有「先问一句再决定要不要吞」这条路径。应用级 eventFilter 才能
做到「决策 → 有进展才消费，否则原样冒泡」。

分派规则（顺序即优先级）
========================

``QKeyEvent(KeyPress)`` 到达应用级事件过滤器
    │
    ├─ 0. 暂停中 / 模态 / 弹窗 / 焦点不在本窗口 → 不处理（直接冒泡）
    ├─ 1. 带 Ctrl / Alt / Meta → 交给 keymap（``trigger("Ctrl+S")``），
    │      命中则消费；修饰键**自己**按下（``Meta`` → ``Meta+Meta``）不算
    │      快捷键，直接冒泡。**无修饰键的快捷键暂不分派**（见「已知缺口」）
    ├─ 2. 转成 nav 键位名（``qt_event_to_nav``），不是导航键 → 冒泡
    ├─ 3. ``nav.should_pass_through(page, key)`` 命中 → 冒泡给控件自治
    ├─ 4. Tab / Shift+Tab：
    │      ├─ 光标在**容器节点**（有 ``child_domain``）或虚拟根上 → Tab 进域
    │      │   （``enter(how="tab")``）、Shift+Tab 出域（``exit_back``）；
    │      └─ 光标在域内的叶 / 按钮上 → Tab 走 ``tab_next``、Shift+Tab 走
    │          ``tab_prev``（域内遍历）；到边界（``None``）时 v1 **不跨域**，
    │          直接消费掉这次按键（Qt 的焦点链已被 nav 接管）
    ├─ 5. 方向键 / Enter / Esc → nav 动作（``move`` / ``enter`` / ``esc``）
    └─ 6. 拿到落点 Node → ``focus_node``（受控执行），执行结果决定是否消费

「消费」的判据是**状态真的变了**（栈深 / 某帧 current 变化）或拿到了落点，
不是「调用了 nav」。所以边界上没动、叶域里被让渡、Esc 到底这些情况都会退回
冒泡——控件的原生行为（列表翻页、行编辑光标、按钮空格）不会被吞。

受控执行与回滚
==============

``NavRegistry.focus_node`` 会验证焦点是否真的落到目标控件上（读后端的
``current_focus_chain``）。验证失败时本类做三件事：

1. 用 ``nav.restore(page_id, before)`` 把逻辑光标**回滚**到动作前；
2. 不消费这次按键（事件冒泡，控件原生行为兜底）；
3. 打 ``warning``（真正的执行事故，不该静默）。

``focus_fn`` 自定义执行与「观测不到焦点」的空链不判失败（见
``NavRegistry.focus_node`` 的 docstring）。

反向同步（契约 2）
==================

``QApplication.focusChanged`` → 焦点控件的**祖先链**上找注册过的 Node →
``nav.set_current``；目标节点不在当前帧的域里时，沿 ``child_domain`` 逐层
push 到它的域（必要时先 ``exit_back`` 回到能到达的位置）。于是「用鼠标点
任意区域」之后，下一次按键从**被点的那个控件**继续，而不是从旧光标。

反向同步有三种结局：

* **命中节点** → 对齐光标（跨区域时逐层 push）；
* **命中的节点当前不可见**（页面没开、HUD 收起）→ **不动光标**：焦点按理
  不会跑到隐藏控件上，Qt 的焦点链 / 程序化 ``setFocus`` 偶尔会；
* **祖先链上没有注册节点** → 焦点跑出了 nav 的地盘（背景层、临时浮层、
  别的窗口的控件）→ **只告警，不拉回**（``_report_stray_focus``）。硬接管
  之后 nav 不跟 Qt 抢焦点：拉回曾在「叠层刷新后焦点跳回主窗口」里当帮凶，
  现在焦点去向由控件树自己决定，结构问题靠这条 warning 暴露。

跨区域 push 会先把**父帧的 current 设成区域入口**（``set_current`` 是 nav
唯一的公开「选中」入口），所以「点进某个区域再按 Esc」会回到入口而不是
虚拟根。这是有意的：鼠标点进来、Esc 退回到进来的那一格，比退回「什么都没
选中」更符合直觉。

暂停（契约 1）
==============

两条互补的闸门：

* 本类自己的 ``_suspended`` 计数 + ``_is_blocked()``（模态 / 弹窗）——挡的是
  **事件分派**；
* ``NavRegistry.suspend(page_id)`` / ``resume()``——挡的是 **nav 动作方法**，
  给「绕过分发器直接调 nav」的调用方（命令 / 插件）也加上保险。

窗口失活（``WindowDeactivate``）只记录状态（``_window_active``），不冻结分派：
失活窗口收不到按键，硬挂起反而会在窗口重新激活前一直吞键。

已知缺口
========

* **无修饰键的快捷键**不分派给 keymap：单键绑定（``F5`` / ``G, G`` 和弦）要
  等 keymap 落地「QKeyEvent → 和弦状态机」那一层。当前默认表里的条目也没有
  对应的槽，所以实际损失为零；
* **和弦**（``G, G``）与 ``QShortcut`` 级别的抢占不在本层；
* 叶域的 ``pass_through_keys`` 是**静态声明**：焦点控件和 ``nav.current``
  不一致时（同步失败，例如节点已悬空）方向键会按声明走，可能被 nav 抢走；
* Tab 到域边界时 v1 不跨域（见 D2）：末尾那次 Tab 被消费但光标不动。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent, QKeySequence
from PySide6.QtWidgets import QApplication

from albuswall.ui.keymap.errors import InvalidKeySequence
from albuswall.ui.keymap.qt_keys import qt_event_to_nav
from albuswall.ui.nav.model import Direction
from albuswall.ui.nav.qt import QtFocusBackend
from albuswall.ui.nav.wiring import NAV_PAGE_ID

from .base import Presenter, register_presenter

if TYPE_CHECKING:
    from albuswall.core.container import Container

    from albuswall.ui.nav.model import Node
    from albuswall.ui.nav.registry import NavRegistry

    from .main import MainPresenter

__all__ = ["NavPresenter"]

logger = logging.getLogger(__package__)

#: nav 键位名 → 方向枚举。
_NAV_DIRECTIONS: dict[str, Direction] = {
    "up": Direction.UP,
    "down": Direction.DOWN,
    "left": Direction.LEFT,
    "right": Direction.RIGHT,
}

#: 出现这些修饰键就当作「显式快捷键」，交给 keymap 而不是导航。
_SHORTCUT_MODIFIERS: Qt.KeyboardModifier = (
    Qt.KeyboardModifier.ControlModifier
    | Qt.KeyboardModifier.AltModifier
    | Qt.KeyboardModifier.MetaModifier
)

#: 修饰键**本身**按下时的按键值。单独按下 ``Meta`` / ``Ctrl`` 时 Qt 照样派发
#: ``KeyPress``，而且带着自己的修饰位（PortableText 出来是 ``Meta+Meta`` 这种
#: 非法序列）。它不是快捷键，必须挡在 keymap 之前，否则每次按修饰键都会在
#: 日志里刷一屏 ``InvalidKeySequence``。
_MODIFIER_KEYS: frozenset[Qt.Key] = frozenset({
    Qt.Key.Key_Control,
    Qt.Key.Key_Shift,
    Qt.Key.Key_Alt,
    Qt.Key.Key_AltGr,
    Qt.Key.Key_Meta,
    Qt.Key.Key_CapsLock,
    Qt.Key.Key_NumLock,
    Qt.Key.Key_ScrollLock,
})

#: ``focusChanged`` 反向同步时最多 push 多少层（防注册表有环时死循环）。
_MAX_PUSH_DEPTH = 8


class NavPresenter(Presenter):
    """把键盘事件接到 nav 注册表上（nav 的 Qt 侧分发器）。"""

    name = "nav"

    def __init__(
            self,
            container: Container,
            *,
            window: Any = None,
            page_id: str = NAV_PAGE_ID,
            parent: Presenter | None = None,
    ) -> None:
        super().__init__(container, parent=parent)
        self._window = window
        self._page_id = page_id
        self._nav: NavRegistry | None = None
        self._shortcuts: Any = None
        #: widget → node id（焦点回流用；控件在窗口生命周期内固定，只需建一次）
        self._widget_index: dict[Any, str] = {}
        #: 顶层窗口 → page_id（多窗口从这里扩）
        self._page_ids: dict[Any, str] = {}
        self._installed_app: QApplication | None = None
        self._suspended = 0
        #: 正在执行我们自己发起的 ``focus_node``：期间的 focusChanged 不该被
        #: 当成「用户用鼠标点了别处」再反推一次（否则退出区域会立刻弹回去）。
        self._applying_focus = False
        #: 窗口当前是否处于激活状态（失活时冻结回灌，见模块 docstring）。
        self._window_active = True

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def on_start(self) -> None:
        """解析依赖 → 装应用级事件过滤器 + 焦点回流。

        缺件时**安静退化为空操作**（headless / 没跑界面 boot / 没做 nav
        wiring 的进程里，presenter 树照样要能起来）。
        """
        nav = self.service("nav_registry")
        if nav is None:
            logger.debug("nav presenter: no nav_registry in container; idle")
            return
        self._nav = nav
        self._shortcuts = self.service("shortcuts")

        window = self._window
        if window is None:
            logger.debug("nav presenter: no window bound; idle")
            return
        if nav.restore_page(self._page_id) is None:
            logger.warning(
                "nav presenter: page %r is not wired; idle "
                "(did ui boot run register_window_nav?)", self._page_id)
            return

        self._page_ids = {window: self._page_id}
        self.refresh()

        # 注入受控执行后端：nav 从此能验证「焦点是否真的落到了那个控件上」。
        # 放在 refresh() 之后——``widget_to_node_id`` 要的是建好的索引。
        nav.set_backend(QtFocusBackend(
            focus_getter=self.target_widget,
            node_id_resolver=self.node_id_for_widget,
            is_landing_widget=self.is_leaf_widget,
        ))

        app = QApplication.instance()
        if not isinstance(app, QApplication):
            logger.debug("nav presenter: no QApplication; idle")
            return
        app.installEventFilter(self)
        app.focusChanged.connect(self._on_focus_changed)
        self._installed_app = app
        logger.debug(
            "nav presenter: installed (page=%s, %d widgets indexed)",
            self._page_id, len(self._widget_index))

    def on_stop(self) -> None:
        app = self._installed_app
        self._installed_app = None
        self._restore_backend()
        if app is None:
            return
        try:
            app.removeEventFilter(self)
        except (RuntimeError, TypeError):  # 应用先没了
            pass
        try:
            app.focusChanged.disconnect(self._on_focus_changed)
        except (RuntimeError, TypeError):
            pass

    def _restore_backend(self) -> None:
        """把 nav 的执行后端退回默认实现（``stop`` 之后不留 Qt 引用）。"""
        nav = self._nav
        if nav is not None:
            nav.set_backend(None)
            nav.resume(self._page_id)

    # ------------------------------------------------------------------ #
    # 暂停（契约 1：模态期间调用方不该让 nav 动）
    # ------------------------------------------------------------------ #
    def suspend(self) -> None:
        """暂停分派（可重入：与 :meth:`resume` 成对使用）。

        同时挂起 nav 注册表里本页的**动作方法**：这样即使有调用方绕过本分发器
        直接调 ``nav.move(...)``（命令 / 插件），模态期间也动不了光标。
        """
        self._suspended += 1
        if self._suspended == 1 and self._nav is not None:
            self._nav.suspend(self._page_id)

    def resume(self) -> None:
        if self._suspended > 0:
            self._suspended -= 1
        if self._suspended == 0 and self._nav is not None:
            self._nav.resume(self._page_id)

    @contextmanager
    def suspended(self) -> Iterator[None]:
        """``with presenter.suspended(): ...`` —— 进出自动配对。"""
        self.suspend()
        try:
            yield
        finally:
            self.resume()

    @property
    def is_suspended(self) -> bool:
        return self._suspended > 0

    @property
    def page_id(self) -> str:
        return self._page_id

    # ------------------------------------------------------------------ #
    # 事件分派
    # ------------------------------------------------------------------ #
    def eventFilter(self, watched, event):  # noqa: N802 - Qt 命名
        kind = event.type()
        if kind is QEvent.Type.KeyPress and self.handle_key_event(event):
            event.accept()
            return True
        if kind in (QEvent.Type.WindowActivate, QEvent.Type.WindowDeactivate) \
                and watched is self._window:
            # 记录窗口激活状态：失活窗口的焦点变化不代表用户在别处点了什么
            # （见模块 docstring「暂停」小节）。
            self._window_active = kind is QEvent.Type.WindowActivate
        return super().eventFilter(watched, event)

    def handle_key_event(self, event: QKeyEvent) -> bool:
        """处理一次按键；返回 ``True`` 表示 nav / keymap 已经消费掉它。

        对外可单独调用（测试与将来的插件都用这条入口，不必走事件过滤器）。
        **暂停 / 模态判定也在这里**：绕过 ``eventFilter`` 直接调用它，同样不会
        在 ``QDialog.exec`` 期间偷偷路由。
        """
        if self._is_blocked():
            return False
        nav = self._nav
        if nav is None:
            return False
        page_id = self._page_id_for_focus()
        if page_id is None or nav.restore_page(page_id) is None:
            return False

        if event.modifiers() & _SHORTCUT_MODIFIERS:
            if event.key() in _MODIFIER_KEYS:
                # 修饰键自己按下（Meta / Ctrl / Shift …）：不是快捷键，别去问
                # keymap——它只认「修饰键 + 实键」的组合。
                return False
            return self._run_shortcut(event)

        key = qt_event_to_nav(event)
        if key is None:
            return False
        if nav.should_pass_through(page_id, key):
            return False

        if key == "tab":
            return self._handle_tab(nav, page_id, event)
        if key == "enter":
            return self._act(
                nav, page_id, "enter",
                lambda: nav.enter(page_id, how="enter"))
        if key == "esc":
            if self._at_root(nav, page_id) and self._current(nav, page_id) is None:
                # 根域虚拟根上 Esc：nav.esc 会弹掉根帧把栈打到只剩 page_focus，
                # 那既不是「取消选中」也不是「关页」——交给调用方（关页）判定。
                return False
            return self._act(nav, page_id, "esc", lambda: nav.esc(page_id))

        direction = _NAV_DIRECTIONS.get(key)
        if direction is None:
            return False
        return self._act(
            nav, page_id, f"move({direction.value})",
            lambda: nav.move(page_id, direction))

    # ------------------------------------------------------------------ #
    # Tab / Shift+Tab：域间进出 vs 域内遍历
    # ------------------------------------------------------------------ #
    def _handle_tab(
            self, nav: NavRegistry, page_id: str, event: QKeyEvent,
    ) -> bool:
        """Tab / Shift+Tab。两者的语义分工见模块 docstring 的规则 4。

        返回 ``True``（消费）的情况：

        * **成功进 / 出域**：光标进了子域或弹回了父域；
        * **域内遍历**：``tab_next`` / ``tab_prev`` 挪了光标，或者已经到头
          （``None``）——到头也消费，因为 Qt 的焦点链已经被 nav 接管，再让它
          跑一遍会和 nav 的光标打架（v1 不跨域，见 D2）。

        **任何** Shift+Tab 都不冒泡（硬接管）：根帧上没有可弹的域时消费掉，并
        请求关闭当前 overlay 页（见 :meth:`_close_current_overlay`）；绝不让
        Qt 的 ``QWidget::focusNextPrevChild`` 接手 —— 那正是「根帧 Shift+Tab
        把焦点甩给主窗口」的来路。
        """
        shift = self._is_shift_tab(event)
        if self._is_container(nav, page_id):
            # 容器踏板 / 区域入口 / 虚拟根：Tab 进域、Shift+Tab 出域
            if shift:
                if self._at_root(nav, page_id):
                    # 已到页面根帧：再 ``exit_back`` 会把根域帧本身也弹掉，
                    # 导航从此失据。这里消费按键 + 有页就关页，绝不冒泡给
                    # Qt 的焦点链（``focusNextPrevChild`` 会把焦点甩给主窗口）。
                    self._close_current_overlay()
                    return True
                if self._act(
                        nav, page_id, "exit_back",
                        lambda: nav.exit_back(page_id)):
                    return True
                # 弹不动（``allow_exit=False``）：同样消费。
                self._close_current_overlay()
                return True
            return self._act(
                nav, page_id, "enter(tab)",
                lambda: nav.enter(page_id, how="tab"))

        if shift:
            # 域内反向：先试上一个兄弟，到头再弹栈出去
            if self._act(nav, page_id, "tab_prev",
                         lambda: nav.tab_prev(page_id)):
                return True
            if self._act(nav, page_id, "exit_back",
                         lambda: nav.exit_back(page_id)):
                return True
            # 也弹不动（域声明了 ``allow_exit=False``）：同样消费。
            return True
        consumed = self._act(
            nav, page_id, "tab_next", lambda: nav.tab_next(page_id))
        if consumed:
            return True
        # 已经走到域内最后一个可见节点：键盘仍然归 nav（不冒泡给 Qt 焦点链）
        return True

    @staticmethod
    def _is_container(nav: NavRegistry, page_id: str) -> bool:
        """光标是否停在虚拟根或容器节点（区域入口 / 踏板）上。"""
        frame = nav.current_frame(page_id)
        if frame is None or frame.current is None:
            return True
        node = nav.get_node(frame.current)
        return node is not None and node.child_domain is not None

    def _close_current_overlay(self) -> None:
        """根帧 Shift+Tab：若窗口暴露了关页钩子，关掉当前 overlay 页。

        「可选钩子」是刻意的：presenter 不假设窗口一定有 ``close_overlay``
        （派生窗口 / 用例替身可能没有）。没有钩子、或者当前本来就是空白层
        时只消费按键，不做任何动作。
        """
        window = self._window
        if window is None:
            return
        overlay = getattr(window, "overlay", None)
        is_blank = getattr(overlay, "is_blank", None)
        try:
            if callable(is_blank) and is_blank():
                return
        except Exception:  # noqa: BLE001 - 观测失败就当没有开页
            return
        close = getattr(window, "close_overlay", None)
        if not callable(close):
            logger.debug(
                "nav presenter: no close_overlay hook on window; "
                "root Shift+Tab consumed only")
            return
        try:
            close()
        except Exception:  # noqa: BLE001 - 关页失败不该吃掉按键处理
            logger.debug("nav presenter: close_overlay failed", exc_info=True)
            return
        logger.debug("nav presenter: root Shift+Tab closed the overlay page")

    # ------------------------------------------------------------------ #
    # 反向同步：焦点控件 → nav.current
    # ------------------------------------------------------------------ #
    def _on_focus_changed(self, _old: Any, new: Any) -> None:
        if self._applying_focus:
            # 自因焦点：nav 刚刚决定的就是它，不必再反推（否则区域入口的
            # ``focus_fn`` 会立刻把光标重新推进区域，退出动作「弹不出去」）。
            return
        if new is None:
            # 焦点离开整个界面（窗口失活 / 控件被销毁）：冻结回灌，等焦点回来
            return
        if self.sync_to_widget(new):
            return
        self._report_stray_focus(new)

    def _report_stray_focus(self, stray: Any) -> None:
        """焦点跑出了 nav 的地盘 → 只留证据，**不拉回**、不改 nav 状态。

        这里的克制是硬接管的一部分：nav 管的是「跨区域焦点该落在哪个叶域」，
        一旦焦点离开 nav 的控件树（背景层、临时浮层、别的窗口），把它拽回旧
        光标只会和真正该拿焦点的东西打架 —— 「叠层刷新后焦点跳回主窗口」就是
        这么来的（刷新中途 Qt 会临时把焦点挪走，拉回逻辑随即把它往旧光标拽）。

        所以这里只 ``warning``：控件类型 + ``nav.current`` 快照，方便诊断
        「哪个控件不在注册表里」这个结构问题。
        """
        nav = self._nav
        if nav is None:
            return
        page_id = self._page_id_for_widget(stray)
        if page_id is None:
            # 别的窗口 / 还没登记的控件：完全不属于本页，连告警都不必
            return
        frame = nav.current_frame(page_id)
        logger.warning(
            "nav presenter: focus landed on unregistered widget %r "
            "(page=%s, domain=%s, nav.current=%r); focus left untouched",
            type(stray).__name__, page_id,
            None if frame is None else frame.domain_id,
            None if frame is None else frame.current,
        )

    def sync_to_widget(self, widget: Any) -> bool:
        """把光标对齐到 ``widget``（``focusChanged`` 的出口）；返回是否同步成功。

        公开是为了让「反向同步」可以脱离真实焦点测试 —— 无 WM 的环境
        （offscreen 之外的平台、CI）里 ``focusWidget()`` 常常是 ``None``。
        """
        page_id = self._page_id_for_widget(widget)
        if page_id is None:
            return False
        node_id = self.node_id_for_widget(widget)
        if node_id is None:
            return False
        nav = self._nav
        node = nav.get_node(node_id) if nav is not None else None
        if node is not None and not nav.is_visible(node):
            # 隐藏区域的控件（页面没开 / HUD 收起了）：光标不该跟着跑过去
            logger.debug(
                "nav presenter: %r is hidden; cursor left as is", node_id)
            return False
        self.sync_current(page_id, node_id)
        return True

    def refresh(self) -> None:
        """重建 widget → node id 索引（窗口重建 / 重新 wiring 之后调用）。"""
        nav = self._nav
        if nav is None:
            return
        index: dict[Any, str] = {}
        for domain in nav.domains():
            for node in domain.nodes.values():
                widget = node.widget
                if widget is None:
                    continue
                try:
                    index[widget] = node.id
                except RuntimeError:  # 底层 C++ 对象已销毁
                    continue
        self._widget_index = index

    def node_id_for_widget(self, widget: Any) -> str | None:
        """沿祖先链找最近的已注册节点（复合控件因此能对上它的叶节点）。"""
        if not self._widget_index:
            self.refresh()
        current = widget
        while current is not None:
            node_id = self._widget_index.get(current)
            if node_id is not None:
                return node_id
            parent = getattr(current, "parentWidget", None)
            if not callable(parent):
                return None
            current = parent()
        return None

    def is_leaf_widget(self, widget: Any) -> bool:
        """``widget`` 是否同时是某个**叶节点**（``child_domain is None``）的落点控件。

        给 ``QtFocusBackend.ensure_focusable`` 用：容器节点一律 ``NoFocus``，
        但「容器与叶共用同一个控件」时（内容层：``window.content`` 与
        ``content.body`` 都是 ``Content``）容器规则让位于叶——否则叶域的键盘
        光标无处可落。注册表在 presenter 手里，判定只能在这里派生。
        """
        nav = self._nav
        if nav is None or widget is None:
            return False
        for domain in nav.domains():
            for node in domain.nodes.values():
                if node.child_domain is None and node.widget is widget:
                    return True
        return False

    def sync_current(self, page_id: str, node_id: str) -> None:
        """把 ``nav.current`` 对齐到 ``node_id``（必要时逐层 push / 弹栈）。"""
        nav = self._nav
        if nav is None:
            return
        frame = nav.current_frame(page_id)
        if frame is None:
            return
        target_domain = self._owner_domain(nav, node_id)
        if target_domain is None:
            return
        if frame.domain_id == target_domain:
            if frame.current != node_id:
                nav.set_current(page_id, node_id)
            return

        if not self._push_towards(page_id, target_domain):
            logger.debug(
                "nav presenter: cannot reach domain %r from %r "
                "(cursor left as is)", target_domain, frame.domain_id)
            return
        try:
            nav.set_current(page_id, node_id)
        except Exception:  # noqa: BLE001 - 悬空 / 不在该域：保持原样
            logger.debug(
                "nav presenter: sync to %r failed; cursor left as is", node_id)

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _act(self, nav: NavRegistry, page_id: str, label: str,
             action: Any) -> bool:
        """跑一次 nav 动作；只有**状态真的变了**才算消费。

        拿到落点就做受控执行：``focus_node`` 的返回值为 ``False``（后端看得
        见焦点、却看到它没落到目标上）时**回滚**逻辑光标到动作前，并且不
        消费这次按键——让控件原生行为兜底，比停在一个「视觉上没选中、逻辑上
        已选中」的状态安全。

        注意 ``is False`` 而不是 ``not ok``：``focus_node`` 在 v1 语义下返回
        ``None``（旧调用方 / 被用例替换掉的实现）时视为「无法判定」，按成功
        处理。
        """
        before = nav.snapshot(page_id)
        try:
            node: Node | None = action()
        except Exception:  # noqa: BLE001 - 决策层不该把 UI 拖崩
            logger.exception("nav presenter: %s failed", label)
            return False
        if node is not None:
            ok = self._focus(nav, node, label)
            if ok is False:
                if nav.restore(page_id, before):
                    logger.warning(
                        "nav presenter: %s landed on %s but focus did not "
                        "take; cursor rolled back", label, node.id)
                return False
            return True
        return nav.snapshot(page_id) != before

    def _focus(
            self, nav: NavRegistry, node: Node, label: str,
    ) -> bool | None:
        """执行焦点；返回 ``focus_node`` 的判定（``None`` = 无法判定）。"""
        self._applying_focus = True
        try:
            return nav.focus_node(node)
        except Exception:  # noqa: BLE001 - 控件可能已销毁
            logger.debug(
                "nav presenter: focus on %r failed after %s", node.id, label)
            return False
        finally:
            self._applying_focus = False

    @staticmethod
    def _at_root(nav: NavRegistry, page_id: str) -> bool:
        """当前是否就站在页面根帧上（栈只有 ``page_focus`` + 根域）。"""
        state = nav.restore_page(page_id)
        return state is not None and len(state.stack) <= 2

    @staticmethod
    def _current(nav: NavRegistry, page_id: str) -> str | None:
        frame = nav.current_frame(page_id)
        return None if frame is None else frame.current

    @staticmethod
    def _owner_domain(nav: NavRegistry, node_id: str) -> str | None:
        for domain in nav.domains():
            if node_id in domain.nodes:
                return domain.id
        return None

    def _push_towards(self, page_id: str, target_domain: str) -> bool:
        """从当前帧逐层走到 ``target_domain``；到不了返回 ``False``。"""
        nav = self._nav
        if nav is None:
            return False
        for _ in range(_MAX_PUSH_DEPTH):
            frame = nav.current_frame(page_id)
            if frame is None:
                return False
            if frame.domain_id == target_domain:
                return True

            step = self._next_domain(nav, frame.domain_id, target_domain)
            if step is None:
                # 目标不在当前域的子图里（点到了兄弟区域）：先弹一层再找。
                # 只弹到根帧为止（``<= 2``）：再往下弹就越过页面根域了。
                state = nav.restore_page(page_id)
                if state is None or len(state.stack) <= 2:
                    return False
                depth = len(state.stack)
                nav.exit_back(page_id)
                if len(state.stack) == depth:
                    return False  # allow_exit=False，弹不动
                continue

            gateway = self._gateway(nav, frame.domain_id, step)
            if gateway is None:
                return False
            nav.set_current(page_id, gateway.id)
            nav.enter(page_id, how="tab")
            moved = nav.current_frame(page_id)
            if moved is None or moved.domain_id != step:
                return False
        return False

    @staticmethod
    def _next_domain(
            nav: NavRegistry, start: str, target: str,
    ) -> str | None:
        """沿 ``child_domain`` 做 BFS，返回从 ``start`` 迈出的第一步。"""
        edges: dict[str, set[str]] = {}
        for domain in nav.domains():
            for node in domain.nodes.values():
                if node.child_domain:
                    edges.setdefault(domain.id, set()).add(node.child_domain)

        seen = {start}
        frontier: list[tuple[str, str | None]] = [(start, None)]
        while frontier:
            domain_id, first_step = frontier.pop(0)
            for child in sorted(edges.get(domain_id, ())):
                if child in seen:
                    continue
                step = first_step or child
                if child == target:
                    return step
                seen.add(child)
                frontier.append((child, step))
        return None

    @staticmethod
    def _gateway(nav: NavRegistry, domain_id: str, child_domain: str) -> Node | None:
        for node in nav.nodes_in(domain_id):
            if node.child_domain == child_domain:
                return node
        return None

    def _is_blocked(self) -> bool:
        if self._suspended > 0:
            return True
        app = self._installed_app
        if app is None:
            return True
        if app.activeModalWidget() is not None:
            return True
        return app.activePopupWidget() is not None

    def target_widget(self) -> Any:
        """当前应接收键盘的控件。

        默认取 ``QApplication.focusWidget()``。留成方法是为了两件事：无 WM
        的环境（offline / CI）里焦点恒为 ``None``，以及让用例能把「焦点在哪」
        换成确定的值而不依赖窗口激活。
        """
        app = self._installed_app
        if app is None:
            return None
        return app.focusWidget()

    def _page_id_for_focus(self) -> str | None:
        return self._page_id_for_widget(self.target_widget())

    def _page_id_for_widget(self, widget: Any) -> str | None:
        if widget is None:
            return None
        top_level = getattr(widget, "window", None)
        if not callable(top_level):
            return None
        try:
            owner = top_level()
        except RuntimeError:  # 控件已销毁
            return None
        return self._page_ids.get(owner)

    def _run_shortcut(self, event: QKeyEvent) -> bool:
        shortcuts = self._shortcuts
        if shortcuts is None:
            return False
        text = _portable_text(event)
        if not text:
            return False
        try:
            results = shortcuts.trigger(text)
        except InvalidKeySequence:
            # 按下的一串不是合法快捷键（典型：单独按住 Meta → ``Meta+Meta``）。
            # 这是「没配上」，不是故障：安静冒泡，不打 traceback。
            logger.debug(
                "nav presenter: %r is not a shortcut; bubbling", text)
            return False
        except Exception:  # noqa: BLE001 - 绑定表损坏 / 槽抛异常
            logger.debug("nav presenter: shortcut %r failed", text, exc_info=True)
            return False
        if not results:
            return False
        logger.debug("nav presenter: shortcut %r consumed", text)
        return True

    @staticmethod
    def _is_shift_tab(event: QKeyEvent) -> bool:
        if event.key() == Qt.Key.Key_Backtab:
            return True
        return bool(
            event.key() == Qt.Key.Key_Tab
            and event.modifiers() & Qt.KeyboardModifier.ShiftModifier
        )


def _portable_text(event: QKeyEvent) -> str:
    """``QKeyEvent`` → PortableText（``Ctrl+S``）；转换不了返回空串。"""
    combination = getattr(event, "keyCombination", None)
    if not callable(combination):
        return ""
    try:
        sequence = QKeySequence(combination())
        return str(sequence.toString(
            QKeySequence.SequenceFormat.PortableText))
    except Exception:  # noqa: BLE001 - 未知键组合
        return ""


# --------------------------------------------------------------------------- #
# 登记
# --------------------------------------------------------------------------- #

@register_presenter
def _build_nav_presenter(
        container: Container,
        main: MainPresenter,
) -> Presenter | None:
    """子 presenter 工厂：没有主窗口时安静跳过（headless / 单测）。"""
    window = main.window
    if window is None:
        logger.debug("nav presenter skipped: no window bound")
        return None
    return NavPresenter(container, window=window, parent=main)
