#
"""``albuswall.ui.setting`` —— 设置页的**被动视图**（不含写回）。

层职责
======

* :mod:`~albuswall.ui.setting.model`：不可变设置树（``Leaf`` / ``Group`` /
  ``SettingTree``）+ 索引 + 派生字段；
* :mod:`~albuswall.ui.setting.contract`：版本校验 / 归一化入口 / 展示判定；
* :mod:`~albuswall.ui.setting.content`：可见性 / 排序 / 合成分组（纯函数）；
* :mod:`~albuswall.ui.setting.strings`：全部用户可见文案（i18n 的落点）；
* :mod:`~albuswall.ui.setting.binder`：一个叶子的初值 / 当前值 / dirty / 错误；
* :mod:`~albuswall.ui.setting.editors`：值类别 → 编辑器注册表；
* :mod:`~albuswall.ui.setting.leaf_row`：叶子行控件（标签 + 编辑器 + 重置 + 错误）；
* :mod:`~albuswall.ui.setting.group_box`：可折叠分组；
* :mod:`~albuswall.ui.setting.preview`：``dry_run`` 变更预览表；
* :mod:`~albuswall.ui.setting.nav`：左侧锚点导航 + 两步焦点语义；
* :mod:`~albuswall.ui.setting.result`：写回报告的形状（唯一构造器）；
* :mod:`~albuswall.ui.setting.view`：整页被动视图（范围 / 锚点 / 提交 / 回填）；
* :mod:`~albuswall.ui.setting.demo`：特制 presenter/controller 的独立演示。

演示入口::

    QT_QPA_PLATFORM=offscreen python -m albuswall.ui.setting.demo --smoke
    python -m albuswall.ui.setting.demo
"""

from __future__ import annotations

from .binder import LeafBinder
from .contract import (
    SUPPORTED_SCHEMA_VERSIONS,
    Component,
    DisabledReason,
    Group,
    Leaf,
    Scope,
    ScopeKind,
    SettingSchemaError,
    SettingTree,
    SettingWidget,
    SubmitMode,
    UncoveredItem,
    UnsupportedSchemaVersionError,
    editability,
    normalize_dump,
    normalize_leaf,
)
from .group_box import SettingGroupBox
from .layout import (
    BUILTIN_THEME_GROUP,
    BUILTIN_THEME_ORDER,
    LayoutClaim,
    LayoutComponent,
    LayoutGroup,
    LayoutMove,
    SettingLayout,
    apply_layout,
    builtin_layout,
    merge_layouts,
)
from .leaf_row import SettingRow
from .view import SettingsPreviewDialog, SettingView

__all__ = [
    "BUILTIN_THEME_GROUP",
    "BUILTIN_THEME_ORDER",
    "SUPPORTED_SCHEMA_VERSIONS",
    "Component",
    "DisabledReason",
    "Group",
    "LayoutClaim",
    "LayoutComponent",
    "LayoutGroup",
    "LayoutMove",
    "Leaf",
    "LeafBinder",
    "Scope",
    "ScopeKind",
    "SettingGroupBox",
    "SettingLayout",
    "SettingRow",
    "SettingSchemaError",
    "SettingTree",
    "SettingView",
    "SettingWidget",
    "SettingsPreviewDialog",
    "SubmitMode",
    "UncoveredItem",
    "UnsupportedSchemaVersionError",
    "apply_layout",
    "builtin_layout",
    "merge_layouts",
    "editability",
    "normalize_dump",
    "normalize_leaf",
]
