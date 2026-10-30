#! /usr/bin/python3
# -*- coding: utf-8 -*-
""" Project AlbusWall entrance."""

import gc
import logging

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
from albuswall.repositories import registry_repository
from albuswall.service import register_service

CONFIG_FILE_PATH = get_user_config_dir(
    albuswall.__title__, albuswall.__author__)
DATA_FILE_PATH = get_user_data_dir(
    albuswall.__title__, albuswall.__author__)
CONFIG_FILE_NAME = "config.ini"

_logger = getLogger(__package__)


def _boot(app: Application):
    container: Container = app.container()
    container.register("app", app)
    app.on_init(lambda: _logger.debug("Program bootstrap completed."))

    # phase cold
    # discover_all(
    #     builtin_package="albuswall.plugins",
    #     user_dir=DATA_FILE_PATH / "plugins",
    # )
    # conf_file = CONFIG_FILE_PATH / CONFIG_FILE_NAME
    # if conf_file.is_file():
    #     plugin_disables = parse_section_file(
    #         conf_file,
    #         "plugin_disables",
    #         required=False,
    #     )
    # else:
    #     _logger.info(
    #         "Not found _config file: %s",
    #         conf_file
    #     )
    #     plugin_disables = {}
    #
    # app.plugins.activate(
    #     container,
    #     disabled=plugin_disables
    # )

    # phase1 static _config
    # 文件不存在时用默认值启动（ignore_missing=True）；用户改配置后 save()
    # 会自动创建文件。
    setup_config(
        container,
        base_dir=CONFIG_FILE_PATH,
        file_name=CONFIG_FILE_NAME,
        ignore_missing=True,
    )

    # phase2 logger
    setup_log(container)
    # app.log_enable = True

    # phase3 gc
    gc.collect()
    gc.freeze()

    # phase4 state and cache
    # 旧的动态子树（@dynamic_state / StatefulNamespace / load_typed）已停用：
    # 全仓库无人使用，新包用 ConfigFile.JSON + @configclass 表达状态文件。

    # phase5 infrastructure
    registry_command_registry(container)
    registry_infrastructure(container)
    registry_repository(container)

    # phase6 service
    register_service(container)

    # phase7 UI
    # registry_ui(container)

    # phase others
    # dbg msg
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
