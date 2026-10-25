#
""""""

from albuswall.log import getLogger
from albuswall.plugin import declare, ui_loader

PLUGIN_ID = "builtin_ui"

_logger = getLogger(f"{__name__.rsplit('.', 1)[0]}.factory")


# noinspection unused-imports,unused-parameter
@declare(PLUGIN_ID)
def setup(container):
    """插件阶段：配置之前，轻量声明。

    这个阶段的硬约束：**不能导入 Qt**。内置配置器（``configue``）就在本阶段
    之后加载，加载完不再接受新的子配置文件，所以所有「配置信息」必须在这里
    登记完；而 UI 实现（连带 Qt）要等 ``@ui_loader`` 才 import。
    """
    from .config import WindowPresenterConfs  # noqa: F401

    # 快捷键子系统（keymap）的配置信息：``[shortcuts]`` 段的取值类型走
    # configue 的加载钩子登记。这部分是纯 Python，不碰 Qt（见 keymap/config.py）。
    from .keymap import config as keymap_config

    keymap_config.install()
    _logger.debug("keymap config registered: section=%s", keymap_config.SECTION)


# noinspection unused-imports,unused-parameter
@ui_loader("builtin")
def load(container):
    """UI 阶段：配置就绪后，按需 import 真正的 UI 实现。

    `from .application import Application` 会触发 `@register_ui("builtin")`，
    把 UI 类登记到 `albuswall.ui.protocol._REGISTRY`。

    配置阶段已经结束，这里才开始用 Qt：

    - 先 import ``Application``（连带各窗口 / 动作模块，``@shortcut`` 都在
      这一层执行完）；
    - 再把 ``[shortcuts]`` 段的用户值灌进注册中心——此时字符串键才能被
      ``qt_binder`` 换成 ``QShortcut``。
    """
    # 延迟 import：本模块在插件激活阶段就会被 import，
    # 那时 ui.bootstrap 可能还没装完。
    from ..bootstrap import ui_config

    if ui_config()["no_builtin"]:
        return None

    from .application import Application

    # Qt 阶段登记内置动作（导入即执行 @shortcut），然后把 [shortcuts] 的用户值
    # 灌进注册中心（此时还没有窗口，attach 发生在 Window.__init__）。
    from . import shortcuts as _builtin_shortcuts  # noqa: F401
    from . import actions as _builtin_actions  # noqa: F401

    try:
        from .keymap import load_from_config
        from .keymap import config as keymap_config

        missing = load_from_config()
        if missing:
            _logger.info("keymap: %d [shortcuts] entries skipped: %s",
                         len(missing), missing)
        # 注册中心之后的每次改动都由它写回 [shortcuts]（单账）
        keymap_config.attach_persistence()
    except Exception:  # noqa: BLE001  按键配置坏了不该挡住整个 UI
        _logger.exception("keymap: load_from_config() failed, "
                          "falling back to code defaults")

    return Application
