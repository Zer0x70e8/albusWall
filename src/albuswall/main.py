#! /usr/bin/python3
# -*- coding: utf-8 -*-
""" Project AlbusWall entrance."""

import gc
import logging
from typing import TYPE_CHECKING

import albuswall
from albuswall.command import registry_command_registry
from albuswall.configue.bootstrap import setup_config
from albuswall.configue.utils.platform_dir import (
    get_user_config_dir,
    get_user_data_dir,
)
from albuswall.core import Application, Container, ExitCode, Runtime
from albuswall.infrastructure import registry_infrastructure
from albuswall.log import getLogger
from albuswall.log.bootstrap import setup_log
from albuswall.log.handlers import MemoryCacheHandler
from albuswall.plugin.discovery import discover_all
from albuswall.repositories import registry_repository
from albuswall.service import register_service

if TYPE_CHECKING:
    from albuswall.configue import Configue

CONFIG_FILE_PATH = get_user_config_dir(
    albuswall.__name__, albuswall.__author__)
DATA_FILE_PATH = get_user_data_dir(
    albuswall.__name__, albuswall.__author__)
CONFIG_FILE_NAME = "config.ini"
BUILTIN_PLUGINS_MODEL = "albuswall.plugins"

_logger = getLogger(__package__)


def plugin_disables(config: "Configue | None") -> set[str]:
    """从配置读 ``[plugin_disables]`` 段，返回被禁用的插件名。

    段的 key 就是插件名；value 只是「开关记法」，本阶段不参与判断——这与旧
    实现的 ``parse_section_file(..., "plugin_disables")``（取 dict 的 keys）
    保持同一语义。

    **这里是树视图的合法用途**（不是漏网）：需要的是「文件里实际有什么」而不是
    「某个已声明字段的值」。段名固定但 key 是运行期才知道的插件名（可能来自
    第三方分发的插件），声明期写不出 ``@configclass`` 字段；``_SectionView``
    可迭代、``in`` 判段存在，正好对上「枚举 + 探测」的需求。

    ``Configue`` 没有 ``dict.get``（它是声明式树视图），所以先判「段存在」，
    再对段视图取 keys。
    """
    if config is None:
        return set()
    tree = config.tree.main
    if "plugin_disables" not in tree:
        return set()
    return set(tree["plugin_disables"])


def _boot(app: Application):
    container: Container = app.container()
    app.on_init(lambda: _logger.debug("Program bootstrap completed."))

    # phase 1: config
    setup_config(
        container,
        base_dir=CONFIG_FILE_PATH,
        file_name=CONFIG_FILE_NAME,
        ignore_missing=True,
        seed_defaults=True,
    )
    app.resolve_slot("config")

    # phase 2: plugins
    # 先 resolve 插件器（宿主可在 select_plugins 里替换），再发现插件。
    # resolve 必须在 app.plugins() 之前——它是 @property，未 resolve 会抛
    # SlotNotResolvedError。
    app.resolve_slot("plugins")
    discover_all(
        builtin_package=BUILTIN_PLUGINS_MODEL,
        user_dir=None,
    )
    manager = app.plugins()
    manager.load_pending()
    manager.setup_all(disabled=plugin_disables(container.get("config")))

    # phase 3: logger
    setup_log(container)

    # phase 4: main loop
    # 插件在 setup 阶段注册的 select_loop hook 在这里触发（例如 builtin_ui）。
    # 槽位工厂只造**被动的** Qt 主循环并把 ``ui_app`` 登记进容器；界面装配
    # （语言 / 主窗口 / presenter）由 builtin_ui 在 init hook 里做——那时
    # phase 6/7 已经把基础设施与服务装好，presenter 才拿得到自己依赖的服务。
    # 本 phase 又必须早于 phase 7：服务层据此建 ``main_loop_service``。
    loop = app.resolve_slot("main_loop")
    # 放进容器：服务层据此建 ``main_loop_service``（写入口回主线程的通道）。
    # 必须与 ``loop.run()`` 是**同一个对象**，否则调度会投递到没人消费的队列。
    container.set("main_loop", loop)
    _logger.debug("Main loop: %r", type(loop).__name__)

    # phase 5: gc
    gc.collect()
    gc.freeze()

    # phase 6: infrastructure
    registry_command_registry(container)
    registry_infrastructure(container)
    registry_repository(container)

    # phase 7: service
    register_service(container)

    _logger.debug("Program boot registry finished.")
    app.on_exit(lambda: _logger.debug("Program stop."))


def _main(app: Application) -> ExitCode:
    _boot(app)
    return app.exec()


def main() -> ExitCode:
    logging.getLogger().setLevel(1)
    memory_handler = MemoryCacheHandler()
    _logger.addHandler(memory_handler)
    _logger.debug("Program start.")
    return Runtime(memory_handler).run_app(_main)


if __name__ == "__main__":
    raise SystemExit(main())
