#
""""""

import typing
import warnings
from pathlib import Path
from typing import Any, Dict, TYPE_CHECKING
from logging import getLogger

from .configue import Configue
from .utils import *
from .utils.ini_parser import TypedConfigParser, DictInterpolation
from .utils.deep_merge import deep_merge_dicts

import albuswall

if TYPE_CHECKING:
    from albuswall.core import Container
    from albuswall.configue.declaration import ConfigDeclaration

_Missing = object()
_logger = getLogger(f"{albuswall.__name__}.configue")


def setup_config(
        container,  # type: Container
        config_file_path: Path
):
    # lazy import
    from albuswall.core import Application
    from albuswall.configue import ConfigDeclaration
    import albuswall

    config: "Configue" = Application.instance().configure

    container.reg("config", lambda: config)
    container.reg("configue", lambda: config)

    declaration = ConfigDeclaration()
    # basic_config = get_basic_file_config()
    # Inside ConfigService initialization or setup
    app_name = albuswall.__title__
    app_author = albuswall.__author__
    declaration.registry["path.config"] = {
        "path": ("path", "config"),
        "default": get_user_config_dir(app_name, app_author),
        "type": Path,
    }
    declaration.registry["path.data"] = {
        "path": ("path", "data"),
        "default": get_user_data_dir(app_name, app_author),
        "type": Path,
    }
    declaration.registry["path.cache"] = {
        "path": ("path", "cache"),
        "default": get_cache_dir(app_name, app_author),
        "type": Path,
    }
    declaration.registry["path.temp"] = {
        "path": ("path", "temp"),
        "default": get_temp_dir(app_name),
        "type": Path,
    }
    declaration.registry["files"] = {
        "path": ("files",),
        "default": {},
        "type": Dict[str, Path],
    }
    declaration.load()

    _logger.info(f"Config file: {config_file_path}")

    # 将 __default__ 节（INI 无节部分）提升到根级别
    config_dict = load_file(config_file_path, declaration)
    #
    # # 合并基本路径配置和最终配置
    # final_config = deep_merge_dicts(basic_config, config_dict)
    config.static.load(config_dict)


# def get_basic_file_config():
#     app_name = albuswall.__title__
#     app_author = albuswall.__author__
#     return {"path": {
#         "config": get_user_config_dir(app_name, app_author),
#         "data": get_user_data_dir(app_name, app_author),
#         "cache": get_cache_dir(app_name, app_author),
#         "temp": get_temp_dir(app_name),
#     },
#         "files": {}
#     }


def load_file(file: Path, service):
    match file.suffix[1:]:
        case "ini":
            return load_ini_(file, service)
        case _:
            msg = f"Not supported file suffix: {file.suffix}"
            raise RuntimeError(msg)


# noinspection PyShadowingNames
def _resolve_annotation(ann: Any) -> Any:
    """将字符串注解解析为类型对象，失败时回退为 str。"""
    if isinstance(ann, str):
        import builtins
        namespace = {}
        namespace.update(vars(builtins))
        namespace.update(vars(typing))
        # 额外加入一些常见类型，确保 eval 能找到
        namespace.update({
            "Path": Path,
            "Any": Any,
            "Optional": typing.Optional,
            "List": typing.List,
            "Tuple": typing.Tuple,
            "Dict": typing.Dict,
            "Union": typing.Union,
        })
        try:
            return eval(ann, namespace)
        except Exception as e:
            warnings.warn(f"Failed to resolve annotation string '{ann}': {e}, falling back to str")
            return str
    return ann


def _build_type_map_and_defaults(service: "ConfigDeclaration"):
    """从 ConfigService 注册表构建 type_map 和 defaults。"""
    type_map: Dict[str, Dict[str, Any]] = {}
    defaults: Dict[str, Dict[str, Any]] = {}

    # 先处理双路径条目，确保节的默认值已建立
    for key, meta in service.registry.items():
        path = meta["path"]
        if len(path) != 2:
            continue
        section, field = path
        resolved_type = _resolve_annotation(meta["type"])
        type_map.setdefault(section, {})[field] = resolved_type
        if meta["default"] is not _Missing:
            defaults.setdefault(section, {})[field] = meta["default"]

    # 处理单路径条目
    for key, meta in service.registry.items():
        path = meta["path"]
        if len(path) != 1:
            continue
        field = path[0]
        resolved_type = _resolve_annotation(meta["type"])
        default = meta["default"]

        # 判断是否为字典类型（配置节）
        is_dict_type = (
                resolved_type is dict or
                (hasattr(resolved_type, "__origin__") and
                 resolved_type.__origin__ in (dict, typing.Dict)) or
                isinstance(default, dict)
        )

        if is_dict_type:
            # 视为节，节名就是 field
            section = field
            if default is not _Missing and isinstance(default, dict):
                # 获取已有的节默认值（来自双路径条目），深度合并，双路径优先
                existing = defaults.get(section, {})
                merged = deep_merge_dicts(default, existing)  # existing 覆盖 default
                defaults[section] = merged
            # 不将 field 放入 __default__
            continue
        else:
            # 标量，放入 __default__
            type_map.setdefault("__default__", {})[field] = resolved_type
            if default is not _Missing:
                defaults.setdefault("__default__", {})[field] = default

    # defaults = deep_merge_dicts(defaults, get_basic_file_config())

    return type_map, defaults


def _promote_default_section(config_dict: Dict[str, Any]) -> Dict[str, Any]:
    """将 '__default__' 节的内容合并到顶层，并移除该键。"""
    if "__default__" in config_dict:
        default_section = config_dict.pop("__default__")
        # 使用深度合并，避免覆盖顶层已有的同名键（通常不会存在）
        return deep_merge_dicts(config_dict, default_section)
    return config_dict


from itertools import chain


def _needs_default_section(file: Path) -> bool:
    """判断第一个有效行是否不是节头。文件不存在/为空/已有节头则返回 False。"""
    if not file.exists():
        return False
    with file.open("r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith(("#", ";")):
                continue
            return not stripped.startswith("[")
    return False


def load_ini_(file: Path, service: "ConfigDeclaration"):
    type_map, defaults = _build_type_map_and_defaults(service)
    parser = TypedConfigParser(
        type_map=type_map,
        interpolation=DictInterpolation(defaults),
        default=defaults,
    )

    if not file.exists():
        parser.read_string("", source=str(file))
    elif _needs_default_section(file):
        with file.open("r", encoding="utf-8") as f:
            parser.read_file(chain(["[__default__]\n"], f), source=str(file))
    else:
        with file.open("r", encoding="utf-8") as f:
            parser.read_file(f, source=str(file))

    config_dict = parser.load()
    return _promote_default_section(config_dict)
