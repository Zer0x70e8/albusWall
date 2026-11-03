#
"""硬接管骨架的结构性守卫（AST / 源码结构断言）。

目的：**未来任何让硬接管失效的改动，必须在 CI 阶段红**，而不是等用户人肉排查。
这不是修 bug，是加防线 —— 所以这里只读源码、不改任何被测代码。

五条守卫（S1–S5）：

============================  ==========================================
守卫                          锁的是什么
============================  ==========================================
S1 ``_report_stray_focus``    探针只许"事后记录"：必须有 warning 级日志，
                              不许把"纠正"（setFocus / nav 动作 / 拉回）塞进去
S2 ``_handle_tab``            Tab / Backtab 不许 ``ignore`` 给 Qt 焦点链，
                              且必须存在"已消费"的返回路径
S3 ``_on_overlay_page_changed`` 关页落点不许直接 ``setFocus``，必须经 nav 决策
S4 容器 NoFocus 逻辑           只许在 ``nav/qt.py`` 里（语义位置，不只是 import 位置）
S5 ``PassthroughStack``       ``_release_focus`` 必须存在、含 ``clearFocus``、
                              且被切页路径调用（否则关页焦点落回占位页）
============================  ==========================================

设计原则（别把守卫写成"当前实现快照"）：

* 只用 ``ast`` 判定**语义在不在**，不看行数 / 顺序 / 变量名；
* 注释与 docstring 里出现 ``setFocus`` / ``NoFocus`` 这些词**合法**——AST 里
  它们不是 ``Call`` / ``Attribute`` 节点；
* 锁的是骨架函数名（``_report_stray_focus`` / ``_handle_tab`` /
  ``_on_overlay_page_changed`` / ``ensure_focusable`` / ``_release_focus``）。
  重命名这些函数是架构级操作：PR 里要单独说明并同步改守卫，不是"测试红了
  改一改"；
* 每条守卫都有一条**反证**（在内存里改源码字符串，不落地）：证明守卫真的会
  拦下"弱化硬接管"的改动，而不是恒真。
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[4]
SRC = REPO / "src" / "albuswall"
NAV_DIR = SRC / "ui" / "nav"
PRESENTER_PATH = SRC / "ui" / "presenter" / "nav_presenter.py"
PASSTHROUGH_PATH = SRC / "ui" / "widgets" / "passthrough_stack_widget.py"

#: 够"高"的日志级别：探针必须留下可见痕迹（debug / info 不算）
HIGH_LOG_LEVELS = frozenset({"warning", "warn", "error", "critical", "exception"})

#: 探针里绝不允许出现的调用（按**末段名**判定：``nav.enter`` /
#: ``self._nav.enter`` / ``foo.exit_back`` 一律命中）。探针只能记录，
#: 任何"改 nav 状态 / 动焦点"的动作都是掩盖。
FORBIDDEN_PROBE_ACTIONS = frozenset({
    "setFocus", "setCurrent", "setCurrentIndex",
    "enter", "exit_back", "move", "esc", "shift_esc",
    "tab_next", "tab_prev", "jump_to_page_root", "set_current",
})

#: 探针里绝不允许出现的"拉回 / 纠正"辅助函数（含将来可能出现的同义名）
FORBIDDEN_PROBE_HELPERS = ("_restore", "_pullback", "_pull_back",
                           "sync_to_widget")


# ==================== AST 小工具 ====================

def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _parse(source: str) -> ast.Module:
    return ast.parse(source)


def _method_snippet(source: str, name: str) -> str:
    """把某个方法**连同缩进**抠出来，包进一个 ``class Stub`` 返回。

    反证用：在内存里改这段字符串，喂回同一个守卫，验证它会红。
    """
    tree = _parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == name:
            lines = source.splitlines(keepends=True)
            start = node.lineno - 1
            # 带上装饰器（如果有）
            if node.decorator_list:
                start = min(item.lineno for item in node.decorator_list) - 1
            return "class Stub:\n" + "".join(lines[start:node.end_lineno])
    raise AssertionError(f"源码里找不到方法 {name}")


def _method(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == name:
            return node
    raise AssertionError(f"源码里找不到方法 {name}")


def _dotted(func: ast.AST) -> str | None:
    """调用的点号名：``logger.warning`` / ``self._focus`` / ``nav.enter``。"""
    parts: list[str] = []
    node: Any = func
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _attr_chain(node: ast.Attribute) -> str:
    """属性链的完整文本（``Qt.FocusPolicy.NoFocus``）；非纯名字链返回 ``""``。"""
    dotted = _dotted(node)
    return dotted or ""


def _call_names(node: ast.AST) -> list[str]:
    """``node`` 子树里所有调用的点号名。"""
    names: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            dotted = _dotted(child.func)
            if dotted:
                names.append(dotted)
    return names


def _attribute_names(node: ast.AST) -> list[str]:
    """``node`` 子树里所有属性访问的末段名（``x.setFocus`` → ``setFocus``）。"""
    return [child.attr for child in ast.walk(node)
            if isinstance(child, ast.Attribute)]


def _docstring_ids(tree: ast.AST) -> set[int]:
    """所有 docstring 常量的 ``id()``（字符串里提到关键字必须合法）。"""
    ids: set[int] = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                and isinstance(first.value.value, str):
            ids.add(id(first.value))
    return ids


def _returns_true(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Return) and isinstance(child.value, ast.Constant) \
                and child.value.value is True:
            return True
    return False


# ==================== S1：探针不得弱化 ====================

def probe_violations(source: str) -> list[str]:
    """``_report_stray_focus`` 的违规项（空列表 = 合规）。"""
    body = _method(_parse(source), "_report_stray_focus")
    violations: list[str] = []

    calls = _call_names(body)
    attributes = _attribute_names(body)

    # 必须：至少一次「够高」的日志调用（不锁变量名，只锁级别后缀）
    logged = [name for name in calls
              if name.rsplit(".", 1)[-1] in HIGH_LOG_LEVELS]
    if not logged:
        violations.append(
            "探针没有 warning 级（或更高）日志调用：探针失去可观测性")

    # 不得：任何"纠正焦点 / 改 nav 状态"的调用
    for name in calls:
        tail = name.rsplit(".", 1)[-1]
        if tail in FORBIDDEN_PROBE_ACTIONS:
            violations.append(
                f"探针里出现纠正动作 {name}(...)（探针只能记录）")
        for helper in FORBIDDEN_PROBE_HELPERS:
            if helper in name:
                violations.append(
                    f"探针里调用了拉回 / 纠正辅助 {name}(...)（掩盖而非诚信）")

    # 不得：即使没调用，只是引用这些纠正函数也违规（防止将来包一层再调）
    for attr in attributes:
        for helper in FORBIDDEN_PROBE_HELPERS:
            if helper == attr:
                violations.append(f"探针引用了纠正辅助 .{attr}")

    return violations


# ==================== S2：Tab 不放行给 Qt ====================

def tab_violations(source: str) -> list[str]:
    """``_handle_tab`` 的违规项。"""
    body = _method(_parse(source), "_handle_tab")
    violations: list[str] = []

    for name in _call_names(body):
        if name.rsplit(".", 1)[-1] == "ignore":
            violations.append(
                f"_handle_tab 里出现 {name}(...)：会把 Tab 交回 Qt 焦点链")

    if not _returns_true(body):
        violations.append(
            "_handle_tab 里没有任何「已消费」路径（return True）："
            "Tab 会冒泡给 QWidget::focusNextPrevChild")

    return violations


# ==================== S3：关页落点不得直接 setFocus ====================

def _overlay_close_violations(source: str) -> list[str]:
    tree = _parse(source)
    body = _method(tree, "_on_overlay_page_changed")
    violations: list[str] = []

    for name in _call_names(body):
        if name.rsplit(".", 1)[-1] == "setFocus":
            violations.append(
                f"_on_overlay_page_changed 里直接调用 {name}(...)："
                "落点绕过 nav 栈语义与 focus_node 验证")

    # 必须：经 nav 决策（直接调 nav 动作，或走本类的落点辅助）
    calls = _call_names(body)
    decides = [
        name for name in calls
        if name in {"self._landing_after_overlay_closed", "self._focus"}
        or name in {"nav.enter", "nav.exit_back", "nav.set_current",
                    "nav.focus_node", "nav.move", "nav.esc"}
    ]
    if not decides:
        violations.append(
            "_on_overlay_page_changed 没有经 nav 决定落点"
            "（应调 nav.enter / nav.exit_back / nav.focus_node 或其辅助）")

    return violations


def landing_helper_violations(source: str) -> list[str]:
    """落点辅助链本身必须真的在算 nav 落点（防止把决策搬到别处偷懒）。"""
    tree = _parse(source)
    violations: list[str] = []

    landing = _call_names(_method(tree, "_landing_after_overlay_closed"))
    if "nav.exit_back" not in landing:
        violations.append(
            "_landing_after_overlay_closed 没有 exit_back 回根帧")
    if not ({"nav.enter", "nav.set_current"} & set(landing)):
        violations.append(
            "_landing_after_overlay_closed 没有 enter / set_current 选落点")

    focus = _call_names(_method(tree, "_focus"))
    if "nav.focus_node" not in focus:
        violations.append("_focus 没有走 nav.focus_node（受控执行 + 验证）")

    return violations


# ==================== S4：容器 NoFocus 逻辑只在 qt.py ====================

def container_focus_violations(sources: dict[str, str]) -> list[str]:
    """``{文件名: 源码}`` → 容器 NoFocus 逻辑的违规项。"""
    violations: list[str] = []
    qt_source = sources.get("qt.py", "")

    for name, source in sources.items():
        if name == "qt.py":
            continue
        tree = _parse(source)
        docstrings = _docstring_ids(tree)

        for node in ast.walk(tree):
            # (a) Qt.FocusPolicy.* 属性链（哪怕没 import Qt，用 getattr 拼出来的）
            if isinstance(node, ast.Attribute):
                chain = _attr_chain(node)
                if "FocusPolicy" in chain:
                    violations.append(
                        f"{name} 里出现 {chain}：容器焦点策略必须在 nav/qt.py")
                if node.attr in {"setFocusPolicy", "focusPolicy"}:
                    violations.append(
                        f"{name} 里访问 .{node.attr}：焦点策略读写必须在 nav/qt.py")
            # (b) getattr(..., "NoFocus") 之类的字符串绕法
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and id(node) not in docstrings \
                    and node.value in {"NoFocus", "FocusPolicy", "setFocusPolicy"}:
                violations.append(
                    f"{name} 里出现字符串 {node.value!r}"
                    "（getattr 绕法）：容器焦点策略必须在 nav/qt.py")
            # (c) 越界定义 ensure_focusable（协议与 Null 实现在 backend.py）
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and node.name == "ensure_focusable" \
                    and name not in {"backend.py"}:
                violations.append(
                    f"{name} 定义了 ensure_focusable：执行细节只允许在 "
                    "nav/backend.py（协议 / 空实现）与 nav/qt.py（Qt 实现）")

    # 语义在位：qt.py 的 ensure_focusable 必须真的有「容器 → NoFocus」分支
    if not qt_source:
        violations.append("缺少 nav/qt.py")
        return violations
    ensure = _method(_parse(qt_source), "ensure_focusable")
    # 容器判定可能写成属性访问（node.child_domain）也可能是 getattr 的字符串
    reads_child_domain = any(
        (isinstance(child, ast.Attribute) and child.attr == "child_domain")
        or (isinstance(child, ast.Constant) and child.value == "child_domain")
        for child in ast.walk(ensure))
    sets_nofocus = any(
        name.endswith("setFocusPolicy") or name.endswith("set_policy")
        for name in _call_names(ensure))
    has_nofocus_literal = any(
        "NoFocus" in _attr_chain(child)
        for child in ast.walk(ensure) if isinstance(child, ast.Attribute))
    if not reads_child_domain:
        violations.append(
            "qt.py 的 ensure_focusable 不再判断 child_domain"
            "（容器规则消失了？）")
    if not (sets_nofocus or has_nofocus_literal):
        violations.append(
            "qt.py 的 ensure_focusable 不再设置容器焦点策略"
            "（NoFocus 分支消失了？）")

    return violations


# ==================== S5：PassthroughStack 焦点交还 ====================

#: 切页路径：至少两个必须交还焦点，且 ``show_blank``（关页主路径）必须交还
RELEASE_FOCUS_ROUTES = ("show_blank", "setCurrentWidget", "setCurrentIndex")


def passthrough_violations(source: str) -> list[str]:
    """``PassthroughStack`` 焦点交还逻辑的违规项。"""
    tree = _parse(source)
    violations: list[str] = []

    stack = next(
        (node for node in ast.walk(tree)
         if isinstance(node, ast.ClassDef) and node.name == "PassthroughStack"),
        None)
    if stack is None:
        return ["找不到 PassthroughStack 类"]

    release = next(
        (node for node in stack.body
         if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
         and node.name == "_release_focus"),
        None)
    if release is None:
        return ["PassthroughStack 缺少 _release_focus（切页前交还焦点）"]

    if not any(name.endswith("clearFocus") for name in _call_names(release)):
        violations.append("_release_focus 里没有 clearFocus 调用")

    routes_calling = [
        route for route in RELEASE_FOCUS_ROUTES
        if any(node.name == route for node in stack.body
               if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)))
        and any("_release_focus" == name.rsplit(".", 1)[-1]
                for name in _call_names(next(
                    node for node in stack.body
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name == route)))
    ]
    if len(routes_calling) < 2:
        violations.append(
            "切页路径调用 _release_focus 的少于 2 条："
            f"实际 {routes_calling}")
    if "show_blank" not in routes_calling:
        violations.append(
            "show_blank（关页主路径）没有调用 _release_focus："
            "关页后焦点会落回空白占位页")

    return violations


# ==================== 真源码：五条守卫 ====================

def _nav_sources() -> dict[str, str]:
    return {path.name: _read(path) for path in sorted(NAV_DIR.glob("*.py"))}


def test_probe_is_not_weakened():
    """S1：``_report_stray_focus`` 只记录、不纠正。"""
    violations = probe_violations(_read(PRESENTER_PATH))
    assert not violations, "探针被弱化了：\n" + "\n".join(violations)


def test_tab_is_never_ignored():
    """S2：``_handle_tab`` 不 ``ignore``，且存在消费路径。"""
    violations = tab_violations(_read(PRESENTER_PATH))
    assert not violations, "Tab 可能被交回 Qt 焦点链：\n" + "\n".join(violations)


def test_overlay_close_never_directly_sets_focus():
    """S3：关页落点必须经 nav 决定，不许直接 ``setFocus``。"""
    source = _read(PRESENTER_PATH)
    violations = _overlay_close_violations(source)
    violations += landing_helper_violations(source)
    assert not violations, "关页落点绕过了 nav：\n" + "\n".join(violations)


def test_container_no_focus_logic_stays_in_qt_backend():
    """S4：容器 NoFocus 逻辑只在 ``nav/qt.py``。"""
    violations = container_focus_violations(_nav_sources())
    assert not violations, "容器焦点策略跑出了 qt.py：\n" + "\n".join(violations)


def test_passthrough_release_focus_is_wired():
    """S5：``_release_focus`` 存在、``clearFocus`` 在、且切页路径都调它。"""
    violations = passthrough_violations(_read(PASSTHROUGH_PATH))
    assert not violations, "切页焦点交还链路断了：\n" + "\n".join(violations)


# ==================== 反证：守卫真的会红（内存里改，不落地）====================

def test_probe_guard_catches_a_pullback_counterexample():
    """反证 S1：往探针里塞一次 ``setFocus`` / 降级日志 / nav 动作都必须红。"""
    snippet = _method_snippet(_read(PRESENTER_PATH), "_report_stray_focus")
    assert not probe_violations(snippet), "基线：真源码的探针本该合规"

    pullback = snippet.replace(
        "        nav = self._nav",
        "        self.target_widget.setFocus()\n        nav = self._nav", 1)
    assert pullback != snippet
    assert probe_violations(pullback), "拉回焦点进探针却没被拦下"

    quiet = snippet.replace("logger.warning(", "logger.debug(", 1)
    assert quiet != snippet
    assert probe_violations(quiet), "探针日志被降级成 debug 却没被拦下"

    navigate = snippet.replace(
        "        nav = self._nav",
        "        self._nav.enter('main')\n        nav = self._nav", 1)
    assert probe_violations(navigate), "探针里改 nav 状态却没被拦下"


def test_tab_guard_catches_an_ignore_counterexample():
    """反证 S2：注入 ``event.ignore()`` 必须红。"""
    snippet = _method_snippet(_read(PRESENTER_PATH), "_handle_tab")
    assert not tab_violations(snippet), "基线：真源码的 _handle_tab 本该合规"

    ignoring = snippet.replace(
        "        shift = self._is_shift_tab(event)",
        "        event.ignore()\n        shift = self._is_shift_tab(event)", 1)
    assert ignoring != snippet
    assert tab_violations(ignoring), "ignore 注入后没被拦下"

    # 去掉所有消费路径 → 也必须红
    no_consume = snippet
    while "return True" in no_consume:
        no_consume = no_consume.replace("return True", "return False", 1)
    assert tab_violations(no_consume), "没有消费路径却没被拦下"


def test_overlay_close_guard_catches_a_direct_set_focus_counterexample():
    """反证 S3：把 ``self._focus`` 换成 ``widget.setFocus`` 必须红。"""
    snippet = _method_snippet(_read(PRESENTER_PATH), "_on_overlay_page_changed")
    assert not _overlay_close_violations(snippet), "基线：真源码本该合规"

    bypass = snippet.replace(
        'self._focus(nav, node, "overlay-closed")',
        "node.widget.setFocus()", 1)
    assert bypass != snippet
    assert _overlay_close_violations(bypass), "直接 setFocus 没被拦下"


def test_container_guard_catches_logic_moved_out_of_qt_py():
    """反证 S4：把 NoFocus 逻辑挪到别的 nav 模块必须红。"""
    sources = _nav_sources()
    assert not container_focus_violations(sources), "基线：真源码本该合规"

    leaked = dict(sources)
    leaked["registry.py"] = sources["registry.py"] + (
        "\n\ndef _sneak(node):\n"
        "    node.widget.setFocusPolicy(Qt.FocusPolicy.NoFocus)\n")
    assert container_focus_violations(leaked), "搬出去的容器逻辑没被拦下"

    sneaky_getattr = dict(sources)
    sneaky_getattr["wiring.py"] = sources["wiring.py"] + (
        "\n\ndef _sneak(widget):\n"
        "    widget.setFocusPolicy('NoFocus')\n")
    assert container_focus_violations(sneaky_getattr), "getattr 绕法没被拦下"


def test_passthrough_guard_catches_a_missing_release():
    """反证 S5：删掉关页路径上的 ``_release_focus()`` 必须红。"""
    source = _read(PASSTHROUGH_PATH)
    assert not passthrough_violations(source), "基线：真源码本该合规"

    show_blank = _method_snippet(source, "show_blank")
    stripped_method = show_blank.replace("        self._release_focus()\n", "", 1)
    assert stripped_method != show_blank
    # 把改过的 show_blank 塞回真源码，重建整个模块再看守卫
    original = _method_snippet(source, "_release_focus")
    mutated = source.replace(original, original + "\n" + stripped_method)
    assert "_release_focus" in mutated
    assert passthrough_violations(
        _replace_method(source, "show_blank", stripped_method)
    ), "关页路径漏调 _release_focus 却没被拦下"


def _replace_method(source: str, name: str, replacement: str) -> str:
    """用 ``replacement``（含 ``class Stub:`` 外壳）替换 ``source`` 里的同名方法。"""
    tree = _parse(source)
    target = _method(tree, name)
    lines = source.splitlines(keepends=True)
    body = "".join(lines[target.lineno - 1:target.end_lineno])
    replacement_body = replacement.split("class Stub:\n", 1)[1]
    raw = source[:source.index(body)] + replacement_body \
        + source[source.index(body) + len(body):]
    _parse(raw)          # 语法自检：替换后必须仍是合法源码
    return raw
