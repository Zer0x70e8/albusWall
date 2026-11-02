#
"""把主窗口的控件树翻译成 nav 注册表里的域与节点。

这是 nav 从「注册层」走到「真实界面」的唯一一座桥，三条自我约束：

* **不 import Qt**：只按属性名访问控件（鸭子类型），``widget`` 原样存进
  :class:`~albuswall.ui.nav.model.Node`；
* **不改 ``ui/window/`` 一行代码**：本模块只读窗口的属性，注册完不碰任何控件；
* **可重复调用**：``setup_ui`` 可能在同进程里跑多次（用例、重载界面），所以
  域名 / 节点 id 一律 ``replace=True`` 覆盖，页面栈用
  ``jump_to_page_root`` 复位。

区域划分（与 :class:`~albuswall.ui.window.Window` 的图层一一对应）
==================================================================

======================  ====================================================
域                     覆盖的控件
======================  ====================================================
``window``（根域）      三个「区域入口」：标题栏 / 内容层 / 当前 overlay 页
``titlebar``            工具按钮（相册 / 搜索 / 更多）+ 窗口动作按钮
``content``             内容层虚拟滚动网格（叶域，键盘给控件自己）
``album``               相册页：关闭 / 编辑 / 新建 + 封面网格 + 回收站
``source``              导入源页：关闭 / 添加 / 应用 + 卡片列表 + 表单
``detail``              详情页：大图 / 关闭 / 标签栏 / 缩略导航 + 两个入口
``detail_hud``          详情页操作按钮（多选 / 收藏 / 信息 / 编辑 / 回收站）
``detail_panel``        全屏信息面板（关闭 + 键值列表）
======================  ====================================================

两份「叶域」约定（见 :mod:`albuswall.ui.nav` 包文档的定位节）
============================================================

1. **列表 / 长内容自己做键盘** —— 内容层、相册网格、回收站、导入源卡片、
   详情大图、缩略导航条，都注册成**一个节点**代表整个控件，并声明
   :data:`LEAF_PASS_THROUGH_KEYS`（方向键 + 回车让渡给控件）。
   Esc / Tab 不让渡：那两下属于 nav 的「退出 / 前进」。
2. **复合控件用 ``focus_fn`` 指派内部焦点** —— 标签栏、导入源表单这类控件
   没有单一焦点目标，``focus_fn`` 把焦点交给它们的第一个输入控件
   （``tag_line_edit`` / ``title_line_edit``）。

让渡集按控件**实际能吞的键**声明，禁止一刀切全集
================================================

``pass_through_keys`` 表达的是「这个键交给控件自己按」，声明多了等于把 nav
的方向键入口糊死（光标卡在控件里出不来）。所以按控件能力分三档：

==========================  ======================================
集合                        控件与理由
==========================  ======================================
``LEAF_PASS_THROUGH_KEYS``  滚动网格 / 列表：内容层、相册封面、回收站、
                            导入源卡片、详情大图、缩略导航条 —— 四个方向键
                            + 回车全都由控件自己消化（翻页 / 激活）
``LEAF_PASS_THROUGH_HORIZONTAL``  单行输入：标签栏（``tag_bar``）、导入源
                            表单（``detail_widget``）。只有 ←/→ 归行编辑
                            的光标，↑/↓ 留给 nav 跳到相邻区块，回车留给
                            nav 走 ``action``
``LEAF_PASS_THROUGH_VERTICAL``    可滚动的键值表：全屏信息面板。只有 ↑/↓
                            归控件滚动，←/→ 留给 nav
==========================  ======================================

``visible_fn`` 的四个真实用途
=============================

* overlay 的三个页（相册 / 导入源 / 详情）**共用同一格** ``(2, 0)``，用
  ``overlay.currentWidget() is page`` 过滤 —— 于是「按 DOWN 进当前那一页」
  不需要任何特判；
* **整页不可见时页内节点一律不可落点**（``_all_of`` 把「页面开着」与各自的
  额外条件合起来）：否则方向键 / 焦点回流会把人带进看不见的页；
* 详情页的信息面板只在 ``panel.isVisible()`` 时可达；
* 详情页的 HUD（含关闭按钮）在快速预览模式下被隐藏，那时不该能落上去；
* **叠层可见时，被它盖住的内容层不可落点**（``_overlay_blank``）：内容层的
  区域入口（``window.content``）与页内叶节点（``content.body``）都加上这条
  闸门。标题栏不加 —— 它盖在叠层之上，仍然可达，符合现有交互。

按钮节点的 ``action`` 一律是 ``widget.click``
=============================================

与「焦点在按钮上按回车」的 Qt 原生行为一致，也顺带满足 nav 的契约 5：像
``detail.trash_button`` 这种「同一个控件两种语义」的按钮，点下去由控件自己的
槽按状态分流（删除 / 恢复），nav 不需要知道状态。

按钮的 ``visible_fn`` 一律再与 :func:`_enabled_visible` 取交集：**disabled
的按钮不是合法落点**（方向键 / Tab 会跳过它），否则光标会停在一个按不动的
控件上，回车没反应也没有提示。
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

from albuswall.ui.nav.model import (
    NAV_DOWN,
    NAV_ENTER,
    NAV_LEFT,
    NAV_RIGHT,
    NAV_UP,
    Direction,
    Domain,
    GridPlacement,
    Node,
)
from albuswall.ui.nav.registry import NavRegistry, get_default_registry

__all__ = [
    "LEAF_PASS_THROUGH_HORIZONTAL",
    "LEAF_PASS_THROUGH_KEYS",
    "LEAF_PASS_THROUGH_VERTICAL",
    "NAV_PAGE_ID",
    "ROOT_DOMAIN",
    "register_window_nav",
]

logger = logging.getLogger(__package__)

#: 页面 id：主窗口只有一个页面。
NAV_PAGE_ID = "main"

#: 根域名（``window`` 域承载三个区域入口）。
ROOT_DOMAIN = "window"

#: 滚动网格 / 列表叶域的让渡键：四个方向键 + 回车交给控件，Esc / Tab 留给 nav。
#: 只声明 ``set`` 的一份模板；每个叶节点拿到的是它的副本（避免共享可变集合）。
LEAF_PASS_THROUGH_KEYS: set[str] = {
    NAV_UP, NAV_DOWN, NAV_LEFT, NAV_RIGHT, NAV_ENTER,
}

#: 单行输入叶域的让渡键：←/→ 归行编辑光标，↑/↓ 与回车留给 nav。
LEAF_PASS_THROUGH_HORIZONTAL: set[str] = {NAV_LEFT, NAV_RIGHT}

#: 垂直可滚动叶域的让渡键：↑/↓ 归控件滚动，←/→ 留给 nav。
LEAF_PASS_THROUGH_VERTICAL: set[str] = {NAV_UP, NAV_DOWN}

#: 区域入口在 ``window`` 域里的行号：标题栏 0、内容层 1、overlay 页 2。
_ROW_TITLEBAR = 0
_ROW_CONTENT = 1
_ROW_PAGE = 2


# ==================== 小工具 ====================

def _get(owner: Any, name: str) -> Any:
    """安全取属性：窗口派生类可能砍掉某个区域，缺了就当没有。"""
    return getattr(owner, name, None)


def _focus_fn(widget: Any) -> Any:
    """``widget.setFocus`` 的闭包；没有该方法时返回 ``None``（默认焦点）。"""
    set_focus = getattr(widget, "setFocus", None)
    return set_focus if callable(set_focus) else None


def _click_fn(widget: Any) -> Any:
    """按钮的 ``action``：等价于「焦点在按钮上按回车」。"""
    click = getattr(widget, "click", None)
    return click if callable(click) else None


def _overlay_shows(window: Any, page_attr: str) -> Any:
    """``visible_fn``：overlay 当前显示的正是这个页。"""
    def visible() -> bool:
        overlay = _get(window, "overlay")
        page = _get(window, page_attr)
        current = _get(overlay, "currentWidget")
        if overlay is None or page is None or not callable(current):
            return False
        return current() is page

    return visible


def _overlay_blank(window: Any) -> Any:
    """``visible_fn``：overlay 当前**没有**显示任何页（空白层）。

    叠层可见时，被它盖住的内容层不该还能被落点：否则方向键 / 焦点回流会把
    光标送到一个被遮住的控件上，用户看到的是「选中了看不见的东西」。
    标题栏不受影响——它在叠层之上仍然可达。
    """
    def visible() -> bool:
        overlay = _get(window, "overlay")
        is_blank = _get(overlay, "is_blank")
        if callable(is_blank):
            return bool(is_blank())
        # 没有 ``is_blank`` 的替身：退回「当前页是不是空白」的最小判定
        current = _get(overlay, "currentWidget")
        return bool(callable(current) and current() is None)

    return visible


def _all_of(*checks: Any) -> Any:
    """把若干 ``visible_fn`` 合成一个：全部可见才可见（``None`` 视为恒可见）。"""
    active = [check for check in checks if check is not None]

    def visible() -> bool:
        return all(bool(check()) for check in active)

    return visible


def _widget_visible(owner: Any, attr: str) -> Any:
    """``visible_fn``：控件的 Qt 可见性（父控件隐藏时 ``isVisible()`` 为假）。"""
    def visible() -> bool:
        widget = _get(owner, attr)
        is_visible = _get(widget, "isVisible")
        return bool(callable(is_visible) and is_visible())

    return visible


def _enabled_visible(owner: Any, attr: str) -> Any:
    """``visible_fn``：控件是否启用（disabled 的按钮不该被落点命中）。

    与 :func:`_widget_visible` 的取舍不同：**读不到 ``isEnabled`` 时算启用**
    （返回 ``True``）。原因是用例里的鸭子类型替身 / 非 QObject 控件没有这个
    方法，把「没有这个概念」当成 disabled 会让整张注册表突然没有落点；而
    Qt 控件永远有 ``isEnabled``，真实界面不会因此漏判。
    """
    def visible() -> bool:
        widget = _get(owner, attr)
        is_enabled = _get(widget, "isEnabled")
        if not callable(is_enabled):
            return True
        try:
            return bool(is_enabled())
        except RuntimeError:  # 底层 C++ 对象已销毁
            return False

    return visible


def _button_node(
        node_id: str, owner: Any, attr: str, *,
        row: int, col: int = 0, visible_fn: Any = None,
) -> Node | None:
    """普通按钮节点（``action`` = 点它）；控件缺失时返回 ``None``。

    ``visible_fn`` 会与 :func:`_enabled_visible` 取交集：disabled 的按钮永不
    参与落点判定（网格扫描 / Tab / 反向同步都跳过它）。
    """
    widget = _get(owner, attr)
    if widget is None:
        return None
    return Node(
        id=node_id,
        widget=widget,
        grid=GridPlacement(row, col),
        action=_click_fn(widget),
        visible_fn=_all_of(visible_fn, _enabled_visible(owner, attr)),
    )


def _leaf_node(
        node_id: str, owner: Any, attr: str, *,
        row: int, col: int = 0, focus_on: Any = None,
        visible_fn: Any = None,
        pass_through: Iterable[str] | None = None,
) -> Node | None:
    """叶域节点：一个节点代表整个控件，方向键按控件能力让渡。

    ``focus_on`` 是复合控件的「内部焦点目标」（如标签栏的输入框）；不给时焦点
    直接落在 ``attr`` 那个控件上。

    ``focus_fn`` **只在「落点控件与节点控件不同」时安装**（``focus_on`` 不是
    ``None`` 且不是同一个控件）。普通叶节点不装：让
    :meth:`~albuswall.ui.nav.registry.NavRegistry.focus_node` 走
    ``apply_focus`` + **验证** 路径——焦点没落上去能被发现并回滚；装了
    ``focus_fn`` 的节点按契约 4 视为「nav 猜不出来」，不验证。

    ``pass_through`` 声明这个控件真正能自己消化的键（见模块 docstring 的三档
    约定）；``None`` → :data:`LEAF_PASS_THROUGH_KEYS`（滚动网格全集）。
    """
    widget = _get(owner, attr)
    if widget is None:
        return None
    keys = set(
        LEAF_PASS_THROUGH_KEYS if pass_through is None else pass_through)
    landing = focus_on if focus_on is not None else widget
    install_focus_fn = focus_on is not None and focus_on is not widget
    return Node(
        id=node_id,
        widget=widget,
        grid=GridPlacement(row, col),
        focus_fn=_focus_fn(landing) if install_focus_fn else None,
        pass_through_keys=keys,
        visible_fn=visible_fn,
    )


def _gateway_node(
        node_id: str, owner: Any, attr: str, child_domain: str, *,
        row: int, focus_on: Any = None,
        visible_fn: Any = None,
) -> Node | None:
    """区域入口：可见、可落点，``child_domain`` 指向区域域。

    ``focus_on`` 是这个区域「进来该把焦点交给谁」（通常是它的第一个控件）；
    不给时焦点落在入口节点自己的 widget 上。

    ``focus_fn`` 在这里**保留**（与 ``_leaf_node`` 的策略不同）：容器的
    ``widget`` 是区域容器本体，而容器在 Qt 侧被
    :meth:`~albuswall.ui.nav.qt.QtFocusBackend.ensure_focusable` 固定成
    ``NoFocus``（Qt 的焦点链不该碰到区域容器），所以「落点」只能靠
    ``focus_fn`` 交给内部控件。``Node.focusable`` 仍为 ``True``：nav 侧照旧
    可以把光标落在这个区域入口上（方向键 / Tab 的落点语义不变）。
    """
    widget = _get(owner, attr)
    if widget is None:
        return None
    return Node(
        id=node_id,
        widget=widget,
        grid=GridPlacement(row, 0),
        child_domain=child_domain,
        focus_fn=_focus_fn(focus_on if focus_on is not None else widget),
        visible_fn=visible_fn,
    )


def _register(nav: NavRegistry, domain_id: str,
              nodes: Iterable[Node | None]) -> None:
    """注册一批节点（跳过 ``None``），id 重复时覆盖。"""
    for node in nodes:
        if node is None:
            continue
        nav.register_node(node, domain_id=domain_id, replace=True)


# ==================== 域 ====================

def _register_domains(nav: NavRegistry) -> None:
    """先登记全部域（``parent`` 校验要求父域已存在，所以顺序有讲究）。"""
    # 区域入口在 ``window`` 域里按 UP / DOWN 移动；进了区域再各自网格移动。
    for domain in (
        Domain(
            id=ROOT_DOMAIN,
            has_root=True,
            is_real_page=True,
            # Esc 先把光标退回虚拟根，再 Esc 才交还给「关页」的调用方。
            esc_clears_current=True,
        ),
        Domain(
            id="titlebar",
            has_root=False,
            entry="titlebar.album",
            parent=ROOT_DOMAIN,
            # ←/→ 只走网格扫描，**不**声明 ``direction_aliases``：标题栏是两行
            # 网格（第 0 行三个工具按钮、第 1 行三个窗口按钮），线性 next/prev
            # 会按注册顺序跨行跑（extra(0,2).RIGHT → minimize(1,0)）。网格扫描
            # 才是视觉语义：RIGHT 停在同一行右侧最近的可见按钮。
            # Tab 的顺序另由 ``Domain.tab_order`` / 网格排序决定（见 tab_next）。
            exit_directions={Direction.DOWN},
        ),
        Domain(
            id="content",
            has_root=False,
            entry="content.body",
            parent=ROOT_DOMAIN,
            exit_directions={Direction.DOWN},
        ),
        Domain(
            id="album",
            has_root=False,
            entry="album.close",
            parent=ROOT_DOMAIN,
            exit_directions={Direction.DOWN},
        ),
        Domain(
            id="source",
            has_root=False,
            entry="source.close",
            parent=ROOT_DOMAIN,
            exit_directions={Direction.DOWN},
        ),
        Domain(
            id="detail",
            has_root=False,
            entry="detail.image",
            parent=ROOT_DOMAIN,
            exit_directions={Direction.DOWN},
        ),
        Domain(
            id="detail_hud",
            has_root=False,
            entry="hud.multiple",
            parent="detail",
            direction_aliases={Direction.LEFT: "prev", Direction.RIGHT: "next"},
            exit_directions={Direction.DOWN},
        ),
        Domain(
            id="detail_panel",
            has_root=False,
            entry="panel.close",
            parent="detail",
            exit_directions={Direction.DOWN},
        ),
    ):
        nav.register_domain(domain, replace=True)


# ==================== 节点 ====================

def _register_root(nav: NavRegistry, window: Any) -> None:
    """``window`` 域：三个区域入口（overlay 的三个页共用一格，靠可见性区分）。"""
    _register(nav, ROOT_DOMAIN, [
        # 标题栏不因叠层开着而不可达：它盖在叠层之上，仍然能点、能按。
        _gateway_node("window.titlebar", window, "title_bar", "titlebar",
                      row=_ROW_TITLEBAR,
                      focus_on=_get(_get(window, "title_bar"), "album_button")),
        # 内容层被叠层盖住时不可落点（见 _overlay_blank 的 docstring）。
        _gateway_node("window.content", window, "content", "content",
                      row=_ROW_CONTENT,
                      visible_fn=_overlay_blank(window)),
        _gateway_node("window.album", window, "album", "album",
                      row=_ROW_PAGE,
                      focus_on=_get(_get(window, "album"), "album_view"),
                      visible_fn=_overlay_shows(window, "album")),
        _gateway_node("window.source", window, "source", "source",
                      row=_ROW_PAGE,
                      focus_on=_get(_get(window, "source"), "view"),
                      visible_fn=_overlay_shows(window, "source")),
        _gateway_node("window.detail", window, "detail", "detail",
                      row=_ROW_PAGE,
                      focus_on=_get(_get(window, "detail"), "image_viewer"),
                      visible_fn=_overlay_shows(window, "detail")),
    ])


def _register_titlebar(nav: NavRegistry, window: Any) -> None:
    title_bar = _get(window, "title_bar")
    if title_bar is None:
        return
    _register(nav, "titlebar", [
        _button_node("titlebar.album", title_bar, "album_button", row=0, col=0),
        _button_node("titlebar.search", title_bar, "search_button", row=0, col=1),
        _button_node("titlebar.extra", title_bar, "extra_button", row=0, col=2),
        _button_node("titlebar.minimize", title_bar, "minimize_button",
                     row=1, col=0),
        _button_node("titlebar.maximize", title_bar, "maximize_button",
                     row=1, col=1),
        _button_node("titlebar.close", title_bar, "close_button",
                     row=1, col=2),
    ])


def _register_content(nav: NavRegistry, window: Any) -> None:
    """内容层：整块虚拟滚动网格是一个叶节点（它自带光标与翻页）。"""
    _register(nav, "content", [
        # 叠层可见时内容层被盖住：页内的叶节点也不该可落点。
        _leaf_node("content.body", window, "content", row=0,
                   visible_fn=_overlay_blank(window)),
    ])


def _register_album(nav: NavRegistry, window: Any) -> None:
    album = _get(window, "album")
    if album is None:
        return
    # 整页不可见时，页内任何节点都不该可落点（否则方向键会走进看不见的页）
    page = _overlay_shows(window, "album")
    _register(nav, "album", [
        _button_node("album.close", album, "close_button", row=0,
                     visible_fn=page),
        _button_node("album.edit", album, "edit_button", row=1, col=0,
                     visible_fn=page),
        _button_node("album.add", album, "add_button", row=1, col=1,
                     visible_fn=page),
        _leaf_node("album.cover", album, "album_view", row=2,
                   visible_fn=page),
        _leaf_node("album.trash", album, "trash_view", row=3,
                   visible_fn=page),
    ])


def _register_source(nav: NavRegistry, window: Any) -> None:
    source = _get(window, "source")
    if source is None:
        return
    page = _overlay_shows(window, "source")
    _register(nav, "source", [
        _button_node("source.close", source, "close_button", row=0,
                     visible_fn=page),
        _button_node("source.add", source, "add_button", row=1,
                     visible_fn=page),
        _button_node("source.apply", source, "apply_button", row=2,
                     visible_fn=page),
        _leaf_node("source.cards", source, "view", row=3, visible_fn=page),
        # 右侧表单：卡片被点开后才出现；焦点交给第一个输入框。单行输入，
        # 只让渡 ←/→（↑/↓ 留给 nav 退回卡片列表）。
        _leaf_node(
            "source.form", source, "detail_widget", row=4,
            focus_on=_get(_get(source, "detail_widget"), "title_line_edit"),
            visible_fn=_all_of(
                page, _widget_visible(source, "detail_container")),
            pass_through=LEAF_PASS_THROUGH_HORIZONTAL,
        ),
    ])


def _register_detail(nav: NavRegistry, window: Any) -> None:
    detail = _get(window, "detail")
    if detail is None:
        return
    page = _overlay_shows(window, "detail")
    # 快速预览模式会藏掉整个 HUD（含关闭按钮）：那时这些节点不可达。
    hud_visible = _all_of(page, _widget_visible(detail, "overlay"))
    panel = _get(detail, "panel")
    panel_visible = _all_of(page, _widget_visible(detail, "panel"))

    _register(nav, "detail", [
        _leaf_node("detail.image", detail, "image_viewer", row=0, col=0,
                   visible_fn=page),
        _button_node("detail.close", detail, "close_button",
                     row=0, col=1, visible_fn=hud_visible),
        # 标签栏是单行输入：只让渡 ←/→。↑ 回到大图、↓ 去缩略条、回车走
        # nav 的 action（提交标签），都不能被行编辑吞掉。
        _leaf_node("detail.tagbar", detail, "tag_bar", row=1,
                   focus_on=_get(_get(detail, "tag_bar"), "tag_line_edit"),
                   visible_fn=hud_visible,
                   pass_through=LEAF_PASS_THROUGH_HORIZONTAL),
        _leaf_node("detail.strip", detail, "item_line_viewer", row=2,
                   visible_fn=hud_visible),
        _gateway_node("detail.hud", detail, "operation_container_widget",
                      "detail_hud", row=3,
                      focus_on=_get(detail, "favourite_button"),
                      visible_fn=hud_visible),
        # 信息面板：整页覆盖层，只有打开时可落点。
        _gateway_node("detail.panel", detail, "panel", "detail_panel",
                      row=4, focus_on=_get(panel, "close_button"),
                      visible_fn=panel_visible),
    ])

    _register(nav, "detail_hud", [
        _button_node("hud.multiple", detail, "multiple_choice_button",
                     row=0, col=0, visible_fn=page),
        _button_node("hud.favorite", detail, "favourite_button",
                     row=0, col=1, visible_fn=page),
        _button_node("hud.information", detail, "information_button",
                     row=0, col=2, visible_fn=page),
        _button_node("hud.edit", detail, "edit_button", row=0, col=3,
                     visible_fn=page),
        _button_node("hud.trash", detail, "trash_button", row=0, col=4,
                     visible_fn=page),
    ])

    if panel is not None:
        _register(nav, "detail_panel", [
            _button_node("panel.close", panel, "close_button", row=0,
                         visible_fn=panel_visible),
            # 键值表可纵向滚动：只让渡 ↑/↓，←/→ 留给 nav。
            _leaf_node("panel.meta", panel, "view", row=1,
                       visible_fn=panel_visible,
                       pass_through=LEAF_PASS_THROUGH_VERTICAL),
        ])


# ==================== 对外入口 ====================

def register_window_nav(
        window: Any, *,
        registry: NavRegistry | None = None,
        page_id: str = NAV_PAGE_ID,
) -> NavRegistry:
    """把 ``window`` 的控件树注册成域与节点，并建好页面状态。

    Args:
        window: 主窗口（默认 :class:`~albuswall.ui.window.Window`）。只按属性名
            读取，派生窗口少几个区域也只是少注册几个节点。
        registry: 目标注册表；``None`` → 进程级单例
            :func:`~albuswall.ui.nav.registry.get_default_registry`。
        page_id: 页面 id，默认 ``"main"``。

    Returns:
        写入的注册表（便于调用方接着 ``nav.focus_node(...)``）。

    可重复调用：域名 / 节点 id 固定，重复注册走 ``replace=True``；页面栈用
    :meth:`NavRegistry.jump_to_page_root` 复位到根域虚拟根，不会随着上一次
    的 ``current`` / 深度继续跑。
    """
    nav = registry if registry is not None else get_default_registry()

    _register_domains(nav)
    _register_root(nav, window)
    _register_titlebar(nav, window)
    _register_content(nav, window)
    _register_album(nav, window)
    _register_source(nav, window)
    _register_detail(nav, window)

    state = nav.ensure_page(page_id, root_domain=ROOT_DOMAIN)
    # 复位：弹出历史帧并把光标退回虚拟根（重复注册时不继承旧状态）。
    nav.jump_to_page_root(page_id)

    logger.debug(
        "nav.wiring: page %s wired (%d domains, %d nodes, stack=%d)",
        page_id, len(nav.domains()),
        sum(len(domain.nodes) for domain in nav.domains()),
        len(state.stack),
    )
    return nav
