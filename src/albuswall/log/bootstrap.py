#
""""""

import logging
import logging.config
from typing import TYPE_CHECKING, Union
from pprint import pformat
from pathlib import Path

import albuswall
from albuswall.core import Container
from albuswall.configue import ConfigField
from albuswall.configue.utils import get_user_config_dir

from .common import TRACE
from .handlers import MemoryCacheHandler

if TYPE_CHECKING:
    from albuswall.configue import Configue

logging.addLevelName(TRACE, "TRACE")
_logger = logging.getLogger(f"{albuswall.__name__}.log")

LOG_HEAD = "[Log]"


# noinspection bad-assignment
class LogConf:
    root = ConfigField("log", default={})
    log_file: str = ConfigField("files", "log", default="log_config.ini")
    log_file_path: str = ConfigField("log", "config", default=None)
    level: Union[int, str] = ConfigField("log", "level", default=None)


def setup_log(container: Container):
    config: Configue = container.get("config")
    # ensure method is not enable in frozen namespace
    # config.static.ensure("log", Namespace)
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

    # 用类型查找，别用 handlers[0]
    root_logger = logging.getLogger(albuswall.__name__)
    buffer_handler = next(
        (h for h in root_logger.handlers
         if isinstance(h, MemoryCacheHandler)),
        None,
    )

    if log_conf_file.is_file():
        msg = f"Loaded log file: {log_conf_file}"
        _logger.debug(msg)
        logging.getLogger(albuswall.__name__).removeHandler(buffer_handler)
        logging.config.fileConfig(log_conf_file, disable_existing_loggers=False)
    else:
        msg = f"Not found log config: {log_conf_file}"
        _logger.warning(msg)
        logging.getLogger(albuswall.__name__).removeHandler(buffer_handler)
    # print(log_conf_file.read_text())

    if log_level is not None:
        try:
            logging.getLogger(albuswall.__name__).setLevel(log_level)
        except ValueError as e:
            msg = pformat(f"{LOG_HEAD} Set root logger log level failed: {e}")
            # noinspection PyNoneFunctionAssignment
            [_logger.warning(l) for l in msg.split("\n")]

    # clear buffer 重放缓冲区（关键修复）
    # handle() 不做级别过滤，必须自己用 isEnabledFor 检查
    if buffer_handler is not None and hasattr(buffer_handler, "buffer"):
        for record in list(buffer_handler.buffer):
            source_logger = logging.getLogger(record.name)
            if source_logger.isEnabledFor(record.levelno):
                source_logger.handle(record)
        buffer_handler.buffer.clear()
    # # print(config.static.files)
    # print(f"_logger.level = {_logger.level}")
    # print(f"_logger.propagate = {_logger.propagate}")
    # print(f"root handlers = {logging.getLogger().handlers}")
    # print(f"albuswall handlers = {logging.getLogger('albuswall').handlers}")
