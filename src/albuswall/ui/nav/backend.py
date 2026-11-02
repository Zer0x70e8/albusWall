#
"""焦点执行后端：nav 把「落实焦点」外包出去的那一半协议。

为什么需要这一层
================

nav 的定位是**受控执行层**：决策（往哪跳）由 :mod:`albuswall.ui.nav.registry`
负责，执行（真的把焦点移过去）发生在 Qt 里。两者之间必须有一道**可验证**的
接缝，否则「逻辑光标」和「Qt 真实焦点」会悄悄失配：nav 以为落上了，方向键
却在下一个控件上继续走。所以执行不是一句 ``widget.setFocus()``，而是四件事：

=====================  ================================================
方法                   职责
=====================  ================================================
``ensure_focusable``   落点之前的准备（焦点策略 / 滚动到可见）
``apply_focus``        真正执行焦点
``current_focus_chain``读出执行后的焦点链（当前焦点 + 祖先），用于**验证**
``widget_to_node_id``  反向映射：某个控件（含祖先）属于哪个节点，用于回灌
=====================  ================================================

本模块**不 import Qt**：协议只用 ``typing.Protocol`` / ``dataclasses`` /
``enum`` 级别的依赖，实现方（``albuswall.ui.nav.qt.QtFocusBackend``）才碰 Qt。
于是：

* ``nav/model.py`` / ``registry.py`` / ``errors.py`` / 本模块的 Qt-free 约束
  依旧成立（有测试盯着）；
* 无 Qt 的单测可以直接塞一个假后端（``tests/ui/nav/test_backend.py``），
  验证「验证失败 → 回滚」这类语义，不需要 ``QApplication``。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from albuswall.ui.nav.model import Node

__all__ = ["FocusBackend", "NullFocusBackend"]


@runtime_checkable
class FocusBackend(Protocol):
    """焦点执行后端协议（结构化子类型，不需要继承）。

    四个方法都是**执行语义**，不是决策语义：决策层只调它们，不解释它们。
    ``node.widget`` 对本层仍是不透明对象。
    """

    def apply_focus(self, node: Node) -> None:
        """把焦点交给 ``node`` 的控件。失败应抛出，或让后续的
        :meth:`current_focus_chain` 看不到它。"""
        ...

    def current_focus_chain(self) -> tuple[Any, ...]:
        """当前焦点控件及其祖先链（近 → 远）。

        返回**空元组**表示「这台环境观测不到焦点」（无 WM / offscreen /
        无 Qt），调用方据此跳过验证——观测不到不等于失败。
        """
        ...

    def ensure_focusable(self, node: Node) -> None:
        """落点前的准备：让 ``node`` 的控件具备接收焦点的条件。

        实现方可以调整焦点策略、滚动到可见等；不该抛异常。
        """
        ...

    def widget_to_node_id(self, widget: Any) -> str | None:
        """控件 → 节点 id，沿祖先链向上探测（复合控件因此能对上叶节点）。

        未注册的控件返回 ``None``（例如背景层、浮层里的临时控件）。
        """
        ...


class NullFocusBackend:
    """不依赖 Qt 的默认后端：只做 ``widget.setFocus()``，不做验证。

    这是 :class:`~albuswall.ui.nav.registry.NavRegistry` 在没有注入真实后端时
    用的实现，**故意保持「永远乐观」**：

    * ``apply_focus`` 调控件的 ``setFocus()``（鸭子类型，非 Qt 替身也能用）；
    * ``current_focus_chain`` 回放**自己刚执行过的那个控件**——于是
      :meth:`~albuswall.ui.nav.registry.NavRegistry.focus_node` 的验证恒通过，
      行为与 v1 完全一致（无 Qt 的用例零改动）；
    * ``ensure_focusable`` 空操作；``widget_to_node_id`` 恒 ``None``（没有索引
      就没法回灌，回灌由真实后端负责）。

    真正的「执行是否落地」只有 :class:`~albuswall.ui.nav.qt.QtFocusBackend`
    才看得出来。
    """

    def __init__(self) -> None:
        self._last: Any = None

    def apply_focus(self, node: Node) -> None:
        """记录落点并调 ``widget.setFocus()``（控件没这个方法就只记录）。"""
        self._last = node.widget
        set_focus = getattr(node.widget, "setFocus", None)
        if callable(set_focus):
            set_focus()

    def current_focus_chain(self) -> tuple[Any, ...]:
        """回放刚执行过的控件：无 Qt 时这就是「焦点链」。"""
        return () if self._last is None else (self._last,)

    def ensure_focusable(self, node: Node) -> None:
        """空操作（没有焦点策略的概念）。"""
        return None

    def widget_to_node_id(self, widget: Any) -> str | None:
        """恒 ``None``：默认后端没有节点索引。"""
        return None

    def __repr__(self) -> str:  # pragma: no cover - 仅日志可读性
        return f"{type(self).__name__}()"
