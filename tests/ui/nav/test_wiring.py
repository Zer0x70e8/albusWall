#
"""``nav.wiring``：把主窗口的控件树注册成域 / 节点的回归。

三段：

* **假窗口**（鸭子类型）—— 不 import Qt，锁住「注册了什么」：域名、节点 id、
  网格坐标、``pass_through_keys`` / ``visible_fn`` / ``action`` 的接线；
* **真窗口**（offscreen Qt）—— 锁住「属性名对得上」：``Window`` 的真实控件名
  与 wiring 里写的名字一致，否则这条会红；
* **boot 接线** —— ``bootstrap.setup_ui`` 的 phase 3 真的把窗口注册进容器里
  那个注册表（不变量：容器键 ``nav_registry`` 与注册结果是同一个对象）。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from albuswall.ui.nav.model import Direction, GridPlacement
from albuswall.ui.nav.registry import NavRegistry
from albuswall.ui.nav.wiring import (
    LEAF_PASS_THROUGH_HORIZONTAL,
    LEAF_PASS_THROUGH_KEYS,
    LEAF_PASS_THROUGH_VERTICAL,
    NAV_PAGE_ID,
    ROOT_DOMAIN,
    register_window_nav,
)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


# ==================== 假窗口 ====================

class FakeWidget:
    """假控件：只有 wiring 用到的四个能力。"""

    def __init__(self, *, visible: bool = True) -> None:
        self.clicks = 0
        self.focus_calls = 0
        self._visible = visible

    def click(self) -> None:
        self.clicks += 1

    def setFocus(self, *args: Any) -> None:  # noqa: N802 - 对接 Qt 命名
        self.focus_calls += 1

    def isVisible(self) -> bool:  # noqa: N802 - 对接 Qt 命名
        return self._visible

    def property(self, name: str) -> Any:  # noqa: A003 - 对接 Qt 命名
        return None


class FakeOverlay:
    def __init__(self) -> None:
        self.current: Any = None

    def currentWidget(self) -> Any:  # noqa: N802 - 对接 Qt 命名
        return self.current


def _fake_window() -> SimpleNamespace:
    window = SimpleNamespace()
    window.overlay = FakeOverlay()

    window.title_bar = SimpleNamespace(
        album_button=FakeWidget(),
        search_button=FakeWidget(),
        extra_button=FakeWidget(),
        minimize_button=FakeWidget(),
        maximize_button=FakeWidget(),
        close_button=FakeWidget(),
    )
    window.content = FakeWidget()

    window.album = SimpleNamespace(
        close_button=FakeWidget(),
        edit_button=FakeWidget(),
        add_button=FakeWidget(),
        album_view=FakeWidget(),
        trash_view=FakeWidget(),
    )
    window.source = SimpleNamespace(
        close_button=FakeWidget(),
        add_button=FakeWidget(),
        apply_button=FakeWidget(),
        view=FakeWidget(),
        detail_container=FakeWidget(visible=False),
        detail_widget=SimpleNamespace(title_line_edit=FakeWidget()),
    )
    window.detail = SimpleNamespace(
        image_viewer=FakeWidget(),
        close_button=FakeWidget(),
        tag_bar=SimpleNamespace(tag_line_edit=FakeWidget()),
        item_line_viewer=FakeWidget(),
        operation_container_widget=FakeWidget(),
        multiple_choice_button=FakeWidget(),
        favourite_button=FakeWidget(),
        information_button=FakeWidget(),
        edit_button=FakeWidget(),
        trash_button=FakeWidget(),
        overlay=FakeWidget(),
        panel=SimpleNamespace(
            close_button=FakeWidget(),
            view=FakeWidget(),
            isVisible=lambda: False,
        ),
    )
    # 默认：相册页开着（overlay 显示 album）
    window.overlay.current = window.album
    return window


@pytest.fixture
def nav() -> NavRegistry:
    return NavRegistry()


@pytest.fixture
def window() -> SimpleNamespace:
    return _fake_window()


# ==================== 注册结果 ====================

def test_registers_all_domains(nav, window):
    register_window_nav(window, registry=nav)

    ids = [domain.id for domain in nav.domains()]
    assert ROOT_DOMAIN in ids
    assert {
        "titlebar", "content", "album", "source", "detail",
        "detail_hud", "detail_panel",
    } <= set(ids)

    root = nav.get_domain(ROOT_DOMAIN)
    assert root is not None
    assert root.has_root is True
    assert root.is_real_page is True
    assert root.parent is None

    # 区域域都是无根域，且有明确入口（Tab 的落点）
    for name, entry in (
        ("titlebar", "titlebar.album"),
        ("content", "content.body"),
        ("album", "album.close"),
        ("source", "source.close"),
        ("detail", "detail.image"),
        ("detail_hud", "hud.multiple"),
        ("detail_panel", "panel.close"),
    ):
        domain = nav.get_domain(name)
        assert domain is not None, name
        assert domain.has_root is False, name
        assert domain.entry == entry, name
        assert Direction.DOWN in domain.exit_directions, name


def test_page_is_ready_at_root(nav, window):
    register_window_nav(window, registry=nav)

    state = nav.restore_page(NAV_PAGE_ID)
    assert state is not None
    assert len(state.stack) == 2
    assert state.stack[0].domain_id == f"{NAV_PAGE_ID}::page_focus"
    assert state.stack[1].domain_id == ROOT_DOMAIN
    assert state.stack[1].current is None


def test_root_gateways_have_grid_and_child_domain(nav, window):
    register_window_nav(window, registry=nav)

    titlebar = nav.get_node("window.titlebar")
    content = nav.get_node("window.content")
    assert titlebar is not None and titlebar.child_domain == "titlebar"
    assert titlebar.grid == GridPlacement(0, 0)
    assert content is not None and content.child_domain == "content"
    assert content.grid == GridPlacement(1, 0)
    # overlay 的三页共用同一格：谁开着谁可见
    for node_id, child in (
        ("window.album", "album"),
        ("window.source", "source"),
        ("window.detail", "detail"),
    ):
        node = nav.get_node(node_id)
        assert node is not None
        assert node.child_domain == child
        assert node.grid == GridPlacement(2, 0)


def test_gateway_visibility_follows_overlay(nav, window):
    register_window_nav(window, registry=nav)

    def visible(node_id: str) -> bool:
        node = nav.get_node(node_id)
        assert node is not None and node.visible_fn is not None
        return bool(node.visible_fn())

    assert visible("window.album") is True
    assert visible("window.source") is False
    assert visible("window.detail") is False

    window.overlay.current = window.detail
    assert visible("window.album") is False
    assert visible("window.detail") is True

    # 空白 overlay（收起）：三页都不可见
    window.overlay.current = object()
    assert not any(
        visible(node_id)
        for node_id in ("window.album", "window.source", "window.detail")
    )


def test_leaf_nodes_declare_arrow_pass_through(nav, window):
    """让渡集按控件能力分三档（A1：不再一刀切全集）。"""
    register_window_nav(window, registry=nav)

    # 滚动网格 / 列表：方向键 + 回车全给控件
    for node_id in ("content.body", "album.cover", "album.trash",
                    "source.cards", "detail.image", "detail.strip"):
        node = nav.get_node(node_id)
        assert node is not None, node_id
        assert node.pass_through_keys == LEAF_PASS_THROUGH_KEYS, node_id
        # Esc / Tab 不让渡：那两下归 nav
        assert "esc" not in node.pass_through_keys
        assert "tab" not in node.pass_through_keys

    # 单行输入：只让渡 ←/→（↑/↓ 与回车归 nav）
    for node_id in ("detail.tagbar", "source.form"):
        node = nav.get_node(node_id)
        assert node is not None, node_id
        assert node.pass_through_keys == LEAF_PASS_THROUGH_HORIZONTAL, node_id
        assert {"up", "down", "enter"}.isdisjoint(node.pass_through_keys), node_id

    # 可纵向滚动的键值表：只让渡 ↑/↓
    meta = nav.get_node("panel.meta")
    assert meta is not None
    assert meta.pass_through_keys == LEAF_PASS_THROUGH_VERTICAL

    # 每个叶节点拿的是副本，改一个不影响别的
    first = nav.get_node("content.body")
    second = nav.get_node("album.cover")
    assert first is not None and second is not None
    assert first.pass_through_keys is not second.pass_through_keys


def test_tagbar_down_is_not_passed_through(nav, window):
    """标签栏（单行输入）上的 ↓ 必须归 nav，否则光标出不去（A1）。"""
    register_window_nav(window, registry=nav)
    window.overlay.current = window.detail
    nav.set_current(NAV_PAGE_ID, "window.detail")
    nav.enter(NAV_PAGE_ID, how="tab")           # → detail.image
    nav.move(NAV_PAGE_ID, Direction.DOWN)       # → detail.tagbar
    assert nav.current_frame(NAV_PAGE_ID).current == "detail.tagbar"

    assert nav.should_pass_through(NAV_PAGE_ID, Direction.LEFT) is True
    assert nav.should_pass_through(NAV_PAGE_ID, Direction.RIGHT) is True
    assert nav.should_pass_through(NAV_PAGE_ID, Direction.DOWN) is False
    assert nav.should_pass_through(NAV_PAGE_ID, Direction.UP) is False
    assert nav.should_pass_through(NAV_PAGE_ID, "enter") is False


def test_detail_down_chain_reaches_every_region(nav, window):
    """detail 域 DOWN 链：image → tagbar → strip → hud → panel（A1 验收）。"""
    register_window_nav(window, registry=nav)
    window.overlay.current = window.detail
    window.detail.panel.isVisible = lambda: True     # 面板打开
    nav.set_current(NAV_PAGE_ID, "window.detail")
    nav.enter(NAV_PAGE_ID, how="tab")
    assert nav.current_frame(NAV_PAGE_ID).current == "detail.image"

    landed: list[str] = []
    for _ in range(4):
        node = nav.move(NAV_PAGE_ID, Direction.DOWN)
        assert node is not None
        landed.append(node.id)
    assert landed == ["detail.tagbar", "detail.strip", "detail.hud",
                      "detail.panel"]


def test_album_down_reaches_trash(nav, window):
    """album 域 DOWN：cover → trash（A1 验收）。"""
    register_window_nav(window, registry=nav)
    nav.set_current(NAV_PAGE_ID, "window.album")
    nav.enter(NAV_PAGE_ID, how="tab")
    nav.move(NAV_PAGE_ID, Direction.DOWN)        # close → edit
    nav.move(NAV_PAGE_ID, Direction.DOWN)        # edit → cover
    assert nav.current_frame(NAV_PAGE_ID).current == "album.cover"

    node = nav.move(NAV_PAGE_ID, Direction.DOWN)
    assert node is not None and node.id == "album.trash"


def test_titlebar_right_stays_on_the_same_row(nav, window):
    """A4：标题栏 RIGHT 走网格扫描，不跨行（extra 右侧没有邻居）。"""
    register_window_nav(window, registry=nav)
    nav.set_current(NAV_PAGE_ID, "window.titlebar")
    nav.enter(NAV_PAGE_ID, how="tab")            # → titlebar.album

    right = nav.move(NAV_PAGE_ID, Direction.RIGHT)
    assert right is not None and right.id == "titlebar.search"
    right = nav.move(NAV_PAGE_ID, Direction.RIGHT)
    assert right is not None and right.id == "titlebar.extra"
    # 第 0 行最右：RIGHT 没有邻居，不跳行到 minimize(1,0)
    assert nav.move(NAV_PAGE_ID, Direction.RIGHT) is None
    assert nav.current_frame(NAV_PAGE_ID).current == "titlebar.extra"

    # 行内左移同理
    left = nav.move(NAV_PAGE_ID, Direction.LEFT)
    assert left is not None and left.id == "titlebar.search"


def test_disabled_button_is_not_a_landing_point(nav, window):
    """A4：disabled 按钮不被落点命中（方向键跳过它）。"""
    window.title_bar.search_button.isEnabled = lambda: False
    register_window_nav(window, registry=nav)

    search = nav.get_node("titlebar.search")
    assert search is not None
    assert nav.is_visible(search) is False

    nav.set_current(NAV_PAGE_ID, "window.titlebar")
    nav.enter(NAV_PAGE_ID, how="tab")            # → titlebar.album
    right = nav.move(NAV_PAGE_ID, Direction.RIGHT)
    assert right is not None and right.id == "titlebar.extra"


def test_leaf_nodes_focus_inner_control(nav, window):
    register_window_nav(window, registry=nav)

    form = nav.get_node("source.form")
    assert form is not None and form.focus_fn is not None
    form.focus_fn()
    window.source.detail_widget.title_line_edit.focus_calls == 1

    tagbar = nav.get_node("detail.tagbar")
    assert tagbar is not None and tagbar.focus_fn is not None
    tagbar.focus_fn()
    assert window.detail.tag_bar.tag_line_edit.focus_calls == 1

    body = nav.get_node("content.body")
    assert body is not None and body.focus_fn is not None
    body.focus_fn()
    assert window.content.focus_calls == 1


def test_button_nodes_click_their_widget(nav, window):
    register_window_nav(window, registry=nav)

    expectations = {
        "titlebar.album": window.title_bar.album_button,
        "album.close": window.album.close_button,
        "source.apply": window.source.apply_button,
        "detail.close": window.detail.close_button,
        "hud.trash": window.detail.trash_button,
        "panel.close": window.detail.panel.close_button,
    }
    for node_id, widget in expectations.items():
        node = nav.get_node(node_id)
        assert node is not None, node_id
        assert node.action is not None, node_id
        node.action()
        assert widget.clicks == 1, node_id


def test_source_form_is_hidden_until_opened(nav, window):
    register_window_nav(window, registry=nav)

    form = nav.get_node("source.form")
    assert form is not None and form.visible_fn is not None
    assert form.visible_fn() is False

    # 先打开导入源页，再看「表单是否被点开」
    window.overlay.current = window.source
    assert form.visible_fn() is False

    window.source.detail_container._visible = True
    assert form.visible_fn() is True


def test_detail_hud_and_panel_respect_visibility(nav, window):
    register_window_nav(window, registry=nav)
    window.overlay.current = window.detail      # 详情页开着

    hud = nav.get_node("detail.hud")
    close = nav.get_node("detail.close")
    panel = nav.get_node("detail.panel")
    assert hud is not None and close is not None and panel is not None
    assert hud.visible_fn is not None and close.visible_fn is not None

    # HUD 可见、面板关着
    assert close.visible_fn() is True
    assert panel.visible_fn() is False

    # 快速预览模式：HUD 整体隐藏
    window.detail.overlay._visible = False
    assert close.visible_fn() is False

    # 面板打开
    window.detail.panel.isVisible = lambda: True
    assert panel.visible_fn() is True


def test_wiring_is_idempotent(nav, window):
    register_window_nav(window, registry=nav)
    domain_count = len(nav.domains())
    node_count = sum(len(domain.nodes) for domain in nav.domains())

    # 模拟「上一轮已经进入过某个区域」
    nav.set_current(NAV_PAGE_ID, "window.content")
    nav.enter(NAV_PAGE_ID, how="tab")
    assert nav.current_frame(NAV_PAGE_ID).domain_id == "content"

    register_window_nav(window, registry=nav)

    assert len(nav.domains()) == domain_count
    assert sum(len(d.nodes) for d in nav.domains()) == node_count
    # 复位到根域虚拟根
    assert nav.current_frame(NAV_PAGE_ID).domain_id == ROOT_DOMAIN
    assert nav.current_frame(NAV_PAGE_ID).current is None


def test_wiring_skips_missing_regions(nav, window):
    """派生窗口砍掉某个区域：只少注册那几个节点，不崩。"""
    window.title_bar = None
    del window.album

    register_window_nav(window, registry=nav)

    assert nav.get_node("window.titlebar") is None
    assert nav.get_node("window.album") is None
    assert nav.get_node("window.content") is not None
    assert nav.get_domain("titlebar") is not None  # 域照旧存在（只是空）


# ==================== 导航语义（端到端，用假窗口） ====================

def test_tab_then_tab_enters_titlebar(nav, window):
    """Tab 先落到区域入口，再 Tab 进入区域内部（入口的 entry）。"""
    register_window_nav(window, registry=nav)

    first = nav.enter(NAV_PAGE_ID, how="tab")
    assert first is not None and first.id == "window.titlebar"
    assert nav.current_frame(NAV_PAGE_ID).domain_id == ROOT_DOMAIN

    second = nav.enter(NAV_PAGE_ID, how="tab")
    assert second is not None and second.id == "titlebar.album"
    assert nav.current_frame(NAV_PAGE_ID).domain_id == "titlebar"


def test_move_down_reaches_visible_overlay_page(nav, window):
    register_window_nav(window, registry=nav)
    nav.set_current(NAV_PAGE_ID, "window.content")

    # 相册页开着 → DOWN 落在相册入口
    node = nav.move(NAV_PAGE_ID, Direction.DOWN)
    assert node is not None and node.id == "window.album"

    # 换成详情页 → 同一格落到详情入口
    window.overlay.current = window.detail
    nav.set_current(NAV_PAGE_ID, "window.content")
    node = nav.move(NAV_PAGE_ID, Direction.DOWN)
    assert node is not None and node.id == "window.detail"

    # 收起 overlay → 那一格没有可见目标，DOWN 返回 None（不误落）
    window.overlay.current = object()
    nav.set_current(NAV_PAGE_ID, "window.content")
    assert nav.move(NAV_PAGE_ID, Direction.DOWN) is None


def test_move_within_titlebar_and_exit_down(nav, window):
    register_window_nav(window, registry=nav)
    nav.set_current(NAV_PAGE_ID, "window.titlebar")
    nav.enter(NAV_PAGE_ID, how="tab")           # → titlebar.album

    right = nav.move(NAV_PAGE_ID, Direction.RIGHT)   # 别名 → next
    assert right is not None and right.id == "titlebar.search"

    # DOWN 按列对齐：search 在第 1 列 → 落到第 1 列的 maximize
    down = nav.move(NAV_PAGE_ID, Direction.DOWN)
    assert down is not None and down.id == "titlebar.maximize"

    nav.move(NAV_PAGE_ID, Direction.DOWN)            # 行 1 边界 → exit_back
    assert nav.current_frame(NAV_PAGE_ID).domain_id == ROOT_DOMAIN


def test_esc_pops_region_back_to_root(nav, window):
    register_window_nav(window, registry=nav)
    nav.set_current(NAV_PAGE_ID, "window.album")
    nav.enter(NAV_PAGE_ID, how="tab")               # → album.close

    nav.esc(NAV_PAGE_ID)
    assert nav.current_frame(NAV_PAGE_ID).domain_id == ROOT_DOMAIN


def test_should_pass_through_in_leaf_domain(nav, window):
    """进了内容层叶域，方向键与回车让渡给控件，Esc / Tab 仍归 nav。"""
    register_window_nav(window, registry=nav)
    nav.set_current(NAV_PAGE_ID, "window.content")
    nav.enter(NAV_PAGE_ID, how="tab")               # → content.body

    for key in (Direction.UP, Direction.DOWN, Direction.LEFT, Direction.RIGHT):
        assert nav.should_pass_through(NAV_PAGE_ID, key) is True
    assert nav.should_pass_through(NAV_PAGE_ID, "enter") is True
    assert nav.should_pass_through(NAV_PAGE_ID, "esc") is False
    assert nav.should_pass_through(NAV_PAGE_ID, "tab") is False


# ==================== 真窗口：属性名一致性 ====================

@pytest.fixture(scope="module")
def qapp() -> Iterator[Any]:
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


def test_real_window_attribute_names_line_up(qapp):
    """``Window`` 的真实控件名必须与 wiring 里写的一致（这条红了就是改名了）。"""
    from albuswall.ui.window import Window

    window = Window()
    try:
        nav = register_window_nav(window, registry=NavRegistry())

        # 每个区域至少有一个真实节点（不是 None 被跳过）
        for domain_id, minimum in (
            ("window", 5),
            ("titlebar", 6),
            ("content", 1),
            ("album", 5),
            ("source", 5),
            ("detail", 6),
            ("detail_hud", 5),
            ("detail_panel", 2),
        ):
            nodes = nav.nodes_in(domain_id)
            assert len(nodes) >= minimum, (domain_id, [n.id for n in nodes])

        # 叶节点的 widget 是真实控件，焦点函数可用
        body = nav.get_node("content.body")
        assert body is not None and body.widget is window.content
        assert body.pass_through_keys == LEAF_PASS_THROUGH_KEYS

        # 相册页当前没开 → 入口不可见、源/详情入口也不可见
        assert nav.get_node("window.album").visible_fn() is False
        window.show_album()
        assert nav.get_node("window.album").visible_fn() is True
    finally:
        window.close()
        window.deleteLater()


# ==================== boot 接线（phase 3） ====================

def test_setup_ui_registers_window_nav_into_boot(qapp, monkeypatch):
    """``bootstrap.setup_ui`` 的 phase 3：容器登记 + 窗口结构注册。

    这是**唯一**的接线点（``plugins/builtin_ui`` 的 ``init`` hook 调它），
    所以这里直接验证「跑完 boot，容器里那个注册表就有主窗口的域 / 节点」。
    """
    from albuswall.core.container import Container
    from albuswall.ui import bootstrap
    from albuswall.ui.i18n import get_manager
    from albuswall.ui.nav import get_default_registry

    from albuswall.ui.languages import DEFAULT_LANGUAGE

    # 语言解析走 configue 的进程级单例，会被其它用例留下的全局状态影响；
    # 本用例只关心 phase 3 的导航接线，把语言固定成默认值。
    monkeypatch.setattr(
        bootstrap, "configured_language", lambda: DEFAULT_LANGUAGE)

    container = Container()
    singleton = get_default_registry()
    window = None
    try:
        assert container.get(bootstrap.NAV_REGISTRY_KEY) is None

        window = bootstrap.setup_ui(container, qapp)

        nav = container.get(bootstrap.NAV_REGISTRY_KEY)
        assert nav is singleton, "boot 必须往容器那一格里写"
        assert nav.get_domain(ROOT_DOMAIN) is not None
        assert nav.get_node("window.titlebar") is not None
        assert nav.get_node("content.body") is not None
        assert nav.restore_page(NAV_PAGE_ID) is not None

        # 注册的正是这个窗口的控件（不是上一个用例留下的）
        titlebar_gateway = nav.get_node("window.titlebar")
        assert titlebar_gateway is not None
        assert titlebar_gateway.widget is window.title_bar
    finally:
        singleton.clear()
        if window is not None:
            window.close()
            window.deleteLater()
        get_manager().reset()
