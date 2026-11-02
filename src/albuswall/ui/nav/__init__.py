#
"""``albuswall.ui.nav`` —— 界面导航的**受控执行层**。

:mod:`albuswall.ui.keymap` 回答「哪个键触发哪条命令」；本包回答「按了方向键 /
Tab / Enter 之后，光标该落到哪个控件上」，并**验证**这句话有没有兑现。两件事
都只需要**声明**，不需要 Qt 事件，所以拆成独立子包：

===================================  =====================================
模块                                 职责
===================================  =====================================
``model``                            节点 / 域 / 网格坐标 / 页面状态
``backend``                          焦点执行后端协议（``FocusBackend``）
``registry``                         域与节点的注册表 + 全部导航决策 +
                                     受控执行（``focus_node`` / 回滚 / 挂起）
``wiring``                           把主窗口控件树翻译成域与节点
``errors``                           本层异常
===================================  =====================================

同包里另有一个 **Qt 适配器** ``nav/qt.py``（``QtFocusBackend``）：它是本包
唯一 import Qt 的模块，**不被上面任何模块 import**——只由包外的分发器
（:class:`albuswall.ui.presenter.nav_presenter.NavPresenter`）注入。所以下面
三条硬约束对它**不适用**，对其余模块一律成立。

职责边界（三条硬约束，仍有效）：

* ``model`` / ``errors`` / ``backend`` / ``registry`` / ``wiring``
  **不 import Qt**、不安装事件过滤器、不接 ``QShortcut``；
* **不主动 ``setFocus()``**——焦点执行只通过
  :meth:`NavRegistry.focus_node`，且执行能力外包给 ``FocusBackend``；
* 与 ``albuswall.ui.keymap`` 是**单向**依赖：只在 ``TYPE_CHECKING`` 下
  import ``ShortcutSlot``，运行时按属性鸭子取 ``nav`` 声明。

最小用法：

.. code-block:: python

    from albuswall.ui.nav import Domain, Node, get_default_registry

    nav = get_default_registry()
    nav.register_domain(Domain(id="viewer", has_root=True, is_real_page=True))
    nav.register_node(Node(id="viewer.canvas", widget=canvas), domain_id="viewer")
    state = nav.ensure_page("main", root_domain="viewer")
    node = nav.enter("main", how="tab")     # → viewer.canvas
    nav.focus_node(node)                    # → True（执行并验证）

受控执行：三个部件，三条职责
============================

=====================  ==============================================
部件                   只做这件事
=====================  ==============================================
``NavRegistry``        决策 + 状态：算落点、维护域栈与 ``current``、
                       判断落点是否可见、记录「这次动作有没有进展」
                       （``last_action_ok``）、提供 ``snapshot`` /
                       ``restore`` / ``suspend`` / ``resume``
``FocusBackend``       执行 + 观测：``apply_focus`` / ``ensure_focusable``
                       真的动焦点，``current_focus_chain`` 把结果读回来，
                       ``widget_to_node_id`` 支持反向回灌
``NavDispatcher``      编排 + 收口：拦按键、决定消费还是冒泡、失败时回滚、
（= ``NavPresenter``）  把 ``focusChanged`` 回灌进 ``current``、模态期间
                       ``suspend``
=====================  ==============================================

于是「逻辑光标」和「Qt 真实焦点」不会各走各的：nav 说落了，就一定验证过；
验证不过，分发器把状态回滚并让事件冒泡。nav **仍然**不 import Qt——执行能力
是通过协议借来的，不是自己长出来的。

定位：区域级路由器
====================

nav 只负责跨区域焦点路由：标题栏 ↔ 内容 ↔ overlay 页 ↔ 页内各区块。
列表内部（虚拟滚动网格、树、chip 条、导航条）与浮层内部（模态对话框、
Popup 菜单、标签树弹出）由 Qt 控件自治，nav 只负责进入 / 退出这些叶域。

叶域的 v1 表达方式（无需 v2 类型支持）：

1. ``Domain(has_root=False, entry="容器节点id")``
2. 容器节点注册为 ``focusable=True``（不是 ``False``），widget 指向容器本身
3. 容器节点 ``pass_through_keys=`` **按控件实际能吞的键**声明（或
   ``widget.keymap_passthrough = True`` 表达「让渡全集」）。**禁止一刀切全集**：
   单行输入让渡 ←/→ 就够了（↑/↓ 与回车要留给 nav），滚动网格才值得拿全集。
   主窗口的现成模板是 ``wiring.LEAF_PASS_THROUGH_KEYS`` /
   ``LEAF_PASS_THROUGH_HORIZONTAL`` / ``LEAF_PASS_THROUGH_VERTICAL``
4. 进入该域后 ``nav.current`` 停在容器节点上；后续按键由调用方先问
   :meth:`NavRegistry.should_pass_through` → 命中则 ``event.ignore()``
   让控件自治
5. 退出该域由调用方根据控件信号触发 ``exit_back`` / ``esc``

主窗口的实际接线在 :mod:`albuswall.ui.nav.wiring`（``register_window_nav``），
由界面 boot 的 phase 3 调用（见 :func:`albuswall.ui.bootstrap.setup_ui`），
紧随其后的 phase 3c 负责**首焦点**（落根域第一个可见入口）：wiring 只把页面
复位到虚拟根，虚拟根上方向键没有落点，所以「第一次按键往哪走」归 boot 决定。

调用方契约
==========

违反以下任何一条都会导致行为错误。nav 不做防御性检查，只保证自身决策正确。

1. **模态期间不调 nav 动作方法**
   ``QDialog.exec`` / ``QMenu.exec`` / Popup / ``QFileDialog`` 等阻塞式嵌套
   事件循环期间，调用方不得调 ``move`` / ``enter`` / ``exit_back`` / ``esc`` /
   ``shift_esc`` / ``jump_to_page_root`` / ``tab_next`` / ``tab_prev``。
   用 ``NavRegistry.suspend(page_id)`` / ``resume(page_id)`` 把这条契约变成
   机器保证：挂起期间这些方法一律返回 ``None`` 且不改状态。

2. **焦点回流由分发器自动完成，手工 ``set_current`` 只在绕过它时才需要**
   装了 ``NavPresenter`` 之后，鼠标点击 / 程序化 ``setFocus`` 都会被
   ``QApplication.focusChanged`` 捕获并回灌到 ``nav.current``。祖先链上找不到
   注册节点时（焦点离开了 nav 的地盘）分发器**只记 warning、不拉回焦点**：
   跨区域焦点由 nav 硬接管，跑出地盘的焦点去向由控件树自己决定。此时
   **不要**再手工调 ``set_current``，那是重复劳动，而且会在分发器回灌之前
   插一脚。
   只有两种情况需要手工：① 没有装分发器（headless / 单测 / CLI 内省）；
   ② 要求「逻辑光标先于真实焦点」移位（例如关页前预置落点）。

3. **静态布局用 ``GridPlacement``，动态网格用叶域**
   列数会变（Ctrl+滚轮、进详情模式）/ 行数未知（资产数量）/ 虚拟化（只绘制
   可见行）的网格（``Content``、``SquareGridView``、``ColumnListView``、
   ``album_view``、``source.view``、``virtual_scroll`` 等），一律作为叶域，
   nav 不参与内部方向键。``GridPlacement`` 只服务声明式静态布局。

4. **``focus_fn`` 内部负责「聚焦 + 确保可见」**
   nav 不提供独立 ``scroll_fn``；需要滚动的控件在 ``focus_fn`` 里自己调
   ``scrollTo`` / ``ensureWidgetVisible``。nav 只负责决策「给哪个节点」。
   ``focus_fn`` 一旦给出，其执行结果**被视为成功**（自定义执行可能落到的
   是控件的后代，nav 猜不出来）；没有 ``focus_fn`` 时才走
   ``backend.apply_focus`` 并读焦点链验证。

5. **同一 widget 多语义用 ``action`` 内部分支**
   trash/restore、favourite 切换、checkable Enter 等，一律在 ``action`` 内按
   状态分支。nav 不做状态感知，不读 ``checkable`` / ``checked``。

6. **键位让渡必须先问 ``should_pass_through``**
   在调 ``move`` / ``enter`` / ``esc`` 之前，先问
   ``should_pass_through(page_id, key)``；命中则 ``event.ignore()`` 让控件
   处理，不调 nav 动作方法。叶域容器节点必须声明 ``pass_through_keys`` 或
   ``widget.keymap_passthrough=True``，否则方向键会被 nav 抢走（叶域内无节点
   可跳，返回 ``None``，光标卡死）。

   适用范围：``should_pass_through`` **只对无修饰键**有意义。

   * 带 Ctrl / Alt / Meta 修饰的按键不查 ``should_pass_through``，直接走 nav
     正常流程（Ctrl+方向键通常是跨域跳跃或别的高层语义，不属于叶域控件自治
     范围）。
   * Shift 是例外：Shift+Tab（Qt：``Key_Backtab``）语义是 ``exit_back`` /
     ``tab_prev``，不属于「让渡」。分发器应把 Shift+Tab 优先映射到出域 /
     域内后退，而不是查 ``should_pass_through``。叶域容器仍可在
     ``pass_through_keys`` 里声明 ``"tab"`` 让渡普通 Tab，但分发器需区分
     Tab 与 Shift+Tab。
   * **命中之后就不再管**：让渡的键交给控件，控件自己也不要（``ignore()``）
     时事件继续向窗口冒泡，nav **不再介入**这一次按键——让渡是「我这一层
     放弃」，不是「我先问一句再看要不要抢回来」。需要 nav 接手的键，就不要
     写进 ``pass_through_keys``。

7. **隐藏节点用 ``visible_fn`` 声明，不要靠 unregister**
   ``visible_fn`` 返回 ``False`` 的节点不参与 move / enter / 落点判定。不要
   为了「临时隐藏」而 ``unregister_node``——unregister 会丢失 ``current``
   状态，恢复时无法还原。

8. **set 与 keymap 是并列关系**
   nav 管「跨区域焦点路由」，keymap 管「快捷键分发」。两者在事件循环里由
   调用方先后编排：先 keymap 处理（快捷键命中则消费），再 nav 动作（导航键
   命中则消费），最后冒泡给控件。``keymap_passthrough=True`` 的控件两者都不
   抢（nav 通过 ``should_pass_through`` 让渡，keymap 通过自身机制让渡）。

分发器参考架构（为什么不用 ``QShortcut``）
=========================================

.. code-block:: text

    QApplication.installEventFilter(dispatcher)      ← 唯一入口
        KeyPress → 模态/挂起？→ 放行
                 → keymap 先消费（带修饰键的快捷键）
                 → 转 nav 键位名（qt_event_to_nav）
                 → should_pass_through？→ 放行（控件自治）
                 → Tab / Shift+Tab：容器上进出域，域内走 tab_next/tab_prev
                 → move / enter / esc → 落点 → focus_node
                 → focus_node 失败 → nav.restore() 回滚 + 放行
    QApplication.focusChanged → 回灌 set_current（找不到节点 → 只 warning）
    activeModalWidget / activePopupWidget → suspend

``QShortcut`` 做不到这件事：它只在有窗口上下文时触发，一旦启用就**吃掉**
按键，没有「算完发现没落点再冒泡」这条路径。而方向键必须能「先决策、有进展
才消费」——这正是叶域里滚动网格能自己翻页、边界上空间留白又不被吞的原因。
同理，**不要**把所有控件 ``setFocusPolicy(NoFocus)`` 再手工 ``sendEvent``：
那会同时废掉 IME、spinbox 的键盘输入和无障碍焦点。唯一的反向特例是**区域
容器**——``QtFocusBackend.ensure_focusable`` 把 ``child_domain`` 非空的节点
固定成 ``NoFocus``（Qt 的焦点链不该碰到容器），焦点由 ``focus_fn`` / 子域
entry 落到容器内部的控件上；落点控件仍然只升不降。
"""

from __future__ import annotations

from albuswall.ui.nav.backend import FocusBackend, NullFocusBackend
from albuswall.ui.nav.errors import (
    DuplicateNavDomain,
    DuplicateNavNode,
    InvalidNavGrid,
    NavError,
    NavGridCollision,
    UnknownNavDomain,
    UnknownNavNode,
    UnknownNavPage,
)
from albuswall.ui.nav.model import (
    DEFAULT_PASS_THROUGH_KEYS,
    NAV_DOWN,
    NAV_ENTER,
    NAV_ESC,
    NAV_LEFT,
    NAV_RIGHT,
    NAV_TAB,
    NAV_UP,
    Direction,
    Domain,
    DomainFrame,
    GridPlacement,
    NavPlacement,
    Node,
    PageState,
)
from albuswall.ui.nav.registry import (
    NavRegistry,
    get_default_registry,
    registry_nav_registry,
    set_default_backend,
)
from albuswall.ui.nav.wiring import (
    LEAF_PASS_THROUGH_HORIZONTAL,
    LEAF_PASS_THROUGH_KEYS,
    LEAF_PASS_THROUGH_VERTICAL,
    NAV_PAGE_ID,
    ROOT_DOMAIN,
    register_window_nav,
)

__all__ = [
    "DEFAULT_PASS_THROUGH_KEYS",
    "LEAF_PASS_THROUGH_HORIZONTAL",
    "LEAF_PASS_THROUGH_KEYS",
    "LEAF_PASS_THROUGH_VERTICAL",
    "NAV_PAGE_ID",
    "ROOT_DOMAIN",
    "Direction",
    "Domain",
    "DomainFrame",
    "DuplicateNavDomain",
    "DuplicateNavNode",
    "FocusBackend",
    "GridPlacement",
    "InvalidNavGrid",
    "NAV_DOWN",
    "NAV_ENTER",
    "NAV_ESC",
    "NAV_LEFT",
    "NAV_RIGHT",
    "NAV_TAB",
    "NAV_UP",
    "NavError",
    "NavGridCollision",
    "NavPlacement",
    "NavRegistry",
    "Node",
    "NullFocusBackend",
    "PageState",
    "UnknownNavDomain",
    "UnknownNavNode",
    "UnknownNavPage",
    "get_default_registry",
    "register_domain",
    "register_from_slot",
    "register_node",
    "register_window_nav",
    "registry_nav_registry",
    "set_default_backend",
]


def register_domain(domain, *, replace=False):
    """模块级便捷入口：``get_default_registry().register_domain(...)``。"""
    return get_default_registry().register_domain(domain, replace=replace)


def register_node(node, *, domain_id, replace=False):
    """模块级便捷入口：``get_default_registry().register_node(...)``。"""
    return get_default_registry().register_node(
        node, domain_id=domain_id, replace=replace)


def register_from_slot(slot):
    """模块级便捷入口：``get_default_registry().register_from_slot(...)``。"""
    return get_default_registry().register_from_slot(slot)
