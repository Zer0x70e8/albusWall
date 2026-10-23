#
"""Bootstrap configuration.

两阶段：
  - setup_static_config: 主 INI（可含 include 子树）→ Resolver → StaticConfig（只读）
  - setup_dynamic_config: import 状态模块 → DynamicConfig（运行时可变、可持久化）

多文件加载
==========

主 INI 的 ``[configue]`` 段可声明：

.. code-block:: ini

    [configue]
    include    = ./machine.ini, ./profile.d/*.ini
    include.ro = ./defaults.ini

合并顺序（低 → 高优先级，高优先级覆盖低优先级）：

1. ``include.ro`` 列出的文件（按出现顺序，靠后的赢；这些文件天然只读）
2. ``include`` 列出的文件（同上；文件系统不可写的视同只读）
3. 主文件自身

约定：

- 逗号分隔（与 ``_convert`` 的 list 语义一致）；
- 路径含 ``*`` / ``?`` / ``[`` 时用 ``glob.glob`` 展开，结果按字典序；
- 相对路径相对主文件所在目录解析；
- 子文件自己的 ``include`` 被忽略（扁平结构，不做递归 include），
  避免复杂度和不可预期的加载顺序；
- ``pkg://`` 前缀暂不支持（抛 ``ConfigError``，TODO: importlib.resources）；
- include 指向的文件不存在 → ``ConfigError``（严格）；
- ``[configue] include`` 系字段不会进入 Resolver，StaticConfig 看不到它们。
"""

import glob
import importlib
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional, Sequence

from albuswall.log import getLogger

from .configue import Configue
from .declaration import ConfigField
from .exceptions import ConfigError, ConfigueErrorSource
from .utils import *
from .utils.ini_parser import RawINIParser

import albuswall

if TYPE_CHECKING:
    from albuswall.core import Container

_Missing = object()
_logger = getLogger(f"{albuswall.__name__}.configue")

# [configue] 段里由 load_config_tree 消费、不进 Resolver 的键
_CONFIGURE_SECTION = "configue"
_INCLUDE_KEY = "include"
_INCLUDE_RO_KEY = "include.ro"
_INCLUDE_KEYS = frozenset({_INCLUDE_KEY, _INCLUDE_RO_KEY})

# glob 元字符
_GLOB_CHARS = ("*", "?", "[")


# noinspection bad-assignment
class _ConfigueConfig:
    """配置器自己用自己"""
    disable_type_check: bool = ConfigField("configue", "disable_type_check", default=False)

    # include 系字段只作声明（供文档 / IDE 补全），不注入 Resolver：
    # 它们由 load_config_tree 从原始文件里消费，不应出现在 StaticConfig 里。
    include: str = ConfigField(
        _CONFIGURE_SECTION, _INCLUDE_KEY, default="", inject_default=False,
    )
    include_ro: str = ConfigField(
        _CONFIGURE_SECTION, _INCLUDE_RO_KEY, default="", inject_default=False,
    )


_conf = _ConfigueConfig()


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
        "path.cache": (("path", "cache"), get_cache_dir(app_name), Path),
        "path.temp": (("path", "temp"), get_temp_dir(app_name), Path),
        "files": (("files",), {}, Dict[str, Path]),
    }
    for key, (path, default, typ) in builtins.items():
        ConfigDeclaration.add_builtin(key, path, default, typ)

    # ── 读 INI 树 → raw dict（含 origin）───────
    raw, readonly_files = load_config_tree_ex(config_file_path)
    for (sec, key), (value, origin) in raw.items():
        config.resolver.inject_raw(sec, key, value, origin=origin)
    for path in readonly_files:
        config.resolver.mark_readonly_file(path)

    # 回写默认落点：主 INI（运行期新 key / 只读来源的兜底）
    config.configure_persistence(default_write_file=config_file_path)

    # ── 注册类型 + 注入缺失默认值 ──────────────
    ConfigDeclaration.load(config.resolver)

    _logger.info(
        "Static _config loaded: file=%s sections=%d keys=%d types=%d",
        config_file_path,
        len(config.resolver.sections()),
        sum(1 for _ in config.resolver.nodes()),
        len(config.resolver.schema()),
    )
    # check
    if not _conf.disable_type_check:
        config.resolver.check_all()
    return config


# ── 阶段二：动态配置 ────────────────────────────────
def setup_dynamic_config(
        container,  # type: Container
        *,
        modules: Optional[Sequence[str]] = None,
        preload: Optional[Sequence[str]] = None,
) -> None:
    """只负责 import 状态模块，把懒加载下沉到 ``DynamicConfig.__getattr__``。

    参数：
      modules: 需要 import 的状态模块列表，保证 ``@dynamic_state`` 装饰器先跑过。
               例如 ["app.state", "app.ui"]。
      preload: 需要立刻触发加载的挂载名（默认保持懒加载）。
    """
    from albuswall.core import Application
    from .dynamic_declaration import DYNAMIC_REGISTRY

    for mod in (modules or ()):
        importlib.import_module(mod)

    config: Configue = Application.instance().configure
    for name in (preload or ()):
        getattr(config.dynamic, name)  # 强制触发加载（DYNAMIC_REGISTRY fallback）

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


# ── 多文件加载 ──────────────────────────────────────
def _split_include_list(value: Any) -> list[str]:
    """把 include 字段拆成路径列表（逗号分隔，忽略空项）。"""
    if value is None:
        return []
    return [part.strip() for part in str(value).split(",") if part.strip()]


def _expand_include_spec(spec: str, base_dir: Path, *, key: str) -> list[Path]:
    """展开单个 include 条目为具体文件列表。

    - ``pkg://`` 前缀：未实现，抛 ConfigError（TODO: importlib.resources）
    - 含 glob 元字符：``glob.glob`` 展开后按字典序（无匹配视为空）
    - 其余：视为单个文件路径（不存在由调用方报错）
    """
    if spec.startswith("pkg://"):
        # TODO: 用 importlib.resources 映射到包内只读资源
        raise ConfigError(
            "pkg:// include is not implemented yet",
            source=ConfigueErrorSource.FILE,
            key=key,
            value=spec,
        )

    candidate = Path(spec)
    if not candidate.is_absolute():
        candidate = base_dir / candidate

    if any(ch in str(candidate) for ch in _GLOB_CHARS):
        matches = sorted(glob.glob(str(candidate)))
        if not matches:
            _logger.debug("Include glob matched nothing: %s", candidate)
        return [Path(m) for m in matches]
    return [candidate]


def _strip_include_keys(raw: dict[tuple[str, str], Any]) -> dict[tuple[str, str], Any]:
    """剔除 [configue] include 系（它们不该出现在 Resolver 里）。"""
    return {
        (sec, key): value for (sec, key), value in raw.items()
        if not (sec == _CONFIGURE_SECTION and key in _INCLUDE_KEYS)
    }


def load_config_tree_ex(
        main: Path,
) -> tuple[dict[tuple[str, str], tuple[str, Path]], list[Path]]:
    """``load_config_tree`` 的完整版：额外返回只读来源文件列表。

    Returns:
        (merged, readonly_files)：
          merged: {(section, key): (value, origin_path)}，origin 为生效值所在文件；
          readonly_files: 只读来源（include.ro 中的文件 + 文件系统不可写的 include 文件）。
    """
    main = Path(main)
    main_abs = main.resolve()
    base_dir = main_abs.parent

    if not main.exists():
        _logger.warning("Main _config file not found: %s", main)
        return {}, []

    main_full = load_file(main)
    include_ro = _split_include_list(
        main_full.get((_CONFIGURE_SECTION, _INCLUDE_RO_KEY))
    )
    include = _split_include_list(
        main_full.get((_CONFIGURE_SECTION, _INCLUDE_KEY))
    )
    main_raw = _strip_include_keys(main_full)

    # ── 展开顺序 = 合并顺序（低 → 高）────────────
    # (path, readonly)
    ordered: list[tuple[Path, bool]] = []
    for key, specs, forced_ro in (
            (_INCLUDE_RO_KEY, include_ro, True),
            (_INCLUDE_KEY, include, False),
    ):
        for spec in specs:
            for path in _expand_include_spec(spec, base_dir, key=key):
                if not path.exists():
                    raise ConfigError(
                        f"include target not found: {path}",
                        source=ConfigueErrorSource.FILE,
                        key=key,
                        value=str(path),
                    )
                ordered.append((path, forced_ro))

    # ── 循环检测：主文件不能出现在自己的 include 列表里 ──
    for path, _ in ordered:
        if path.resolve() == main_abs:
            raise ConfigError(
                f"include cycle: {main_abs} includes itself",
                source=ConfigueErrorSource.FILE,
                key=_INCLUDE_KEY,
                value=str(path),
            )

    merged: dict[tuple[str, str], tuple[str, Path]] = {}
    readonly_files: list[Path] = []

    # 1. include.ro（天然只读）
    # 2. include（文件系统不可写 → 视同只读）
    for path, forced_ro in ordered:
        raw = _strip_include_keys(load_file(path))
        is_ro = forced_ro or not os.access(path, os.W_OK)
        if is_ro:
            readonly_files.append(path)
        for node, value in raw.items():
            merged[node] = (value, path)
        _logger.debug(
            "Merged include: file=%s keys=%d readonly=%s", path, len(raw), is_ro
        )

    # 3. 主文件自身（最高优先级）
    for node, value in main_raw.items():
        merged[node] = (value, main_abs)
    _logger.debug("Merged main: file=%s keys=%d", main_abs, len(main_raw))

    _logger.debug(
        "Config tree merged: files=%d nodes=%d readonly=%d",
        len(ordered) + 1, len(merged), len(readonly_files),
    )
    return merged, readonly_files


def load_config_tree(main: Path) -> dict[tuple[str, str], tuple[str, Path]]:
    """读取主 INI 及其 include 子树，返回 ``{(section, key): (value, origin)}``。

    合并顺序（低 → 高优先级）：
      1. 主文件 include.ro 列出的文件（按出现顺序，靠后的赢）
      2. 主文件 include 列出的文件（同上）
      3. 主文件自身

    子文件自己的 include 会被忽略（扁平结构，不做递归 include）。

    失败语义：include 文件不存在 / 出现 pkg:// 前缀 → ``ConfigError(source=FILE)``；
    主文件不存在时返回空 dict（与旧行为一致）。
    """
    merged, _ = load_config_tree_ex(main)
    return merged
