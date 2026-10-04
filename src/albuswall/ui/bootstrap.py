#
""""""

from logging import getLogger

import albuswall
from albuswall.core import Application, Container
from albuswall.configue import ConfigField
from albuswall.plugin import PluginManager

from .common import DEFAULT_THEME, DEFAULT_UI
from .protocol import get_ui

logger = getLogger(f"{albuswall.__title__}.ui")


# noinspection bad-assignment
class UIConfig:
    theme_path: str = ConfigField("path", "theme", default=None)
    main = ConfigField("ui", default={})
    theme: str = ConfigField("ui", "theme", default=DEFAULT_THEME)
    active: str = ConfigField("ui", "ui", default=DEFAULT_UI)
    no_builtin: bool = ConfigField("ui", "no_builtin", default=False)


def registry_ui(container: "Container"):
    confs = UIConfig()
    logger.debug("UI(active=%s, theme=%s) registry now.",
                 confs.active, confs.theme)

    # ① 触发 UI 导入阶段
    try:
        PluginManager.activate_ui(container, confs.active.lower())
    except KeyError:
        logger.warning("UI loader(name=%s) not declared, "
                       "can't registry UI.", confs.active)
        return None

    # ② 从 _REGISTRY 取类
    try:
        ui_cls = get_ui(confs.active.lower())
    except RuntimeError:
        logger.warning(
            "Ui(name=%s) not registered; "
            "may be disabled by plugin 'enabled' filter, "
            "or its @ui_loader didn't import the implementation.",
            confs.active,
        )
        return None

    if container.get("config").static.debug:
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
