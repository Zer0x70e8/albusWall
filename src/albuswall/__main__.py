#
""""""

import logging

import albuswall
from albuswall.core import Application, Runtime
from albuswall.plugin import discover_all
from albuswall.configue import (
    setup_config, ConfigField, parse_section_file,
    get_user_config_dir, get_user_data_dir
)
from albuswall.log import setup_log
from albuswall.infrastructure import register_database
from albuswall.repositories import registry_repository
from albuswall.services import register_service
from albuswall.ui import registry_ui
from albuswall.log.handlers import MemoryCacheHandler

CONFIG_FILE_PATH = get_user_config_dir(
    albuswall.__title__, albuswall.__author__)
DATA_FILE_PATH = get_user_data_dir(
    albuswall.__title__, albuswall.__author__)
CONFIG_FILE_NAME = "config.ini"

logging.getLogger().setLevel(1)
memory_handler = MemoryCacheHandler()
_logger = logging.getLogger(albuswall.__name__)
runtime = Runtime(memory_handler)


# noinspection bad-assignment
class ConfV:
    debug: bool = ConfigField("debug", default=False)


def _boot(app: Application) -> None:
    container = app.container

    discover_all(
        builtin_package="albuswall.plugins",
        user_dir=DATA_FILE_PATH / "plugins",
    )
    plugin_disables = parse_section_file(
        CONFIG_FILE_PATH / CONFIG_FILE_NAME,
        "plugin_disables",
        required=False,
    )

    app.plugins.activate(container, disabled=plugin_disables)

    setup_config(container, CONFIG_FILE_PATH / CONFIG_FILE_NAME)
    setup_log(container)
    app.log_enable = True
    register_database(container)
    registry_repository(container)
    register_service(container)
    registry_ui(container)

    container.on_boot_insert(0, lambda: app.configue.static.path.data.mkdir(
        parents=True, exist_ok=True))
    container.final(lambda: _logger.debug("Program closed."))

    # if container.get("config").static.debug:
    if ConfV().debug:
        _logger.info("Debugging is turned on, "
                     "and debug information will be output. \n"
                     "Note: This does not enable debug-level logging."
                     )
        _logger.debug("Container: %s\n", container)
        _logger.debug("%s\n", app.config)
        _logger.debug(app.plugins)
        container.on_final_insert(0, lambda:
        _logger.debug("The program is exiting gracefully."))
        container.on_boot_insert(
            -1, lambda: _logger.debug(f"{app.config.dynamic}")
        )

    container.on_boot_insert(-1, lambda: _logger.debug("Program boot finished."))

    # # test
    # from albuswall.dto.source import IngestSourceCreate
    # container.get("ingest_source_service").create_source(IngestSourceCreate(
    #     title = "my library",
    #     source_path = "/data/myCode/py/albuswall/assets/thumbs",
    # ))


def _main() -> int | str:
    _logger.addHandler(memory_handler)
    _logger.debug("Program start.")

    app = Application()
    app.container.reg("app", lambda: app)
    runtime.install()
    _boot(app)

    return app.exec()


def main() -> int | str:
    return runtime.run(_main)


if __name__ == "__main__":
    exit(main())
