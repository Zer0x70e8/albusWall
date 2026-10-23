#
""""""

from logging import getLogger
from typing import cast

import albuswall
from albuswall.core import Application, Container
from albuswall.configue import ConfigField
from albuswall.configue.declaration import ConfigDeclaration
from albuswall.configue.typed import load_typed
from albuswall.plugin import PluginManager
from albuswall.ui.schema import UIBootstrapConfig

from .common import DEFAULT_THEME, DEFAULT_UI
from .languages import DEFAULT_LANGUAGE
from .protocol import get_ui

logger = getLogger(f"{albuswall.__title__}.ui")


# noinspection bad-assignment
class UIConfig:
    theme_path: str = ConfigField("path", "theme", default=None)
    main = ConfigField("ui", default={})  # 老声明，等价于「[ui] 是一节」，未暴露给 typed dict
    # 界面语言：与主题同属「UI 引导配置」，放在 ``[ui]`` 节下。
    # 取值统一走 ``ui_config()["language"]``（见 albuswall.ui.builtin.i18n）。
    language: str = ConfigField("ui", "language", default=DEFAULT_LANGUAGE)
    theme: str = ConfigField("ui", "theme", default=DEFAULT_THEME)
    active: str = ConfigField("ui", "ui", default=DEFAULT_UI)
    no_builtin: bool = ConfigField("ui", "no_builtin", default=False)
    # 与 albuswall.__main__.Conf.debug 指向同一个节点（__default__ 节），
    # 这里只补类型、不注入默认值，避免重复声明把默认值再写一遍。
    debug: bool = ConfigField("debug", default=False, inject_default=False)


def ui_config() -> UIBootstrapConfig:
    """UI 引导配置的 typed dict 快照（默认值来自 ``UIConfig`` 的静态声明）。"""
    return cast(UIBootstrapConfig, load_typed(UIBootstrapConfig, ConfigDeclaration))


def registry_ui(container: "Container"):
    confs = ui_config()
    logger.debug("UI(active=%s, theme=%s) registry now.",
                 confs["active"], confs["theme"])

    # ① 触发 UI 导入阶段
    try:
        PluginManager.activate_ui(container, confs["active"].lower())
    except KeyError:
        logger.warning("UI loader(name=%s) not declared, "
                       "can't registry UI.", confs["active"])
        return None

    # ② 从 _REGISTRY 取类
    try:
        ui_cls = get_ui(confs["active"].lower())
    except RuntimeError:
        logger.warning(
            "Ui(name=%s) not registered; "
            "may be disabled by plugin 'enabled' filter, "
            "or its @ui_loader didn't import the implementation.",
            confs["active"],
        )
        return None

    if confs["debug"]:
        # noinspection PyStringConversionWithoutDunderMethod
        logger.debug(str(ui_cls))

    _ui_instance = None   # 闭包持有实例，避免 teardown 误造

    def create_ui():
        nonlocal _ui_instance
        _ui_instance = ui_cls()
        _ui_instance.setup(container)
        return _ui_instance

    container.reg("ui", create_ui, returns=ui_cls)

    # ③ teardown：只在实例真正被创建过时才调用
    def _teardown_ui() -> None:
        if _ui_instance is None:
            return
        # noinspection broad-exception,PyBroadException
        try:
            # 我也不知道为什么要有两种检查抑制方法
            # noinspection unresolved-references,PyUnresolvedReferences
            _ui_instance.teardown()
        except Exception:
            logger.exception("ui teardown failed")

    # 用 insert(0) 保证 teardown 早于其它 final 收尾动作
    Application.on_final(_teardown_ui)

    # container.on_boot(
    #     lambda: setattr(
    #         Application.instance(),
    #         "main_loop",
    #         container.get("ui").main_loop
    #     )
    # )

    return ui_cls
