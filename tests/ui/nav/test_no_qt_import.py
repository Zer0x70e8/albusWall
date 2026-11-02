#
"""Qt-free 守卫：``ui/nav`` 的核心模块不许 import Qt。

架构约束（nav 包文档「不要做的事」）：

* 决策层（``model`` / ``registry`` / ``backend`` / ``wiring`` / ``errors``）只认
  ``Node.widget`` 这个不透明对象，**不 import PySide6**；
* ``qt.py`` 是包里唯一 import Qt 的模块，而且**不被**上面那几个模块 import；
* Qt → nav 的反向依赖也不允许（``qt.py`` 只依赖 PySide6 + 标准库 + 本包的
  ``__init__`` 文档里说明的协议）。

这条用例把约束钉在源码层面上：任何一次「顺手在 registry 里 setFocusPolicy」
都会红。用 ``ast`` 解析而不是 grep 字符串：注释 / docstring 里提到 PySide6
是合法的（包文档就在讲这件事），真正的 import 才是违规。
"""

from __future__ import annotations

import ast
from pathlib import Path

NAV_PACKAGE = Path(__file__).resolve().parents[3] / "src" / "albuswall" / "ui" / "nav"

#: 必须 Qt-free 的核心模块
QT_FREE_MODULES = ("model.py", "registry.py", "backend.py", "wiring.py",
                   "errors.py")

#: 唯一允许 import Qt 的模块
QT_MODULE = "qt.py"


def _imported_names(path: Path) -> set[str]:
    """``path`` 里所有 import 的顶层模块名（``from a.b import c`` → ``a``）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:       # 相对 import：包内模块
                continue
            if node.module:
                names.add(node.module.split(".")[0])
    return names


def test_core_nav_modules_do_not_import_qt():
    violations: list[str] = []
    for module in QT_FREE_MODULES:
        path = NAV_PACKAGE / module
        assert path.is_file(), f"缺少核心模块：{path}"
        imported = _imported_names(path)
        if "PySide6" in imported or "PyQt5" in imported or "PyQt6" in imported:
            violations.append(f"{module}: {sorted(imported)}")
    assert not violations, (
        "nav 核心模块必须保持 Qt-free（Qt 只在 qt.py 里）：" f"{violations}")


def test_core_nav_modules_do_not_import_the_qt_adapter():
    """``qt.py`` 不被核心模块 import（依赖方向只能是 presenter → qt）。"""
    violations: list[str] = []
    for module in QT_FREE_MODULES:
        path = NAV_PACKAGE / module
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                names = {alias.name for alias in node.names}
                if names & {"qt"} or (node.module or "").endswith((".qt", "nav.qt")):
                    violations.append(f"{module}: from {node.module} import {names}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.endswith(".nav.qt"):
                        violations.append(f"{module}: import {alias.name}")
    assert not violations, f"核心模块不许 import qt.py：{violations}"


def test_only_qt_py_touches_pyside6():
    """包里除 ``qt.py`` 外的模块也不该 import Qt（防止第二处 Qt 入口）。"""
    violations: list[str] = []
    for path in sorted(NAV_PACKAGE.glob("*.py")):
        if path.name == QT_MODULE:
            continue
        imported = _imported_names(path)
        if "PySide6" in imported:
            violations.append(path.name)
    assert not violations, (
        f"这些模块也 import 了 Qt（Qt 入口应只有 {QT_MODULE}）：{violations}")


def test_qt_adapter_does_not_import_the_registry():
    """``qt.py`` 不 import 注册表 / wiring（不持有注册表，避免循环依赖）。"""
    imported = _imported_names(NAV_PACKAGE / QT_MODULE)
    assert "PySide6" in imported
    source = (NAV_PACKAGE / QT_MODULE).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level:
            continue
        if isinstance(node, ast.ImportFrom) and node.module:
            assert not node.module.startswith("albuswall"), (
                f"qt.py 不该 import 应用模块：{node.module}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("albuswall"), (
                    f"qt.py 不该 import 应用模块：{alias.name}")
