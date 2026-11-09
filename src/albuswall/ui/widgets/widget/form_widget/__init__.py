# -*- coding: utf-8 -*-
"""表单控件包（form_widget）。

对外只有一个入口：``FormView.add_row(...)``。四轴枚举与预设值
（``RowControl`` / ``RowAccess`` / ``ResetPosition`` / ``ValidationMode`` 与
``RowSpec`` + ``PRESET_*``）是本包的另一半公开面。

模块分工：
    row_format.py  四轴枚举、参数作用域表、RowSpec 与预设（纯数据，无 Qt 依赖）
    row_guard.py   add_row 的运行时校验（Issue / check_row_args / assert_row_args）
    row_builder.py add_row 的 @overload 静态分发 + 实现（委托 FormView 装配）
    row_handle.py  行句柄（值语义 / 提交基线 / 状态点 / 读写状态 / 错误提示）
    row_history.py 值历史栈的纯逻辑（无 Qt 依赖，可单测）
    controls.py    RowControl → 构建 / 读 / 写 注册表（自然默认值也在这里）
    fields.py      复合字段（分割线 / 颜色 / 路径）
    form_view.py   12 栅格表单视图本体
    __main__.py    可运行演示（``python -m albuswall.ui.widgets.widget.form_widget``）

名字按需解析（PEP 562，见 :mod:`albuswall.ui.widgets._lazy`），
因此 ``import form_widget`` 不会立刻把整个 Qt 依赖树拉起来。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..._lazy import install

# ============================================================
# 公开名 -> 定义它的模块（相对本包）
# ============================================================
_LAZY: dict[str, str] = {
    # ---- 表单本体 ----
    "FormView": "form_view",
    "RowHandle": "row_handle",
    "RowHistory": "row_history",
    "ValidationError": "row_handle",
    # ---- 四轴枚举 ----
    "RowControl": "row_format",
    "RowAccess": "row_format",
    "BrowseKind": "row_format",
    "ResetPosition": "row_format",
    "ValidationMode": "row_format",
    # ---- 组合声明 ----
    "RowSpec": "row_format",
    # ---- 参数作用域表 ----
    "COMMON_PARAMS": "row_format",
    "CONTROL_PARAMS": "row_format",
    "IGNORED_PARAMS": "row_format",
    "supports": "row_format",
    "supported_params": "row_format",
    # ---- 运行时自查 ----
    "Issue": "row_guard",
    "IssueKind": "row_guard",
    "check_row_args": "row_guard",
    "assert_row_args": "row_guard",
    "GRID_COLS": "row_guard",
    # ---- 预设 ----
    "PRESET_KV": "row_format",
    "PRESET_KV_READONLY": "row_format",
    "PRESET_KV_DISABLED": "row_format",
    "PRESET_MULTILINE": "row_format",
    "PRESET_NUMBER": "row_format",
    "PRESET_PASSWORD": "row_format",
    "PRESET_KEYSEQUENCE": "row_format",
    "PRESET_SLIDER": "row_format",
    "PRESET_COMBO": "row_format",
    "PRESET_COMBO_READONLY": "row_format",
    "PRESET_RADIO": "row_format",
    "PRESET_CHECKBOX": "row_format",
    "PRESET_COLOR": "row_format",
    "PRESET_PATH": "row_format",
    "PRESET_SUBFORM": "row_format",
    "PRESET_SEPARATOR": "row_format",
    "PRESET_LABEL": "row_format",
}

__all__ = [
    "BrowseKind",
    "COMMON_PARAMS",
    "CONTROL_PARAMS",
    "FormView",
    "GRID_COLS",
    "IGNORED_PARAMS",
    "Issue",
    "IssueKind",
    "PRESET_CHECKBOX",
    "PRESET_COLOR",
    "PRESET_COMBO",
    "PRESET_COMBO_READONLY",
    "PRESET_KEYSEQUENCE",
    "PRESET_KV",
    "PRESET_KV_DISABLED",
    "PRESET_KV_READONLY",
    "PRESET_LABEL",
    "PRESET_MULTILINE",
    "PRESET_NUMBER",
    "PRESET_PASSWORD",
    "PRESET_PATH",
    "PRESET_RADIO",
    "PRESET_SEPARATOR",
    "PRESET_SLIDER",
    "PRESET_SUBFORM",
    "ResetPosition",
    "RowAccess",
    "RowControl",
    "RowHandle",
    "RowHistory",
    "RowSpec",
    "ValidationError",
    "ValidationMode",
    "assert_row_args",
    "check_row_args",
    "supported_params",
    "supports",
]

if TYPE_CHECKING:  # pragma: no cover - 仅供 IDE / 类型检查器
    from .form_view import FormView
    from .row_format import (
        CONTROL_PARAMS,
        COMMON_PARAMS,
        IGNORED_PARAMS,
        PRESET_CHECKBOX,
        PRESET_COLOR,
        PRESET_COMBO,
        PRESET_COMBO_READONLY,
        PRESET_KV,
        PRESET_KV_DISABLED,
        PRESET_KV_READONLY,
        PRESET_KEYSEQUENCE,
        PRESET_LABEL,
        PRESET_MULTILINE,
        PRESET_NUMBER,
        PRESET_PASSWORD,
        PRESET_PATH,
        PRESET_RADIO,
        PRESET_SEPARATOR,
        PRESET_SLIDER,
        PRESET_SUBFORM,
        BrowseKind,
        ResetPosition,
        RowAccess,
        RowControl,
        RowSpec,
        ValidationMode,
        supported_params,
        supports,
    )
    from .row_guard import (
        GRID_COLS,
        Issue,
        IssueKind,
        assert_row_args,
        check_row_args,
    )
    from .row_handle import RowHandle, ValidationError
    from .row_history import RowHistory

install(__name__, globals(), _LAZY)
