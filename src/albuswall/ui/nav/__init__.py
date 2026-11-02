#
"""``albuswall.ui.nav`` —— 界面导航的**注册层**。

:mod:`albuswall.ui.keymap` 回答「哪个键触发哪条命令」；本包回答「按了方向键 /
Tab / Enter 之后，光标该落到哪个控件上」。两件事都只需要**声明**，不需要
Qt 事件，所以拆成独立子包：

====================================  ======================================
模块                                  职责
====================================  ======================================
``model``                             节点 / 域 / 网格坐标 / 页面状态
``registry``                          域与节点的注册表 + 全部导航决策
``errors``                            本层异常
====================================  ======================================

职责边界（三条硬约束）：

* **不 import Qt**、不安装事件过滤器、不接 ``QShortcut``；
* **不主动 ``setFocus()``**——焦点执行只通过
  :meth:`NavRegistry.focus_node` 由调用方触发；
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
    nav.focus_node(node)

定位：区域级路由器
====================

nav 只负责跨区域焦点路由：标题栏 ↔ 内容 ↔ overlay 页 ↔ 页内各区块。
列表内部（虚拟滚动网格、树、chip 条、导航条）与浮层内部（模态对话框、
Popup 菜单、标签树弹出）由 Qt 控件自治，nav 只负责进入 / 退出这些叶域。

叶域的 v1 表达方式（无需 v2 类型支持）：

1. ``Domain(has_root=False, entry="容器节点id")``
2. 容器节点注册为 ``focusable=True``（不是 ``False``），widget 指向容器本身
3. 容器节点 ``pass_through_keys=DEFAULT_PASS_THROUGH_KEYS``
   （或 ``widget.keymap_passthrough = True``）
4. 进入该域后 ``nav.current`` 停在容器节点上；后续按键由调用方先问
   :meth:`NavRegistry.should_pass_through` → 命中则 ``event.ignore()``
   让控件自治
5. 退出该域由调用方根据控件信号触发 ``exit_back`` / ``esc``

主窗口的实际接线在 :mod:`albuswall.ui.nav.wiring`（``register_window_nav``），
由界面 boot 的 phase 3 调用（见 :func:`albuswall.ui.bootstrap.setup_ui`）。

调用方契约
==========

违反以下任何一条都会导致行为错误。nav 不做防御性检查，只保证自身决策正确。

1. **模态期间不调 nav 动作方法**
   ``QDialog.exec`` / ``QMenu.exec`` / Popup / ``QFileDialog`` 等阻塞式嵌套
   事件循环期间，调用方不得调 ``move`` / ``enter`` / ``exit_back`` / ``esc`` /
   ``shift_esc`` / ``jump_to_page_root``。模态返回后调一次 ``set_current``
   同步状态（见契约 2）。

2. **焦点变化必须回调 ``set_current``**
   鼠标点击、程序化 ``setFocus``、Qt 焦点链跳转，都必须在之后调
   ``nav.set_current(page_id, node_id | None)``。nav 不做两向同步，调用方
   不做就是 bug——下次按键会从过期位置跳。

3. **静态布局用 ``GridPlacement``，动态网格用叶域**
   列数会变（Ctrl+滚轮、进详情模式）/ 行数未知（资产数量）/ 虚拟化（只绘制
   可见行）的网格（``Content``、``SquareGridView``、``ColumnListView``、
   ``album_view``、``source.view``、``virtual_scroll`` 等），一律作为叶域，
   nav 不参与内部方向键。``GridPlacement`` 只服务声明式静态布局。

4. **``focus_fn`` 内部负责「聚焦 + 确保可见」**
   nav 不提供独立 ``scroll_fn``；需要滚动的控件在 ``focus_fn`` 里自己调
   ``scrollTo`` / ``ensureWidgetVisible``。nav 只负责决策「给哪个节点」。

5. **同一 widget 多语义用 ``action`` 内部分支**
   trash/restore、favourite 切换、checkable Enter 等，一律在 ``action`` 内按
   状态分支。nav 不做状态感知，不读 ``checkable`` / ``checked``。

6. **键位让渡必须先问 ``should_pass_through``**
   在调 ``move`` / ``enter`` / ``esc`` 之前，先问
   ``should_pass_through(page_id, key)``；命中则 ``event.ignore()`` 让控件
   处理，不调 nav 动作方法。叶域容器节点必须声明 ``pass_through_keys`` 或
   ``widget.keymap_passthrough=True``，否则方向键会被 nav 抢走（叶域内无节点
   可跳，返回 ``None``，光标卡死）。

   适用范围：``should_pass_through`` 只对**无修饰键**有意义。

   * 带 Ctrl / Alt / Meta 修饰的按键不查 ``should_pass_through``，直接走 nav
     正常流程（Ctrl+方向键通常是跨域跳跃或别的高层语义，不属于叶域控件自治
     范围）。
   * Shift 是例外：Shift+Tab（Qt：``Key_Backtab``）语义是 ``exit_back``，不
     属于「让渡」。分发器应把 Shift+Tab 优先映射到 ``exit_back``，而不是查
     ``should_pass_through``。叶域容器仍可在 ``pass_through_keys`` 里声明
     ``"tab"`` 让渡普通 Tab，但分发器需区分 Tab 与 Shift+Tab。

7. **隐藏节点用 ``visible_fn`` 声明，不要靠 unregister**
   ``visible_fn`` 返回 ``False`` 的节点不参与 move / enter / 落点判定。不要
   为了「临时隐藏」而 ``unregister_node``——unregister 会丢失 ``current``
   状态，恢复时无法还原。

8. **set 与 keymap 是并列关系**
   nav 管「跨区域焦点路由」，keymap 管「快捷键分发」。两者在事件循环里由
   调用方先后编排：先 keymap 处理（快捷键命中则消费），再 nav 动作（导航键
   命中则消费），最后冒泡给控件。``keymap_passthrough=True`` 的控件两者都不
   抢（nav 通过 ``should_pass_through`` 让渡，keymap 通过自身机制让渡）。
"""

from __future__ import annotations

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
)
from albuswall.ui.nav.wiring import (
    LEAF_PASS_THROUGH_KEYS,
    NAV_PAGE_ID,
    ROOT_DOMAIN,
    register_window_nav,
)

__all__ = [
    "DEFAULT_PASS_THROUGH_KEYS",
    "LEAF_PASS_THROUGH_KEYS",
    "NAV_PAGE_ID",
    "ROOT_DOMAIN",
    "Direction",
    "Domain",
    "DomainFrame",
    "DuplicateNavDomain",
    "DuplicateNavNode",
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
