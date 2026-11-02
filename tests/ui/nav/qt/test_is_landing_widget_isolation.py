#
"""``is_landing_widget`` 判定必须与 Qt 无关（只对注册表做纯 Python 判定）。

判定回答的问题是「这个控件是否同时是某个**叶节点**的落点控件」，答案完全由
注册表决定（``Node.widget`` 身份比较），**不需要也不允许**碰 Qt：不要
``isinstance(w, QWidget)``、不要 ``QApplication.instance()``、不要读
``focusPolicy()``。presenter 里只允许出现「从注册表派生
``Callable[[Any], bool]`` 并注入 ``QtFocusBackend``」这件事。

这里用 ``ast`` 审源码而不是运行时打桩：一旦有人顺手在判定里加一句 Qt 守卫，
这条用例立刻就红。
"""

from __future__ import annotations

import ast
from pathlib import Path

PRESENTER = (
    Path(__file__).resolve().parents[4]
    / "src" / "albuswall" / "ui" / "presenter" / "nav_presenter.py"
)

#: 判定函数里出现这些名字就说明它开始碰 Qt 了
QT_NAMES = frozenset({
    "Qt", "QApplication", "QWidget", "QObject", "QPushButton",
    "focusPolicy", "setFocusPolicy", "setFocus", "clearFocus",
    "focusWidget", "parentWidget", "isVisible", "isEnabled",
    "isinstance", "instance",
})


def _method(name: str) -> ast.FunctionDef:
    tree = ast.parse(PRESENTER.read_text(encoding="utf-8"),
                     filename=str(PRESENTER))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{PRESENTER.name} 里找不到方法 {name}")


def test_is_landing_widget_touches_no_qt_name():
    """``is_leaf_widget`` 的方法体里零 Qt 名字（含 isinstance / focusPolicy）。"""
    node = _method("is_leaf_widget")
    offenders: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and child.id in QT_NAMES:
            offenders.append(f"Name {child.id}")
        elif isinstance(child, ast.Attribute) and child.attr in QT_NAMES:
            offenders.append(f"Attribute .{child.attr}")
        elif isinstance(child, ast.Call) and isinstance(child.func, ast.Name) \
                and child.func.id == "getattr":
            for argument in child.args[1:2]:
                if isinstance(argument, ast.Constant) \
                        and str(argument.value) in QT_NAMES:
                    offenders.append(f"getattr({argument.value!r})")
    assert not offenders, (
        "is_leaf_widget 里出现了 Qt 调用，判定应保持纯注册表逻辑："
        f"{offenders}")


def test_the_judgement_is_derived_from_the_registry_only(nav_rig):
    """行为侧：判定只看 ``Node.widget`` 身份，与 Qt 焦点策略无关。"""
    rig = nav_rig

    # 内容层：容器与叶共用同一个控件 → True
    assert rig.presenter.is_leaf_widget(rig.window.content) is True
    # 只有容器的区域（标题栏）：容器 widget 不是任何叶的落点控件 → False
    assert rig.presenter.is_leaf_widget(rig.window.title_bar) is False
    # 叶的落点控件 → True
    assert rig.presenter.is_leaf_widget(rig.window.album.album_view) is True
    # 未注册控件 → False
    assert rig.presenter.is_leaf_widget(None) is False


def test_the_injected_callable_is_a_plain_predicate(nav_rig):
    """注入链路：presenter → ``QtFocusBackend.__init__`` → ``ensure_focusable``。"""
    rig = nav_rig
    backend = rig.nav.backend

    # 注入的就是 presenter 的方法（纯 Python 判定），backend 只持有引用
    assert backend._is_landing_widget_fn == rig.presenter.is_leaf_widget
    assert backend._is_landing_widget(rig.window.content) is True
    assert backend._is_landing_widget(rig.window.title_bar) is False
