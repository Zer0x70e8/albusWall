#
""""""

from albuswall.plugin import declare, ui_loader

PLUGIN_ID = "builtin_ui"


# noinspection unused-imports,unused-parameter
@declare(PLUGIN_ID)
def setup(container):
    """插件阶段：配置之前，轻量声明。"""
    from .config import WindowPresenterConfs  # noqa: F401


# noinspection unused-imports,unused-parameter
@ui_loader("builtin")
def load(container):
    """UI 阶段：配置就绪后，按需 import 真正的 UI 实现。

    `from .application import Application` 会触发 `@register_ui("builtin")`，
    把 UI 类登记到 `albuswall.ui.protocol._REGISTRY`。
    """
    # 延迟 import：本模块在插件激活阶段就会被 import，
    # 那时 ui.bootstrap 可能还没装完。
    from ..bootstrap import ui_config

    if ui_config()["no_builtin"]:
        return None

    from .application import Application
    return Application
