#
""""""

from itertools import chain
from pathlib import Path
from logging import getLogger
from typing import TYPE_CHECKING

from albuswall.log import TRACE, Logger

from .registory import PreferenceDeclaration

if TYPE_CHECKING:
    from albuswall.configue import Configue

FILE_TYPE = "ini"

_logger: Logger = getLogger(  # type: ignore
    ".".join(str(__name__).split(".")[:-1]))
_logger.trace = lambda msg, *args: \
    _logger.log(TRACE, msg, *args)


def _resolve_preference_file(config: "Configue") -> Path:
    """与静态配置同构：显式配置优先，否则回落到 data/preference/<name>.ini。"""
    if config.static.ui.preference_file is not None:
        return Path(config.static.ui.preference_file)
    return (
        config.static.path.data / "preference" /
        (config.static.files.preference + "." + FILE_TYPE)
    )


def setup(config: "Configue"):
    """加载动态 preference：把 preference.ini 解析成 dict，落到 dynamic.preference。

    与 bootstrap.setup_config 对 static 的做法完全同构：
        PreferenceDeclaration  →  等价于 ConfigDeclaration
        _build_type_map_and_defaults  →  复用（类型 + 默认值）
        TypedConfigParser / DictInterpolation  →  复用
        dynamic.preference  →  等价于 config.static
    """
    # 延迟导入，避免与 bootstrap 循环依赖
    from albuswall.configue.bootstrap import (
        _build_type_map_and_defaults,
        _needs_default_section,
        _promote_default_section,
    )
    from albuswall.configue.utils.ini_parser import (
        TypedConfigParser, DictInterpolation,
    )

    preference_file = _resolve_preference_file(config)
    _logger.debug("Preference file: %s", str(preference_file))

    # 1) 实例化 Declaration：__init__ 会调用 load()，
    #    把每个 PreferenceField 的 default 预先写进 dynamic.preference，
    #    保证即使 INI 里缺键，字段也读得到默认值。
    declaration = PreferenceDeclaration()

    # 2) 从注册表构建 type_map / defaults —— 与静态完全同一个函数
    type_map, defaults = _build_type_map_and_defaults(declaration)
    _logger.trace(
        "Preference type_map=%s, defaults=%s",
        list(type_map.keys()), list(defaults.keys()))

    parser = TypedConfigParser(
        type_map=type_map,
        interpolation=DictInterpolation(defaults),
        default=defaults,
    )

    # 3) 读 INI —— 与 bootstrap.load_ini_ 完全相同的三种分支
    if not preference_file.is_file():
        _logger.warning(
            "Preference file not found, using defaults: %s",
            str(preference_file))
        parser.read_string("", source=str(preference_file))
    elif _needs_default_section(preference_file):
        with preference_file.open("r", encoding="utf-8") as f:
            parser.read_file(
                chain(["[__default__]\n"], f), source=str(preference_file))
    else:
        with preference_file.open("r", encoding="utf-8") as f:
            parser.read_file(f, source=str(preference_file))

    # 4) 取回结构化的 dict，__default__ 提升到顶层（静态同款处理）
    config_dict = _promote_default_section(parser.load())

    # 5) 写入 dynamic.preference
    node = PreferenceDeclaration._get_config_namespace()
    _apply(node, config_dict)

    _logger.debug("Loaded preferences from: %s", str(preference_file))


def _apply(node, config_dict):
    """把 TypedConfigParser 产出的 dict 落到 dynamic.preference 上。

    结构对应：
        config_dict = {
            "ui": {"theme": "dark", "window_title": "..."},
            "album": {...},
            "language": "zh_CN",     # 顶层键（来自 __default__ 提升）
        }
    即：外层是 section，内层是 field。
    """
    for section, fields in config_dict.items():
        if isinstance(fields, dict):
            section_ns = node.subtree(section)
            for key, value in fields.items():
                if value is None:
                    # parser.load() 对未定义键会补 None，
                    # 不要用它盖掉 Declaration 已经写进去的默认值
                    continue
                setattr(section_ns, key, value)
                _logger.trace(
                    "Loaded preference %s.%s = %r", section, key, value)
        else:
            if fields is None:
                continue
            setattr(node, section, fields)
            _logger.trace("Loaded preference %s = %r", section, fields)
