#
"""插件发现层：内置包 / ``entry_points`` / 用户目录。

三个来源都只做一件事：**把模块 import 进来**。真正的注册动作是 import 的
副作用——模块顶层的 ``@plugin`` 装饰器会把插件对象塞进
:data:`albuswall.core.plugins._pending`。因此本模块不返回插件列表，收集结果
走 ``PluginManager.load_pending()``（或 ``load_builtin`` / ``load_entry_points``
/ ``load_directory`` 这些发现入口）。

顶层只依赖 stdlib，所以 ``discovery`` 相对 ``PluginManager`` 是「叶子」：后者
在方法体里延迟 import 本模块，不会成环，也不会在 import 期产生副作用。
"""

from __future__ import annotations

import importlib
import importlib.metadata as md
import importlib.util
import logging
import pkgutil
import sys
import types
from pathlib import Path
from typing import Any

__all__ = [
    "ENTRY_POINT_GROUP",
    "discover_all",
    "discover_directory",
    "discover_entry_points",
    "discover_package",
]

_logger = logging.getLogger(__name__)

#: ``pyproject.toml`` 里第三方插件使用的 entry point 组名。
ENTRY_POINT_GROUP = "albuswall.plugins"


def discover_package(package: str) -> None:
    """第一方插件：扫描包内所有子模块并 import（触发顶层 ``@plugin``）。

    包不存在时只记 debug —— 一个被剥干净的安装不该因为「没有内置插件目录」
    就启动失败。
    """
    try:
        pkg = importlib.import_module(package)
    except ModuleNotFoundError:
        _logger.debug("plugin package %r is not available, skipping.", package)
        return

    if not hasattr(pkg, "__path__"):
        _logger.warning("%r is not a package, cannot scan", package)
        return

    for _, mod_name, _ in pkgutil.iter_modules(pkg.__path__):
        if mod_name.startswith("_"):
            continue
        _import_quietly(f"{package}.{mod_name}")


# noinspection broad-exception
def discover_entry_points(group: str = ENTRY_POINT_GROUP) -> None:
    """第三方 pip 包：通过 ``entry_points`` 声明插件模块。

    在 ``pyproject.toml`` 里这样声明::

        [project.entry-points."albuswall.plugins"]
        foo = "albuswall_foo"

    target 可以是模块（import 即触发模块内的 ``@plugin``），也可以是 callable
    （显式调用一次，由它自己完成声明）。
    """
    try:
        eps: Any = md.entry_points(group=group)
    except TypeError:
        # Python 3.9 及更早：entry_points() 返回 dict
        # noinspection unresolved-references
        legacy: Any = md.entry_points()
        eps = legacy.get(group, [])

    for ep in eps:
        try:
            target: Any = ep.load()
        except Exception:
            _logger.exception("failed to load entry_point %r, skipped", ep.name)
            continue

        # 情形 1：模块 —— module 级 @plugin 已经在 import 期执行过，无需动作
        # 情形 2：callable —— 显式调用一次，让它自己完成声明
        if isinstance(target, types.ModuleType):
            pass
        elif callable(target):
            try:
                target()
            except Exception:
                _logger.exception("failed to execute entry_point %r, skipped", ep.name)
        else:
            _logger.warning(
                "entry_point %r points to an unsupported object, skipped",
                ep.name,
            )


def discover_directory(
        path: Path | None,
        prefix: str = "_albuswall_user_plugin",
) -> None:
    """用户脚本：扫描目录下的 ``.py`` 并 import。

    ``path`` 为 ``None`` 时直接跳过（用户插件目录默认关闭，见 D7）。
    """
    if path is None:
        return
    if not path.is_dir():
        _logger.debug("plugin dir %s does not exist, skipping", path)
        return

    for py in sorted(path.glob("*.py")):
        if py.name.startswith("_"):
            continue
        mod_name = f"{prefix}.{py.stem}"
        if mod_name in sys.modules:
            continue
        _import_from_path(mod_name, py)


def discover_all(
        builtin_package: str,
        user_dir: Path | None = None,
        entry_group: str = ENTRY_POINT_GROUP,
) -> None:
    """按「内置 → ``entry_points`` → 用户目录」顺序发现。

    **不推荐**直接使用：它一次跑完三个来源，无法按来源做启用/禁用决策。
    新代码优先用 ``PluginManager.load_builtin`` / ``load_entry_points`` /
    ``load_directory``——它们 import 完立刻把 ``_pending`` 收进管理器，且返回
    新收的插件数量。

    无论走哪个入口，发现完都必须 ``PluginManager.load_pending()`` 才会真正
    进入管理器；``user_dir=None``（默认）表示跳过用户目录。
    """
    discover_package(builtin_package)
    discover_entry_points(entry_group)
    discover_directory(user_dir)


# ---------- 内部 ----------

# noinspection broad-exception
def _import_quietly(full_name: str) -> None:
    """import 子模块；坏插件只记日志，不拖垮启动。"""
    try:
        importlib.import_module(full_name)
        _logger.debug("loaded plugin module %r", full_name)
    except Exception:
        _logger.exception("failed to load plugin module %r, skipped", full_name)


# noinspection broad-exception
def _import_from_path(mod_name: str, path: Path) -> None:
    """按文件路径 import 用户插件；失败时把半成品从 ``sys.modules`` 摘掉。"""
    try:
        spec = importlib.util.spec_from_file_location(mod_name, path)
        if spec is None or spec.loader is None:
            _logger.warning("cannot construct spec for %s, skipping", path)
            return
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)
        _logger.debug("loaded user plugin %r from %s", mod_name, path)
    except Exception:
        sys.modules.pop(mod_name, None)
        _logger.exception("failed to load user plugin %s, skipped", path)
