#
"""``albuswall.ui`` —— PySide6 界面层（主循环、主窗口、文案基础设施）。

界面语言两个入口（等价）：

* :func:`set_language` —— 直接喂字面量，``set_language("en_US")``；
* :class:`~albuswall.ui.i18n.TranslationManager` —— 应用层持有 / 注入，
  容器里的 ``i18n`` 就是它。

界面层分四块，各自只跟相邻的一块说话：

``albuswall.ui.model``      Qt 展示模型（role 契约 + 数据 → item data）
``albuswall.ui.delegate``   绘制委托（item data → 像素）
``albuswall.ui.widgets`` / ``albuswall.ui.window``  视图与页面（只发意图信号）
``albuswall.ui.presenter``  表现层（读配置 / 调服务 / 把结果写回视图）

页面与视图都提供 ``set_model`` / ``bind_model`` 这类**直接接口**：
外部把模型递进去就能用，缺省委托会自动装好。

presenter 由 :class:`~albuswall.ui.presenter.main.MainPresenter` 统一编排：
界面 boot 层（:func:`albuswall.ui.bootstrap.setup_ui`，由 ``builtin_ui`` 插件在
``init`` hook 里调用）建好窗口后调用 ``MainPresenter.bootstrap(...)``，
它把主 presenter 注册进容器（``presenter`` / ``main_presenter``），并负责
子 presenter 的构造与生命周期；窗口主题也在那里应用。

UI 主类 :class:`~albuswall.ui.application.Application` 是**被动**的：只是一个
``QApplication`` + ``MainLoop``（事件循环 / 退出码 / 跨线程投递），不建窗口、
不注册 hook；装配全在 :mod:`albuswall.ui.bootstrap`。
"""

from __future__ import annotations

from .application import Application, UIApplication
from .i18n import (
    TranslationManager,
    available_languages,
    current_language,
    set_language,
)
from .presenter import MainPresenter, Presenter, register_presenter

__all__ = [
    "Application",
    "MainPresenter",
    "Presenter",
    "TranslationManager",
    "UIApplication",
    "available_languages",
    "current_language",
    "register_presenter",
    "set_language",
]
