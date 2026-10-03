#
""""""

import gc
import logging

import albuswall
from albuswall.log import getLogger
from albuswall.log.handlers import MemoryCacheHandler
from albuswall.core import Application, Runtime
from albuswall.plugin import discover_all
from albuswall.configue import (
    setup_static_config, setup_dynamic_config,
    ConfigField, parse_section_file,
    get_user_config_dir, get_user_data_dir
)
from albuswall.log.bootstrap import setup_log
from albuswall.infrastructure import (
    register_database,
    register_task_service,
    register_trigger_service
)
from albuswall.repositories import registry_repository
from albuswall.services import register_service
from albuswall.ui import registry_ui

CONFIG_FILE_PATH = get_user_config_dir(
    albuswall.__title__, albuswall.__author__)
DATA_FILE_PATH = get_user_data_dir(
    albuswall.__title__, albuswall.__author__)
CONFIG_FILE_NAME = "config.ini"

logging.getLogger().setLevel(1)
_logger = getLogger(albuswall.__name__)
memory_handler = MemoryCacheHandler()
runtime = Runtime(memory_handler)


# noinspection bad-assignment
class Conf:
    debug: bool = ConfigField("debug", default=False)


_config = Conf()


def _boot(app: Application) -> None:
    container = app.container

    # phase cold
    discover_all(
        builtin_package="albuswall.plugins",
        user_dir=DATA_FILE_PATH / "plugins",
    )
    conf_file = CONFIG_FILE_PATH / CONFIG_FILE_NAME
    if conf_file.is_file():
        plugin_disables = parse_section_file(
            conf_file,
            "plugin_disables",
            required=False,
        )
    else:
        _logger.info(
            "Not found _config file: %s",
            conf_file
        )
        plugin_disables = {}

    app.plugins.activate(
        container,
        disabled=plugin_disables
    )

    # phase1 static _config
    setup_static_config(
        container,
        CONFIG_FILE_PATH / CONFIG_FILE_NAME
    )

    # phase2 log
    setup_log(container)
    app.log_enable = True

    # phase3 gc
    gc.collect()
    gc.freeze()

    # phase4 state and cache
    setup_dynamic_config(container)

    # phase5 infrastructure
    register_database(container)
    register_task_service(container)
    register_trigger_service(container)
    registry_repository(container)

    # phase6 service
    register_service(container)

    # phase7 UI
    registry_ui(container)

    # phase others
    # dbg msg
    container.on_boot_insert(
        0,
        lambda: app.configue.static.path.data.mkdir(
            parents=True, exist_ok=True
        )
    )
    container.final(lambda: _logger.debug("Program closed."))
    container.on_boot_insert(
        1,
        lambda: _logger.debug(
            "Program boot finished."
        )
    )

    # on debug
    _logger.debug("Program boot registry finished.")
    if _config.debug:
        _logger.info(
            "Debugging is turned boot, "
            "and debug information will be output. \n"
            "Note: This does not enable debug-level logging."
        )
        _logger.debug("Container: %s\n", container)
        # _logger.debug("%s\n", app.config)
        _logger.debug(app.plugins)
        container.on_final_insert(0, lambda: (
            _logger.debug("The program is exiting gracefully.")
        ))


def _main() -> int | str:
    _logger.addHandler(memory_handler)
    _logger.debug("Program start.")

    app = Application()
    app.container.reg(
        "app",
        lambda: app,
        returns=Application
    )
    runtime.install()
    _boot(app)

    return app.exec()


def main() -> int | str:
    return runtime.run(_main)


if __name__ == "__main__":
    exit(main())
