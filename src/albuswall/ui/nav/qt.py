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
* ``ensure_focusable``：**只**把 ``NoFocus`` 提升成 ``StrongFocus``。绝不反向
  设置 ``NoFocus``（那会破坏 IME、spinbox、无障碍——见包文档「不要做的事」）；
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
    """

    def __init__(
            self,
            *,
            focus_getter: Callable[[], Any] | None = None,
            node_id_resolver: Callable[[Any], str | None] | None = None,
    ) -> None:
        self._focus_getter = focus_getter or _default_focus_getter
        self._node_id_resolver = node_id_resolver

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
        """``NoFocus`` 的控件提升为 ``StrongFocus``（只升不降）。

        多数注册节点本来就是按钮 / 视图（焦点策略够用），所以这通常是空操作；
        真正需要它的是「自定义控件忘了设焦点策略」的情形——那时方向键能算到
        它、焦点却落不上去，表现成「按了没反应」。
        """
        widget = node.widget
        get_policy = getattr(widget, "focusPolicy", None)
        if not callable(get_policy):
            return
        try:
            policy = get_policy()
        except RuntimeError:  # 底层 C++ 对象已销毁
            return
        if policy != Qt.FocusPolicy.NoFocus:
            return
        set_policy = getattr(widget, "setFocusPolicy", None)
        if not callable(set_policy):
            return
        logger.debug(
            "ensure_focusable %s: NoFocus → StrongFocus", node.id)
        set_policy(Qt.FocusPolicy.StrongFocus)

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
