#
"""builtin_ui —— 把 Qt 界面装进 ``main_loop`` 槽位的插件。

和 ``main.py::_boot`` 的分工
===========================

``main._boot`` 编排**宿主** boot（config → plugins → logger → main loop → gc →
基础设施 → 服务层）；本插件编排**界面** boot，两段 boot 一一对位：

=========================  ==================================================
``main._boot``             本插件
=========================  ==================================================
phase 2 plugins            ``setup``：探测 PySide6，挂三个 hook
                           （直挂 ``hooks_map``，见
                           ``docs/plugin-author-guide.md`` 的回滚边界）
phase 4 main loop          ``select_loop``：投 ``main_loop`` 候选；候选工厂只
                           造**被动**的 Qt 主循环，并登记 ``ui_app``
（宿主 ``run_hooks``）      ``init``：界面装配 —— 语言 → 主窗口 →
                           快捷键注册表 → presenter
                           （:func:`albuswall.ui.bootstrap.setup_ui`）
（宿主 ``run_hooks``）      ``loop``：显示主窗口
                           （:func:`~albuswall.ui.bootstrap.show_ui`）
=========================  ==================================================

三段各就各位的理由
==================

* **工厂只造主循环**：``main._boot`` 的 phase 4 必须早于 phase 7（服务层据此
  建 ``main_loop_service``），所以主循环要先就位；但此刻基础设施 / 服务还没
  注册，presenter 树还不能 start。
* **装配放在 init hook**：``exec`` 的顺序是 ``boot → init → start → loop``，
  init 时 phase 6/7 已完成，presenter 拿得到自己依赖的服务；窗口又在
  ``init`` 的**头部**插队，其它插件的 ``on_init`` 直接
  ``container.get("main_window")`` / ``container.get("presenter")`` 即可，
  不必关心 hook 名单顺序。
* **显示放在 loop hook**：``init`` 里插件还在往界面上加东西，早显示只会闪；
  ``show_ui`` 在那一刻才从容器取 ``main_window``，所以「boot 期间换窗口」
  也显示得到对的对象。

UI 主类（:class:`albuswall.ui.application.Application`）是**被动**的：只有
事件循环、退出码、跨线程投递和进程信号。它既不建窗口，也不注册 hook——

* 要主循环 → 槽位里拿 ``ui_app``；
* 要窗口 → ``container.get("main_window")``；
* 要 presenter → ``container.get("presenter")``。

用户视角的替换路径（优先级从高到低）
====================================

1. ``Application.set_slot("main_loop", your_app)`` —— 显式指定，本插件失效；
   此时也没有界面装配，需要的话自己调 ``bootstrap.setup_ui`` / ``show_ui``；
2. 其它插件 ``provide_slot("main_loop", ..., priority=1)`` —— 挤掉本插件；
3. 禁用本插件（``[plugin_disables]`` 里 ``builtin_ui = false``）—— 回落 headless；
4. **不换主循环，只换内容**：其它插件在 ``on_init`` 里对
   ``container.get("main_window")`` / ``container.get("presenter")`` 增删界面；
5. **只换主窗口**：派生 :class:`albuswall.ui.window.Window` 后，在 UI boot
   之前把实例塞进容器（``container.set("main_window", MyWindow())``，或
   ``container.register("main_window", MyWindow)`` 走工厂）。``setup_ui`` 只用
   容器里那一个，不改 ``ui/window/`` 的任何代码。

PySide6 未安装时静默失败（不提供候选），主循环回落到 headless。
"""

from __future__ import annotations

import logging
from typing import Any

from albuswall.core.container import Container
from albuswall.core.plugins import plugin
from albuswall.core.registry import hooks_map

_logger = logging.getLogger(__name__)


@plugin("builtin_ui", version="1.0.0")
def builtin_ui(container: Container) -> None:
    """探测 PySide6 → 挂三个 hook；界面装配在 boot 阶段按序完成。"""
    try:
        # 延迟 import：重依赖（PySide6 / 整个 ui 层）只在 setup 阶段付；
        # 缺 PySide6 时这里 ImportError，插件安静失效、主循环回落 headless。
        from albuswall.ui.application import Application
        from albuswall.ui.bootstrap import (
            UI_APP_KEY,
            setup_ui,
            show_ui,
        )
    except ImportError:
        _logger.debug(
            "builtin_ui: PySide6 or albuswall.ui unavailable; "
            "plugin inactive (headless main loop will be used)",
            exc_info=True)
        return None

    def _create_loop(c: Container) -> Any:
        """槽位工厂：只造被动主循环，并让它在容器里尽早可见。

        工厂只被槽位 resolve 调用一次（``main._boot`` 的 phase 4），返回值就是
        ``main_loop`` 槽位的对象——它必须是 ``loop.run()`` 的那个本体。
        界面装配**不**在这里做：此时服务层还没注册（见模块 docstring）。
        """
        app = Application()
        c.set(UI_APP_KEY, app)
        return app

    def on_select_loop() -> None:
        # 延迟 import：application 模块级 import 会拉入 core；本 hook 只在
        # boot 阶段跑一次，没必要让插件 import 期付这份开销。
        from albuswall.core.application import Application as _Facade

        try:
            _Facade.provide_slot(
                "main_loop", "builtin_ui",
                _create_loop,
                priority=0,
                source="plugin:builtin_ui",
            )
        except Exception:  # noinspection PyBroadException
            # 宿主 set_slot 之后会抛 SlotFrozenError —— 预期行为，不崩。
            _logger.exception(
                "builtin_ui: failed to provide main_loop candidate")

    def on_init() -> None:
        # 只在「主循环确实是我们造的」时装配界面：主循环被 set_slot 换成别的
        # 实现时容器里没有 ui_app，界面无从谈起（headless 也走这条分支）。
        app = container.get(UI_APP_KEY)
        if app is None:
            _logger.debug("builtin_ui: no ui_app in container, skip UI boot")
            return
        setup_ui(container, app)

    def on_show(_app: object) -> None:
        # loop hook 的签名是 ``hook(app)``（直挂不做零参包装）。
        show_ui(container)

    hooks_map["select_loop"].append(on_select_loop)
    # init 插队：其它插件的 on_init 跑之前窗口 / presenter 就已就绪。
    hooks_map["init"].insert(0, on_init)
    # loop 插队：其它插件的 loop hook 跑之前窗口就已显示。
    hooks_map["loop"].insert(0, on_show)
    return None
