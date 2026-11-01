#
"""插件包：目前只有发现层。

历史说明：本包曾同时承载 ``declaration`` / ``manager`` 两个 UI 子插件模块，
它们在 ``wip/configue1-refactor`` 分支的重构里随 UI 子系统一起下线。新的插件
系统在 :mod:`albuswall.core.plugins`（``@plugin`` / ``PluginManager``），
本包只保留「发现」这一步：

- :func:`discover_package` —— 第一方插件包（默认 ``albuswall.plugins``）；
- :func:`discover_entry_points` —— 第三方 ``entry_points`` 声明；
- :func:`discover_directory` —— 用户脚本目录；
- :func:`discover_all` —— 以上三者的便捷组合（**不推荐**，优先用
  ``PluginManager.load_builtin`` / ``load_entry_points`` / ``load_directory``，
  它们各自 ``import`` 完立刻把 ``_pending`` 收进管理器）。

发现层只负责「import 模块」，注册动作来自模块顶层 ``@plugin`` 的副作用。
"""

from .discovery import (
    ENTRY_POINT_GROUP,
    discover_all,
    discover_directory,
    discover_entry_points,
    discover_package,
)

__all__ = [
    "ENTRY_POINT_GROUP",
    "discover_all",
    "discover_directory",
    "discover_entry_points",
    "discover_package",
]
