#
"""Bootstrap configuration."""

from pathlib import Path
from typing import TYPE_CHECKING, Dict
from logging import getLogger

from .configue import Configue
from .utils import *
from .utils.ini_parser import RawINIParser

import albuswall

if TYPE_CHECKING:
    from albuswall.core import Container

_Missing = object()
_logger = getLogger(f"{albuswall.__name__}.configue")


def setup_config(
        container,  # type: Container
        config_file_path: Path
):
    from albuswall.core import Application
    from albuswall.configue import ConfigDeclaration

    config: "Configue" = Application.instance().configure

    container.reg("config", lambda: config, returns=Configue)
    container.reg("configue", lambda: config, returns=Configue)

    declaration = ConfigDeclaration()

    # 内建字段注册
    app_name = albuswall.__title__
    app_author = albuswall.__author__
    for key, (section, field, default, typ) in {
        "path.config": ("path", "config", get_user_config_dir(app_name, app_author), Path),
        "path.data": ("path", "data", get_user_data_dir(app_name, app_author), Path),
        "path.cache": ("path", "cache", get_cache_dir(app_name, app_author), Path),
        "path.temp": ("path", "temp", get_temp_dir(app_name), Path),
        "files": ("files", None, {}, Dict[str, Path]),
    }.items():
        if field is None:
            declaration.registry[key] = {
                "path": (section,), "default": default, "type": typ,
            }
        else:
            declaration.registry[key] = {
                "path": (section, field), "default": default, "type": typ,
            }

    # 读 INI 原文 → raw dict
    raw = load_file(config_file_path)

    # 灌进 Resolver
    for (sec, key), value in raw.items():
        config.resolver.inject_raw(sec, key, value)

    # 注册类型 + 注入缺失的默认值
    declaration.load(config.resolver)

    _logger.info(f"Config file: {config_file_path}")


def load_file(file: Path) -> dict[tuple[str, str], str]:
    match file.suffix[1:]:
        case "ini":
            return load_ini_(file)
        case _:
            raise RuntimeError(f"Not supported file suffix: {file.suffix}")


def load_ini_(file: Path) -> dict[tuple[str, str], str]:
    parser = RawINIParser()
    if not file.exists():
        parser.read_string("", source=str(file))
    else:
        with file.open("r", encoding="utf-8") as f:
            parser.read_file(f, source=str(file))
    return parser.to_raw_dict()
