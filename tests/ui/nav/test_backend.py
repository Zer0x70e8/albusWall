#
"""``FocusBackend`` 协议 + 受控执行（``focus_node`` 验证 / 回滚 / 挂起）。

三条边界，全部**不需要** ``QApplication``：

* **协议本身**：``nav/backend.py`` 只依赖 ``typing``，假后端就能测；
* **执行语义**：``focus_node`` 在「后端看不到落点」时必须返回 ``False`` 并
  warning，在「观测不到焦点」时按成功处理（无头环境不该整条回滚）；
* **动作结果**：``last_action_ok`` 区分「返回 None 但状态变了」与「什么都没
  发生」，``suspend`` / ``resume`` 与 ``snapshot`` / ``restore`` 供分发器用。

另有一条**结构**约束：``nav`` 的核心模块不得 import Qt（子进程里验证）。
"""

from __future__ import annotations

import logging
import sys

import pytest

from albuswall.ui.nav.backend import FocusBackend, NullFocusBackend
from albuswall.ui.nav.model import Direction, Domain, GridPlacement, Node
from albuswall.ui.nav.registry import (
    NavRegistry,
    get_default_registry,
    set_default_backend,
)


# ==================== 替身 ====================

class FakeWidget:
    """能记「焦点在自己身上没有」的假控件。"""

    def __init__(self) -> None:
        self.apply_calls = 0
        self.keymap_passthrough = False

    def setFocus(self) -> None:  # noqa: N802 - 对接 Qt 命名
        self.apply_calls += 1


class FakeBackend:
    """可控的焦点后端：``lands`` 决定 ``apply_focus`` 是否真的落上。"""

    def __init__(self, *, lands: bool = True, observable: bool = True) -> None:
        self.lands = lands
        self.observable = observable
        self.applied: list[str] = []
        self.ensured: list[str] = []
        self._widget = None

    def apply_focus(self, node: Node) -> None:
        self.applied.append(node.id)
        self._widget = node.widget if self.lands else object()

    def current_focus_chain(self) -> tuple[object, ...]:
        if not self.observable:
            return ()
        return () if self._widget is None else (self._widget, "parent-widget")

    def ensure_focusable(self, node: Node) -> None:
        self.ensured.append(node.id)

    def widget_to_node_id(self, widget: object) -> str | None:
        return "n1" if widget is not None else None


@pytest.fixture
def reg() -> NavRegistry:
    """``page``（有根）里两个按钮 + 一个容器 → ``child``（无根）。"""
    r = NavRegistry()
    r.register_domain(Domain(id="page", has_root=True, is_real_page=True))
    r.register_domain(Domain(id="child", has_root=False, entry="c1"))
    r.register_node(
        Node(id="n1", widget=FakeWidget(), grid=GridPlacement(0, 0)),
        domain_id="page")
    r.register_node(
        Node(id="n2", widget=FakeWidget(), grid=GridPlacement(0, 1)),
        domain_id="page")
    r.register_node(
        Node(id="pedal", widget=FakeWidget(), child_domain="child",
             focusable=False),
        domain_id="page")
    r.register_node(
        Node(id="c1", widget=FakeWidget(), grid=GridPlacement(0, 0)),
        domain_id="child")
    r.ensure_page("p", root_domain="page")
    return r


# ==================== 协议与默认后端 ====================

def test_fake_backend_satisfies_protocol():
    """``FocusBackend`` 是结构化子类型：不需要继承，方法齐了就算。"""
    assert isinstance(FakeBackend(), FocusBackend)
    assert isinstance(NullFocusBackend(), FocusBackend)


def test_null_backend_calls_set_focus_and_plays_back():
    w = FakeWidget()
    backend = NullFocusBackend()
    node = Node(id="n", widget=w)

    assert backend.current_focus_chain() == ()
    backend.ensure_focusable(node)
    backend.apply_focus(node)

    assert w.apply_calls == 1
    assert backend.current_focus_chain() == (w,)
    assert backend.widget_to_node_id(w) is None


def test_null_backend_tolerates_widget_without_set_focus():
    node = Node(id="n", widget=object())
    NullFocusBackend().apply_focus(node)        # 不抛


def test_registry_defaults_to_null_backend(reg):
    assert isinstance(reg.backend, NullFocusBackend)


def test_backend_injection_and_reset():
    backend = FakeBackend()
    r = NavRegistry(backend=backend)
    assert r.backend is backend
    r.set_backend(None)
    assert isinstance(r.backend, NullFocusBackend)

    default = get_default_registry()
    try:
        set_default_backend(backend)
        assert default.backend is backend
    finally:
        set_default_backend(None)
    assert isinstance(default.backend, NullFocusBackend)


# ==================== focus_node 的验证 ====================

def test_focus_node_verifies_landing():
    backend = FakeBackend(lands=True)
    r = NavRegistry(backend=backend)
    r.register_domain(Domain(id="d"))
    node = r.register_node(Node(id="n", widget=FakeWidget()), domain_id="d")

    assert r.focus_node(node) is True
    assert backend.ensured == ["n"]
    assert backend.applied == ["n"]


def test_focus_node_reports_failure_with_warning(caplog):
    backend = FakeBackend(lands=False)
    r = NavRegistry(backend=backend)
    r.register_domain(Domain(id="d"))
    node = r.register_node(Node(id="n", widget=FakeWidget()), domain_id="d")

    with caplog.at_level(logging.WARNING):
        assert r.focus_node(node) is False
    assert any("focus_node n failed" in record.message
               for record in caplog.records
               if record.levelno == logging.WARNING)


def test_focus_node_unobservable_chain_is_optimistic():
    """观测不到焦点（无 WM / offscreen）不判失败：否则整条导航会退化。"""
    backend = FakeBackend(lands=False, observable=False)
    r = NavRegistry(backend=backend)
    r.register_domain(Domain(id="d"))
    node = r.register_node(Node(id="n", widget=FakeWidget()), domain_id="d")

    assert r.focus_node(node) is True


def test_focus_node_trusts_focus_fn():
    """``focus_fn`` 是调用方自定义的执行：视为成功，不做链验证（契约 4）。"""
    backend = FakeBackend(lands=False)
    r = NavRegistry(backend=backend)
    r.register_domain(Domain(id="d"))
    called: list[int] = []
    node = r.register_node(
        Node(id="n", widget=FakeWidget(), focus_fn=lambda: called.append(1)),
        domain_id="d")

    assert r.focus_node(node) is True
    assert called == [1]
    assert backend.applied == []        # 没走 apply_focus 路径


def test_focus_node_uses_identity_not_equality():
    """控件对象可能自定义 ``__eq__``（抛异常 / 恒真）：验证用同一性。"""
    class Weird:
        def __eq__(self, other):  # noqa: D401 - 故意搞坏相等性
            raise RuntimeError("no equality for you")

        def __hash__(self) -> int:
            return id(self)

    class WeirdBackend(FakeBackend):
        def current_focus_chain(self):
            return (Weird(),)

    r = NavRegistry(backend=WeirdBackend())
    r.register_domain(Domain(id="d"))
    node = r.register_node(Node(id="n", widget=Weird()), domain_id="d")
    assert r.focus_node(node) is False      # 不是同一个对象 → 失败，但不炸


# ==================== last_action_ok ====================

def test_last_action_ok_true_on_landing(reg):
    reg.set_current("p", "n1")
    assert reg.move("p", Direction.RIGHT) is not None
    assert reg.last_action_ok is True


def test_last_action_ok_false_at_boundary(reg):
    reg.set_current("p", "n1")
    assert reg.move("p", Direction.LEFT) is None
    assert reg.last_action_ok is False


def test_last_action_ok_true_when_state_changed_without_node(reg):
    """``esc`` 在 ``esc_clears_current`` 域里返回 ``None``，但状态真的变了。"""
    reg.get_domain("page").esc_clears_current = True
    reg.set_current("p", "n1")
    assert reg.esc("p") is None
    assert reg.last_action_ok is True
    assert reg.current_frame("p").current is None


def test_last_action_ok_tracks_push_with_virtual_root():
    """推入有根子域返回 ``None``（停虚拟根），但栈深变了 → 有进展。"""
    r = NavRegistry()
    r.register_domain(Domain(id="page", has_root=True, is_real_page=True))
    r.register_domain(Domain(id="child", has_root=True))
    r.register_node(Node(id="g", widget=object(), child_domain="child"),
                    domain_id="page")
    r.ensure_page("p", root_domain="page")
    r.set_current("p", "g")
    assert r.enter("p", how="enter") is None
    assert r.last_action_ok is True


# ==================== suspend / resume ====================

@pytest.mark.parametrize("action", [
    lambda reg: reg.move("p", Direction.RIGHT),
    lambda reg: reg.enter("p", how="enter"),
    lambda reg: reg.exit_back("p"),
    lambda reg: reg.esc("p"),
    lambda reg: reg.shift_esc("p"),
    lambda reg: reg.jump_to_page_root("p"),
])
def test_suspended_actions_are_inert(reg, action):
    reg.set_current("p", "n1")
    before = reg.snapshot("p")
    reg.suspend("p")
    try:
        assert action(reg) is None
        assert reg.snapshot("p") == before
        assert reg.last_action_ok is False
        assert reg.is_suspended("p") is True
    finally:
        reg.resume("p")
    assert reg.is_suspended("p") is False


def test_resume_restores_actions(reg):
    reg.set_current("p", "n1")
    reg.suspend("p")
    reg.resume("p")
    assert reg.move("p", Direction.RIGHT) is not None


def test_suspend_is_idempotent_and_page_scoped(reg):
    other = NavRegistry()
    other.register_domain(Domain(id="page", has_root=True, is_real_page=True))
    other.register_node(Node(id="n1", widget=object()), domain_id="page")
    other.ensure_page("p", root_domain="page")

    reg.suspend("p")
    reg.suspend("p")                    # 幂等（记录在 set 里）
    reg.resume("p")
    assert reg.is_suspended("p") is False
    # 另一个注册表不受影响
    other.set_current("p", "n1")
    other.suspend("other-page")         # 未建页面也不会炸


def test_clear_releases_suspension(reg):
    reg.suspend("p")
    reg.clear()
    assert reg.is_suspended("p") is False
    assert reg.last_action_ok is False


# ==================== snapshot / restore（回滚） ====================

def test_snapshot_restore_roundtrip(reg):
    reg.set_current("p", "pedal")
    reg.enter("p", how="tab")               # 推入 child 域
    assert reg.current_frame("p").domain_id == "child"

    snap = reg.snapshot("p")
    reg.exit_back("p")
    reg.set_current("p", "n2")
    assert reg.current_frame("p").current == "n2"

    assert reg.restore("p", snap) is True
    assert reg.current_frame("p").domain_id == "child"
    assert reg.current_frame("p").current == "c1"


def test_restore_rebuilds_popped_frames(reg):
    """动作把帧弹掉了也能回滚：快照里的数据够重建（不是只截断）。"""
    reg.set_current("p", "pedal")
    reg.enter("p", how="tab")               # 推入 child 域，栈深 3
    deep = reg.snapshot("p")
    reg.exit_back("p")                      # 弹回 page（栈深 2）

    assert reg.restore("p", deep) is True
    assert reg.current_frame("p").domain_id == "child"
    assert reg.current_frame("p").current == "c1"


def test_restore_refuses_unregistered_domain(reg):
    """快照里的域已被反注册：拒绝恢复，不造悬空栈帧。"""
    reg.set_current("p", "pedal")
    reg.enter("p", how="tab")
    snap = reg.snapshot("p")
    reg.exit_back("p")
    after = reg.snapshot("p")

    reg.unregister_domain("child")
    assert reg.restore("p", snap) is False
    assert reg.snapshot("p") == after


def test_restore_unknown_page(reg):
    assert reg.restore("nope", ()) is False
    assert reg.snapshot("nope") == ()


# ==================== 结构约束：nav 核心不 import Qt ====================

_QT_FREE_MODULES = (
    "model.py",
    "registry.py",
    "errors.py",
    "backend.py",
    "wiring.py",
)


def test_nav_core_modules_import_no_qt():
    """静态检查：nav 核心模块里不许出现任何 Qt / shiboken import。

    用 AST 而不是「子进程里看 ``sys.modules``」：``import albuswall.ui.nav``
    会先执行 ``albuswall.ui.__init__``（它本来就 import Qt），子进程方案会把
    这条本来就成立的间接依赖误判成违规。
    """
    import ast
    import pathlib

    package_dir = pathlib.Path(
        sys.modules["albuswall.ui.nav.model"].__file__).parent
    offenders: list[str] = []
    for name in _QT_FREE_MODULES:
        path = package_dir / name
        assert path.is_file(), path
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            else:
                continue
            for module in modules:
                head = module.split(".")[0]
                if head in ("PySide6", "shiboken", "shiboken6"):
                    offenders.append(f"{name}: {module}")
    assert offenders == [], f"nav 核心模块 import 了 Qt: {offenders}"
