#
"""Qt 侧的焦点执行后端（``nav`` 包里**唯一** import Qt 的模块）。

分工：决策（往哪跳）在 :mod:`albuswall.ui.nav.registry`，执行（真的跳过去、
跳成功了没有）在这里。nav 的核心模块只认
:class:`~albuswall.ui.nav.backend.FocusBackend` 协议，因此
``model.py`` / ``registry.py`` / ``errors.py`` / ``backend.py`` / ``wiring.py``
依旧不 import Qt；本模块**不被它们 import**，只由分发器
（:class:`albuswall.ui.presenter.nav_presenter.NavPresenter`）在界面起来之后
实例化并注入 ``NavRegistry.set_backend()``。

为什么单独一个文件而不是塞进 ``registry.py``：nav 的 Qt-free 约束是靠
「核心模块不 import Qt」守住的，适配器放在同包里但独立成文件，既能用同一个
包的私有约定（``Node.widget`` 的语义），又不会把 Qt 拖进决策层的 import 图。

四个方法的 Qt 实现要点
======================

* ``apply_focus``：``setFocus(Qt.FocusReason.OtherFocusReason)`` —— 显式给出
  焦点原因，避免 Qt 把这次聚焦当成 Tab 导航而触发它自己的焦点链（我们就是要
  让 Qt 的 Tab 链失效）；
* ``ensure_focusable``：两种相反的意图——**容器节点**（``child_domain``
  非空）永远设 ``NoFocus``（Qt 焦点链不该碰到区域容器，这是有意的反向特例）；
  **落点控件**只把 ``NoFocus`` 提升成 ``StrongFocus``，绝不反向设置
  ``NoFocus``（那会破坏 IME、spinbox、无障碍——见包文档「不要做的事」）；
* ``current_focus_chain``：``QApplication.focusWidget()`` 起沿 ``parentWidget``
  向上。**空元组 = 观测不到焦点**（无 WM / 平台不支持 / 窗口未激活），调用方
  据此跳过验证；
* ``widget_to_node_id``：委托给注入的解析器（分发器的 widget → node 索引），
  本模块不持有注册表，避免循环依赖。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

__all__ = ["QtFocusBackend"]

logger = logging.getLogger(__name__)


def _default_focus_getter() -> Any:
    """默认的焦点读取：``QApplication.focusWidget()``（没有应用返回 ``None``）。"""
    app = QApplication.instance()
    if not isinstance(app, QApplication):
        return None
    return app.focusWidget()


class QtFocusBackend:
    """:class:`~albuswall.ui.nav.backend.FocusBackend` 的 Qt 实现。

    Args:
        focus_getter: 读当前焦点控件的函数，默认
            ``QApplication.focusWidget()``。留成参数是为了让用例能把「焦点在
            哪」换成确定值（offscreen / 无合成器的环境里真实焦点常常是
            ``None``）。
        node_id_resolver: 控件 → 节点 id 的解析器（沿祖先链向上探测），默认
            ``None`` = 不做回灌映射。分发器注入自己的索引。
        is_landing_widget: 「这个控件是不是某个叶节点的落点控件」的判定，
            默认 ``None`` = 一律不是。只有「容器与叶共用同一个控件」时才会
            用到（见 :meth:`ensure_focusable`）；由分发器从注册表派生，本模块
            不 import 注册表。
    """

    def __init__(
            self,
            *,
            focus_getter: Callable[[], Any] | None = None,
            node_id_resolver: Callable[[Any], str | None] | None = None,
            is_landing_widget: Callable[[Any], bool] | None = None,
    ) -> None:
        self._focus_getter = focus_getter or _default_focus_getter
        self._node_id_resolver = node_id_resolver
        self._is_landing_widget_fn = is_landing_widget

    # -- 执行 ----------------------------------------------------------
    def apply_focus(self, node) -> None:
        """把键盘焦点交给 ``node.widget``（显式的 ``OtherFocusReason``）。"""
        widget = node.widget
        set_focus = getattr(widget, "setFocus", None)
        if not callable(set_focus):
            logger.debug(
                "apply_focus %s: widget has no setFocus(); skipping", node.id)
            return
        try:
            set_focus(Qt.FocusReason.OtherFocusReason)
        except TypeError:  # 非 QWidget 的鸭子类型替身：只接受无参调用
            set_focus()

    def ensure_focusable(self, node) -> None:
        """容器节点永远 ``NoFocus``；落点控件 ``NoFocus`` → ``StrongFocus``。

        这是两种**不同**的意图，不要合并：

        * **容器节点**（``child_domain`` 非空：标题栏、内容层、overlay 的三
          个页、HUD 容器、信息面板）是反向特例——**永远是** ``NoFocus``。
          设 ``NoFocus`` 在这里是有意行为：Qt 的焦点链（Tab 遍历、
          ``focusNextPrevChild``、``setTabOrder``）不该碰到区域容器；跨区域
          的焦点归属由 nav 决定，nav 把焦点交给容器内部的**落点控件**
          （``node.focus_fn`` / 子域 entry）。
        * **落点控件**（``child_domain`` 为空）只升不降：``NoFocus`` →
          ``StrongFocus``，绝不反向设置 ``NoFocus``（那会破坏 IME、spinbox、
          无障碍——见包文档「不要做的事」）。

        **例外：当容器本身是落点时，容器 NoFocus 规则让位。**
        当某个控件同时是**容器节点的 widget** 与**叶节点的落点 widget**（当前
        唯一案例：``window.content`` 网关与 ``content.body`` 叶节点都指向
        ``Content``），把 ``NoFocus`` 强加给它等于让叶域没有可落点 —— 内容层
        的键盘光标会无处可去。这是「区域入口与叶落点重合」的**已定义边界**，
        不是实现漏洞：判定由注入的 ``is_landing_widget`` 完成（presenter 从
        注册表派生，纯 Python），本模块不持有注册表。

        ``TODO(v2)``：合并 ``window.content`` 网关与 ``content.body`` 叶节点
        （让区域入口与叶落点不再共用同一个控件），届时这条例外自然消失，
        ``is_landing_widget`` 注入也可以删掉。
        """
        widget = node.widget
        set_policy = getattr(widget, "setFocusPolicy", None)

        if getattr(node, "child_domain", None) is not None:
            if not callable(set_policy) or self._is_landing_widget(widget):
                return
            try:
                set_policy(Qt.FocusPolicy.NoFocus)
            except RuntimeError:  # 底层 C++ 对象已销毁
                return
            logger.debug("ensure_focusable %s: container → NoFocus", node.id)
            return

        get_policy = getattr(widget, "focusPolicy", None)
        if not callable(get_policy):
            return
        try:
            policy = get_policy()
        except RuntimeError:  # 底层 C++ 对象已销毁
            return
        if policy != Qt.FocusPolicy.NoFocus:
            return
        if not callable(set_policy):
            return
        logger.debug(
            "ensure_focusable %s: NoFocus → StrongFocus", node.id)
        set_policy(Qt.FocusPolicy.StrongFocus)

    def _is_landing_widget(self, widget: Any) -> bool:
        """``widget`` 是否同时是某个叶节点（非容器）的落点控件。

        默认没有注入判定函数 → 一律 ``False``（容器规则照常生效）。
        """
        check = self._is_landing_widget_fn
        if check is None:
            return False
        try:
            return bool(check(widget))
        except RuntimeError:  # 控件已销毁
            return False

    # -- 观测 ----------------------------------------------------------
    def current_focus_chain(self) -> tuple[Any, ...]:
        """当前焦点控件及其祖先链（近 → 远）；观测不到焦点时返回空元组。"""
        try:
            widget = self._focus_getter()
        except RuntimeError:  # 分发器在窗口销毁后仍被调用
            return ()
        chain: list[Any] = []
        while widget is not None:
            chain.append(widget)
            parent = getattr(widget, "parentWidget", None)
            if not callable(parent):
                break
            try:
                widget = parent()
            except RuntimeError:
                break
        return tuple(chain)

    def widget_to_node_id(self, widget: Any) -> str | None:
        """控件 → 节点 id（沿祖先链）；没有解析器或未注册 → ``None``。"""
        if self._node_id_resolver is None or widget is None:
            return None
        try:
            return self._node_id_resolver(widget)
        except RuntimeError:  # 控件已销毁
            return None

    def __repr__(self) -> str:  # pragma: no cover - 仅日志可读性
        return f"{type(self).__name__}()"
