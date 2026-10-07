#
""""""

import sys
import logging
import logging.config
from pathlib import Path
from pprint import pformat
from typing import TYPE_CHECKING, Union

import albuswall
from albuswall.core import Container
from albuswall.configue import ConfigField
from albuswall.configue.utils import get_user_config_dir

from .handlers import MemoryCacheHandler

if TYPE_CHECKING:
    from albuswall.configue import Configue

# _logger = logging.getLogger(f"{albuswall.__name__}.log")
_logger = logging.getLogger(__package__)

LOG_HEAD = "[Log]"

_DEFAULT_LOG_FORMAT = (
    "%(asctime)s [%(levelname)s] %(name)s:%(lineno)d - %(message)s"
)


# noinspection bad-assignment
class LogConf:
    root = ConfigField("log", default={})
    log_file: str = ConfigField("files", "log", default="log_config.ini")
    log_file_path: str = ConfigField("log", "_config", default=None)
    level: Union[int, str] = ConfigField("log", "level", default=None)


def setup_log(container: Container):
    config: Configue = container.get("config")
    config.static.path.ensure("config", Path)
    config.static.files.ensure("log", None)
    confs = config.static.log

    log_level: int | str = confs.level
    # noinspection unreachable-code
    if LogConf().log_file_path is not None:
        log_conf_file: Path = Path(LogConf().log_file_path)
    else:
        log_conf_file = Path(get_user_config_dir(
            albuswall.__title__, albuswall.__author__)) / "log_config.ini"

    root_logger = logging.getLogger(albuswall.__name__)
    buffer_handler: MemoryCacheHandler | None = next(
        (h for h in root_logger.handlers if isinstance(h, MemoryCacheHandler)),
        None,
    )

    if isinstance(buffer_handler, MemoryCacheHandler):
        root_logger.removeHandler(buffer_handler)

        if log_conf_file.is_file():
            _logger.debug("Loaded log file: %s", log_conf_file)
            logging.config.fileConfig(
                log_conf_file, disable_existing_loggers=False
            )
        else:
            # ★ 关键修复：配置缺失时必须有替代 handler，
            #   否则缓冲移除后启动日志将没有任何落点。
            fallback = logging.StreamHandler(sys.stderr)
            fallback.setLevel(logging.NOTSET)
            fallback.setFormatter(logging.Formatter(
                _DEFAULT_LOG_FORMAT, datefmt="%Y-%m-%d %H:%M:%S"
            ))
            root_logger.addHandler(fallback)
            _logger.warning(
                "Not found log _config: %s; "
                "falling back to stderr StreamHandler",
                log_conf_file,
            )

    if log_level is not None:
        try:
            logging.getLogger(albuswall.__name__).setLevel(log_level)
        except ValueError as e:
            msg = pformat(f"{LOG_HEAD} Set root logger log level failed: {e}")
            # noinspection none-function-assignment
            [_logger.warning(l) for l in msg.split("\n")]

    # 重放缓冲区
    if buffer_handler is not None and hasattr(buffer_handler, "buffer"):
        for record in list(buffer_handler.buffer):
            source_logger = logging.getLogger(record.name)
            if source_logger.isEnabledFor(record.levelno):
                source_logger.handle(record)
        buffer_handler.buffer.clear()
