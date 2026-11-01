#
"""builtin_ui —— 把 Qt UI 装进 ``main_loop`` 槽位的插件。

职责单一：探测 PySide6 → 注册 select_loop hook → 在 hook 里把
``albuswall.ui.application.Application`` 作为候选投进槽位。UI 主类本身（窗口构建、
事件循环、信号处理）全部在 ``albuswall.ui.application`` 里；本插件不重复实现，也不
在 import 期实例化 QApplication。

用户视角的替换路径（优先级从高到低）：

1. ``Application.set_slot("main_loop", your_app)`` —— 显式指定，本插件失效
2. 其它插件 ``provide_slot(..., priority=1)`` —— 挤掉本插件
3. 禁用本插件（``[plugin_disables]`` 里 ``builtin_ui = false``）—— 回落 headless
4. **不换 UI，只换内容**：其它插件在 setup 阶段
   ``container.set("ui_root_factory", lambda app: QWidget)``

PySide6 未安装时静默失败（不提供候选），主循环回落到 headless。
"""

from __future__ import annotations

import logging

from albuswall.core.container import Container
from albuswall.core.plugins import plugin
from albuswall.core.registry import register

_logger = logging.getLogger(__name__)


@plugin("builtin_ui", version="1.0.0")
def builtin_ui(container: Container) -> None:
    """注册 ``albuswall.ui.application.Application`` 为 ``main_loop`` 候选。"""
    try:
        from albuswall.ui.application import Application  # noqa: F401
    except ImportError:
        # 带上 exc_info：模块改名 / 缺依赖时，这条 debug 是唯一线索。
        _logger.debug(
            "builtin_ui: PySide6 or albuswall.ui.application unavailable; "
            "plugin inactive (headless main loop will be used)",
            exc_info=True)
        return None

    def on_select_loop() -> None:
        # 延迟 import：application 模块级 import 会拉入整个 core；本 hook
        # 只在 boot 阶段跑一次，没必要让插件 import 期付这份开销。
        from albuswall.core.application import Application as _Facade
        from albuswall.ui import UIApplication

        try:
            _Facade.provide_slot(
                "main_loop", "builtin_ui",
                lambda c: UIApplication(c),
                priority=0,
                source="plugin:builtin_ui",
            )
        except Exception:  # noinspection PyBroadException
            # 宿主 set_slot 之后会抛 SlotFrozenError —— 预期行为，不崩。
            _logger.exception(
                "builtin_ui: failed to provide main_loop candidate")

    register("select_loop", on_select_loop)
    return None