#
"""UI presenter 层。

对外只暴露组合根 :class:`PresenterManager`。各 presenter 模块刻意不在此
eager import —— 组合根按需逐个构造，任一模块 / 依赖缺失都只影响它自己，
而不是让整个 ``presenter`` 包 import 失败。
"""

from .manager import PresenterManager

__all__ = ["PresenterManager"]
