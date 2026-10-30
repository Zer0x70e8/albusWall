#
""""""

import logging
import logging.config
import sys
from pathlib import Path
from pprint import pformat

import albuswall
from albuswall.configue1 import Configue, configclass
from albuswall.configue1.utils.platform_dir import get_user_config_dir
from albuswall.core import Container

from .common import to_levelno
from .handlers import MemoryCacheHandler

# _logger = logging.getLogger(f"{albuswall.__name__}.logger")
_logger = logging.getLogger(__package__)

LOG_HEAD = "[Log]"

_DEFAULT_LOG_FORMAT = (
    "%(asctime)s [%(levelname)s] %(name)s:%(lineno)d - %(message)s"
)


# noinspection bad-assignment
@configclass("log", "main")
class LogConf:
    """``[log]`` 段（与 ``resources/config/conf.ini`` 一致）。

    - ``config``：日志配置文件路径，支持 ``${path:config}/${files:log}`` 展开；
      留空则退回 ``<user config dir>/<files.log>``。
    - ``level``：``albuswall`` logger 的级别（``TRACE`` / ``DEBUG`` / ``INFO`` /
      ``WARNING`` / ``ERROR`` / ``CRITICAL`` 或数字）；留空表示不改。

    注意：旧包的 ``LogConf`` 读的是 ``[logger] level`` / ``[files] logger`` /
    ``[logger] _config``——这三个键在随包发布的 ``conf.ini`` 里**根本不存在**，
    所以旧代码的 ``[log] level`` / ``[log] config`` 一直是死键。迁移后改为读
    ``[log]``，代价是随包默认的 ``level = WARNING`` 现在真的生效（原先
    ``albuswall`` logger 保持 NOTSET、继承 root 的 TRACE）。
    """

    config: str | None = None
    level: int | str | None = None


def setup_log(container: Container):
    config: Configue = container.require("config")
    tree = config.tree.main

    log_section = tree.log
    log_level: int | str | None = (
        log_section.level if "level" in log_section else None
    )
    raw_log_config = log_section.config if "config" in log_section else None

    if raw_log_config:
        log_conf_file = Path(str(raw_log_config))
    else:
        log_conf_file = (
            Path(get_user_config_dir(albuswall.__title__, albuswall.__author__))
            / tree.files.log
        )

    root_logger = logging.getLogger(albuswall.__name__)
    buffer_handler: MemoryCacheHandler | None = next(
        (h for h in root_logger.handlers if isinstance(h, MemoryCacheHandler)),
        None,
    )

    if isinstance(buffer_handler, MemoryCacheHandler):
        root_logger.removeHandler(buffer_handler)

        if log_conf_file.is_file():
            _logger.debug("Loaded logger file: %s", log_conf_file)
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
                "Not found logger _config: %s; "
                "falling back to stderr StreamHandler",
                log_conf_file,
            )

    if log_level is not None:
        try:
            logging.getLogger(albuswall.__name__).setLevel(to_levelno(log_level))
        except (ValueError, TypeError) as e:
            msg = pformat(f"{LOG_HEAD} Set root logger logger level failed: {e}")
            # noinspection none-function-assignment
            [_logger.warning(line) for line in msg.split("\n")]

    # 重放缓冲区
    if buffer_handler is not None and hasattr(buffer_handler, "buffer"):
        for record in list(buffer_handler.buffer):
            source_logger = logging.getLogger(record.name)
            if source_logger.isEnabledFor(record.levelno):
                source_logger.handle(record)
        buffer_handler.buffer.clear()
