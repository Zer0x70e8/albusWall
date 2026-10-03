#
"""Bootstrap configuration.

两阶段：
  - setup_static_config: 单一主 INI → Resolver → StaticConfig（只读）
  - setup_dynamic_config: 注册懒加载工厂 → DynamicConfig（运行时可变、可持久化）
"""

import importlib
from pathlib import Path
from typing import TYPE_CHECKING, Dict, Sequence, Optional

from albuswall.log import getLogger

from .configue import Configue
from .utils import *
from .utils.ini_parser import RawINIParser

import albuswall

if TYPE_CHECKING:
    from albuswall.core import Container

_Missing = object()
_logger = getLogger(f"{albuswall.__name__}.configue")


# ── 阶段一：静态配置 ────────────────────────────────
def setup_static_config(container, config_file_path: Path) -> Configue:
    from albuswall.core import Application
    from albuswall.configue import ConfigDeclaration

    config: "Configue" = Application.instance().configure

    container.reg("config", lambda: config, returns=Configue)
    container.reg("configue", lambda: config, returns=Configue)

    # ── 内建字段：直接注册到类 ─────────────────
    app_name = albuswall.__title__
    app_author = albuswall.__author__

    builtins = {
        "path._config": (("path", "config"), get_user_config_dir(app_name, app_author), Path),
        "path.data": (("path", "data"), get_user_data_dir(app_name, app_author), Path),
        "path.cache": (("path", "cache"), get_cache_dir(app_name, app_author), Path),
        "path.temp": (("path", "temp"), get_temp_dir(app_name), Path),
        "files": (("files",), {}, Dict[str, Path]),
    }
    for key, (path, default, typ) in builtins.items():
        ConfigDeclaration.add_builtin(key, path, default, typ)

    # ── 读主 INI → raw dict ────────────────────
    raw = load_file(config_file_path)
    for (sec, key), value in raw.items():
        config.resolver.inject_raw(sec, key, value)

    # ── 注册类型 + 注入缺失默认值 ──────────────
    ConfigDeclaration.load(config.resolver)

    _logger.info(
        "Static _config loaded: file=%s sections=%d keys=%d types=%d",
        config_file_path,
        len(config.resolver.sections()),
        sum(1 for _ in config.resolver.nodes()),
        len(config.resolver.schema()),
    )
    return config


# ── 阶段二：动态配置 ────────────────────────────────
def setup_dynamic_config(
        container,  # type: Container
        *,
        modules: Optional[Sequence[str]] = None,
        preload: Optional[Sequence[str]] = None,
) -> None:
    """消费 DYNAMIC_REGISTRY，把每个登记的子树挂成 mount_lazy。

    参数：
      modules: 需要 import 的状态模块列表，保证装饰器先跑过。
               例如 ["app.state", "app.ui"]。
    """
    from albuswall.core import Application
    from .dynamic_declaration import DYNAMIC_REGISTRY

    for mod in (modules or ()):
        importlib.import_module(mod)

    config: Configue = Application.instance().configure
    static = config.static

    def _make_loader(spec, path: Path):
        """闭包工厂：避免在循环里用默认参数捕获变量。"""

        def _load():
            return spec.cls.load(path, autosave=spec.autosave, **spec.overrides)

        return _load

    for mount_name, mount_spec in DYNAMIC_REGISTRY.items():
        mount_path = mount_spec.resolve_path(static)
        config.dynamic.mount_lazy(mount_name, _make_loader(mount_spec, mount_path))
        _logger.debug("Dynamic mount registered: %s -> %s", mount_name, mount_spec)

    for name in (preload or ()):
        getattr(config.dynamic, name)  # 强制触发加载

    # container.reg(
    #     "dynamic_config",
    #     lambda: _config.dynamic,
    #     returns=type(_config.dynamic),
    # )
    _logger.info("Dynamic _config mounts registered: %s", list(DYNAMIC_REGISTRY))


def _resolve_path(static, path_key) -> Path:
    """从 StaticConfig 按元组取路径。例如 ('path','cache','user.json')。"""
    node = static
    for part in path_key:
        node = getattr(node, part)
    return Path(node)


# ── 文件读取 ────────────────────────────────────────
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
