#
"""设置页**被动视图**的数据契约。

这一层只做三件事，全部是纯函数 / 纯数据，不 import Qt：

1. **版本收口**：``schema_version`` 不认识就拒绝渲染，绝不「猜一个结构」；
2. **归一化**：把 ``dump_schema_dict()`` + ``apply_present()`` 的产物解析成
   不可变的 :class:`~albuswall.ui.setting.model.SettingTree`（缺失的展示字段
   有明确兜底，值的字段绝不做类型推断之外的加工）；
3. **展示判定**：可编辑原因（:class:`DisabledReason`）、范围
   （:class:`Scope`）、提交模式（:class:`SubmitMode`）的取值域。

边界（与 UI 的「许愿清单」逐条对应）
====================================

* 类型 → 控件：视图读叶子的 ``widget``；只有它缺失时才用
  :func:`albuswall.common.setting_widget.infer_widget` 做**兜底**
  （契约要求 ``apply_present`` 负责映射）。
* i18n 求值：dump 时已完成；视图只显示 ``label`` / ``description``。
* ``${...}`` 展开：视图拿到的 ``value`` 已展开；``raw`` 只用于
  ``interpolated=True`` 时回显原串。
* 写回 / 落盘 / 文件归属：不属于本层；视图只读 ``target_file`` 做范围过滤。

归一化**只有一份实现**：:mod:`albuswall.ui.setting.model`。本模块保留
``normalize_dump`` / ``normalize_leaf`` 两个入口（旧调用点的稳定名字），
它们只是 ``SettingTree.parse`` / ``Leaf.from_raw`` 的薄包装。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from albuswall.common.enums import SettingWidget
from albuswall.common.setting_widget import infer_widget

from . import strings
from .model import (
    SUPPORTED_SCHEMA_VERSIONS,
    Component,
    Group,
    Leaf,
    SettingSchemaError,
    SettingTree,
    UncoveredItem,
    UnsupportedSchemaVersionError,
)

__all__ = [
    "SUPPORTED_SCHEMA_VERSIONS",
    "Component",
    "DisabledReason",
    "Group",
    "Leaf",
    "Scope",
    "ScopeKind",
    "SettingSchemaError",
    "SettingTree",
    "SettingWidget",
    "SubmitMode",
    "UncoveredItem",
    "UnsupportedSchemaVersionError",
    "editability",
    "format_value",
    "normalize_dump",
    "normalize_leaf",
    "resolve_widget",
]

#: ``widget`` 的取值域。唯一定义在 :class:`albuswall.common.enums.SettingWidget`，
#: 这里只 re-export，避免"两份枚举、类型互认"的历史问题。
_WIDGET_VALUES: frozenset[str] = frozenset(member.value for member in SettingWidget)


class ScopeKind(StrEnum):
    """右侧内容范围的种类。"""

    ALL = "all"
    FILE = "file"
    UNBOUND = "unbound"
    UNCOVERED = "uncovered"


@dataclass(frozen=True)
class Scope:
    """右侧内容的过滤范围（视图私有状态，不参与写回）。

    文件范围用完整路径而不是文件名：两个 ``config.ini`` 可能同名，
    只有完整路径才是唯一的。
    """

    kind: ScopeKind = ScopeKind.ALL
    file: str | None = None

    @classmethod
    def all(cls) -> Scope:
        return cls(ScopeKind.ALL)

    @classmethod
    def for_file(cls, path: str) -> Scope:
        return cls(ScopeKind.FILE, str(path))

    @classmethod
    def unbound(cls) -> Scope:
        return cls(ScopeKind.UNBOUND)

    @classmethod
    def uncovered(cls) -> Scope:
        return cls(ScopeKind.UNCOVERED)

    @property
    def label(self) -> str:
        """下拉框里显示的名字（文件范围只显示文件名，完整路径进 tooltip）。"""
        if self.kind is ScopeKind.ALL:
            return strings.SCOPE_ALL
        if self.kind is ScopeKind.UNBOUND:
            return strings.SCOPE_UNBOUND
        if self.kind is ScopeKind.UNCOVERED:
            return strings.SCOPE_UNCOVERED
        return _file_name(self.file or "")

    @property
    def tooltip(self) -> str:
        if self.kind is ScopeKind.FILE:
            return self.file or ""
        if self.kind is ScopeKind.UNBOUND:
            return strings.SCOPE_UNBOUND_TOOLTIP
        if self.kind is ScopeKind.UNCOVERED:
            return strings.SCOPE_UNCOVERED_TOOLTIP
        return strings.SCOPE_ALL_TOOLTIP


class DisabledReason(StrEnum):
    """叶子不可编辑的原因（统一走「禁用 + 原因 tooltip」）。"""

    NONE = ""
    FIXED = "fixed"
    READONLY = "readonly"
    READ_ERROR = "read_error"
    INTERPOLATED = "interpolated"
    UNBOUND = "unbound"

    @property
    def text(self) -> str:
        """给 tooltip 用的中文说明。"""
        return _REASON_TEXT[self]


_REASON_TEXT: dict[DisabledReason, str] = {
    DisabledReason.NONE: "",
    DisabledReason.FIXED: strings.REASON_FIXED,
    DisabledReason.READONLY: strings.REASON_READONLY,
    DisabledReason.READ_ERROR: strings.REASON_READ_ERROR,
    DisabledReason.INTERPOLATED: strings.REASON_INTERPOLATED,
    DisabledReason.UNBOUND: strings.REASON_UNBOUND,
}


class SubmitMode(StrEnum):
    """提交模式（三选一，对应三个按钮语义）。"""

    DRY_RUN = "dry_run"
    COLLECT_ERRORS = "collect_errors"
    DRAFT = "draft"

    @property
    def text(self) -> str:
        return {
            SubmitMode.DRY_RUN: strings.BUTTON_PREVIEW,
            SubmitMode.COLLECT_ERRORS: strings.BUTTON_SAVE,
            SubmitMode.DRAFT: strings.BUTTON_DRAFT,
        }[self]


# ============================================================================
# 归一化（实现只有一份：model.SettingTree / model.Leaf）
# ============================================================================

def normalize_leaf(leaf: Mapping[str, Any]) -> Leaf:
    """补全一个叶子的展示字段，返回不可变的 :class:`Leaf`。

    缺失字段的兜底全部显式写在 :meth:`Leaf.from_raw` 里，视图不再做第二套推断。
    """
    return Leaf.from_raw(leaf)


def normalize_dump(dump: Mapping[str, Any]) -> SettingTree:
    """校验版本 + 归一化，返回不可变的 :class:`SettingTree`。

    Raises:
        UnsupportedSchemaVersionError: ``schema_version`` 缺失或不在
            :data:`SUPPORTED_SCHEMA_VERSIONS` 里。
        SettingSchemaError: 重复 key / 缺 key / 不是 dict。
    """
    return SettingTree.parse(dump)


def resolve_widget(leaf: Mapping[str, Any]) -> str:
    """取叶子的 ``widget``；缺失或非法时用 :func:`infer_widget` 兜底。

    ``choice`` 但没有 ``choices`` 是坏数据：退回按类型兜底，避免渲染一个
    空下拉框（用户点了也提交不了任何值）。
    """
    widget = str(leaf.get("widget") or "").strip().lower()
    choices = leaf.get("choices") or ()
    if widget == SettingWidget.CHOICE.value and not choices:
        widget = ""
    if widget in _WIDGET_VALUES:
        return widget
    return infer_widget(str(leaf.get("type") or ""), choices)


# ============================================================================
# 展示判定
# ============================================================================

def editability(
        leaf: Mapping[str, Any],
) -> tuple[bool, DisabledReason, str | None]:
    """叶子能不能在界面改，以及不能改的**原因枚举**。

    顺序固定（前面的原因优先），与 tooltip 的文案一一对应：
    读失败 → 插值 → 无归属文件 → 只读文件 → 声明固定。

    Returns:
        ``(editable, reason, detail)``；``detail`` 是给 tooltip 的具体信息
        （读失败时包含异常文案，只读文件时包含文件名）。
    """
    if leaf.get("read_error"):
        return False, DisabledReason.READ_ERROR, str(leaf["read_error"])
    if leaf.get("interpolated"):
        raw = leaf.get("raw")
        return (
            False,
            DisabledReason.INTERPOLATED,
            strings.DETAIL_RAW_VALUE.format(value=raw)
            if raw is not None else None,
        )
    if leaf.get("target_file") is None:
        return False, DisabledReason.UNBOUND, None
    if leaf.get("readonly"):
        origin = leaf.get("origin_file")
        return (
            False,
            DisabledReason.READONLY,
            strings.DETAIL_FILE.format(path=origin) if origin else None,
        )
    if not leaf.get("editable", True):
        return False, DisabledReason.FIXED, None
    return True, DisabledReason.NONE, None


def format_value(value: Any) -> str:
    """给提示 / 预览表用的紧凑文本（字符串不加引号，其它走 JSON）。"""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return repr(value)


def _file_name(path: str) -> str:
    """路径 → 文件名（跨平台：只按最后一段切）。"""
    cleaned = path.replace("\\", "/").rstrip("/")
    return cleaned.rsplit("/", 1)[-1] if cleaned else path
