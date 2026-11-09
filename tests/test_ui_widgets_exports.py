#
"""``albuswall.ui.widgets`` 导出面的一致性契约。

每条 ``__init__`` 都是一张「公开名 -> 定义模块」的死表（PEP 562 惰性解析），
最容易出的错是表漂移：名字拼错、加了类忘记登记、``__all__`` 与 ``_LAZY``
不一致、子包与顶层指向不同模块。这里把契约钉死：

* ``__all__`` 有序、无重复；
* ``__all__`` 里每个名字都能真的解析出来，且 ``dir()`` 里也有；
* ``__all__`` == ``_LAZY`` 的键 ∪ 子包名（不多不少）；
* 表单四轴在顶层与 ``form_widget`` 里是**同一批对象**；
* 按需解析真的按需：解析某个名字只额外拉起它自己那个模块
  （``albuswall.ui`` 父包本身是 eager 的，所以看的是**增量**）。
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

PACKAGES = (
    "albuswall.ui.widgets",
    "albuswall.ui.widgets.layout",
    "albuswall.ui.widgets.view",
    "albuswall.ui.widgets.widget",
    "albuswall.ui.widgets.widget.form_widget",
)


@pytest.fixture(scope="module", params=PACKAGES, ids=PACKAGES)
def package(request):
    return importlib.import_module(request.param)


def test_all_is_sorted_and_unique(package):
    names = list(package.__all__)
    assert names == sorted(names), f"{package.__name__}.__all__ 未排序"
    assert len(names) == len(set(names)), f"{package.__name__}.__all__ 有重复"


def test_all_names_resolve(package):
    missing = []
    for name in package.__all__:
        try:
            getattr(package, name)
        except AttributeError:
            missing.append(name)
    assert not missing, f"{package.__name__} 解析失败：{missing}"


def test_all_matches_lazy_map(package):
    lazy = set(getattr(package, "_LAZY", {}))
    subpackages = set(getattr(package, "_SUBPACKAGES", ()))
    assert set(package.__all__) == lazy | subpackages


def test_lazy_map_targets_exist(package):
    for name, target in getattr(package, "_LAZY", {}).items():
        module = importlib.import_module(f"{package.__name__}.{target}")
        assert hasattr(module, name), f"{name} 不在 {module.__name__} 里"


def test_dir_lists_public_names(package):
    listed = set(dir(package))
    missing = set(package.__all__) - listed
    assert not missing, f"dir() 缺少：{sorted(missing)}"


def test_form_axes_reachable_from_root():
    from albuswall.ui.widgets import (
        BrowseKind,
        FormView,
        ResetPosition,
        RowAccess,
        RowControl,
        RowHandle,
        RowSpec,
        ValidationError,
        ValidationMode,
    )
    from albuswall.ui.widgets.widget import form_widget

    assert FormView is form_widget.FormView
    assert RowHandle is form_widget.RowHandle
    assert ValidationError is form_widget.ValidationError
    assert RowControl is form_widget.RowControl
    assert RowAccess is form_widget.RowAccess
    assert BrowseKind is form_widget.BrowseKind
    assert ResetPosition is form_widget.ResetPosition
    assert ValidationMode is form_widget.ValidationMode
    assert RowSpec is form_widget.RowSpec
    assert issubclass(ValidationError, ValueError)


def test_form_controls_cover_every_row_control():
    from albuswall.ui.widgets import FormView, RowControl

    controllers = set(FormView.supported_controls())
    builtins = {member.name.lower() for member in RowControl}
    assert builtins <= controllers


def _widgets_modules(extra: str) -> set[str]:
    """在干净子进程里 import 顶层包（+ 可选 extra），返回已加载的 widgets 模块。"""
    import albuswall

    src_root = str(Path(albuswall.__file__).resolve().parents[1])
    code = (
        "import sys\n"
        "import albuswall.ui.widgets as w\n"
        f"{extra}\n"
        "prefix = 'albuswall.ui.widgets'\n"
        "print('\\n'.join(sorted(m for m in sys.modules "
        "if m.startswith(prefix))))\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [src_root] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
    )
    env.setdefault("QT_QPA_PLATFORM", "offscreen")

    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return {line for line in result.stdout.splitlines() if line.strip()}


def test_resolving_a_name_only_loads_its_module():
    """解析 ``Grid12Layout`` 的增量必须只有 ``layout.grid12_layout``。"""
    base = _widgets_modules("")
    pulled = _widgets_modules("w.Grid12Layout")
    delta = pulled - base
    assert delta == {"albuswall.ui.widgets.layout.grid12_layout"}, delta


def test_resolving_form_view_pulls_only_form_side():
    """解析 ``FormView`` 不应顺带拉起任何 ``view.*`` 页面模块。"""
    base = _widgets_modules("")
    pulled = _widgets_modules("w.FormView")
    delta = pulled - base
    assert "albuswall.ui.widgets.widget.form_widget.form_view" in delta
    assert not any(m.startswith("albuswall.ui.widgets.view") for m in delta)
    assert not any(m.startswith("albuswall.ui.widgets.widget.action_buttons")
                   for m in delta)
