# -*- coding: utf-8 -*-
"""add_row 的运行时校验层。

分层：
- check_row_args：汇总 Issue，不抛错（外部可用）；
- assert_row_args：add_row 内部转异常。

逻辑单一来源：所有 add_row 的运行时约束都在这里，add_row 只调 assert_row_args。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import Tuple

from .row_format import (
    RowControl,
    RowSpec,
    COMMON_PARAMS,
    CONTROL_PARAMS,
    IGNORED_PARAMS,
)

#: 表单栅格列数（label_span + field_span 的上界）
GRID_COLS = 12


class IssueKind(Enum):
    UNSUPPORTED = auto()   # 形态不接受该参数
    MUTEX = auto()         # 互斥
    MISSING = auto()       # 必需缺失
    VALUE = auto()         # 值不合法


@dataclass(frozen=True)
class Issue:
    kind: IssueKind
    message: str
    params: Tuple[str, ...] = ()

    def __str__(self) -> str:
        return f"[{self.kind.name}] {self.message}"


def _is_whole_row(control) -> bool:
    return control in (RowControl.SEPARATOR, RowControl.LABEL)


def check_row_args(spec: RowSpec, /, **kwargs) -> list:
    """汇总某次 add_row 调用的全部问题；不抛错。

    kwargs 里值为 None 的参数视为「未传」，直接跳过。
    """
    issues: list = []
    control = spec.control
    is_custom = isinstance(control, str)

    # LABEL / SEPARATOR 上被静默忽略的参数（见 row_format.IGNORED_PARAMS）
    ignored = (frozenset() if is_custom
               else IGNORED_PARAMS.get(control, frozenset()))

    # ---- 1. 形态参数适用性 ----
    if not is_custom:
        for name, val in kwargs.items():
            if val is None:
                continue
            if name in ignored:
                continue          # 文档承诺「静默忽略」
            if name in COMMON_PARAMS:
                continue          # 结构参数全部放行（整行控件静默忽略）
            if name not in CONTROL_PARAMS.get(control, frozenset()):
                issues.append(Issue(
                    IssueKind.UNSUPPORTED,
                    f"{name} 不适用于 {control.name}",
                    (name,),
                ))

    # ---- 2. 互斥：schema / rows ----
    if control is RowControl.SUBFORM:
        has_schema = kwargs.get("schema") is not None
        has_rows = kwargs.get("rows") is not None
        if has_schema == has_rows:
            issues.append(Issue(
                IssueKind.MUTEX,
                "SUBFORM 需要 schema 或 rows 二选一",
                ("schema", "rows"),
            ))

    # ---- 3. 数值范围 ----
    if control in (RowControl.NUMBER, RowControl.SLIDER):
        mn, mx = kwargs.get("minimum"), kwargs.get("maximum")
        if mn is not None and mx is not None and mn > mx:
            issues.append(Issue(IssueKind.VALUE, "minimum > maximum",
                                ("minimum", "maximum")))
        if (d := kwargs.get("decimals")) is not None and d < 0:
            issues.append(Issue(IssueKind.VALUE, "decimals < 0", ("decimals",)))

    # ---- 4. default / reset_position ----
    default = kwargs.get("default")
    reset_position = spec.reset_position
    if default is None and reset_position is not None:
        # 静默忽略：无按钮可放，不报错（与 ResetPosition 枚举文档一致）
        pass

    # ---- 5. 栅格 span ----
    label_span, field_span = kwargs.get("label_span"), kwargs.get("field_span")
    if not _is_whole_row(control):
        if label_span is not None and label_span < 1:
            issues.append(Issue(IssueKind.VALUE, "label_span 必须 >= 1",
                                ("label_span",)))
        if field_span is not None and field_span < 1:
            issues.append(Issue(IssueKind.VALUE, "field_span 必须 >= 1",
                                ("field_span",)))
        if (label_span is not None and field_span is not None
                and label_span + field_span > GRID_COLS):
            issues.append(Issue(
                IssueKind.VALUE,
                f"label_span + field_span 超出 {GRID_COLS} 栅格",
                ("label_span", "field_span"),
            ))

    return issues


def assert_row_args(spec: RowSpec, /, **kwargs) -> None:
    """add_row 内部用：有 Issue 就抛 ValueError。"""
    issues = check_row_args(spec, **kwargs)
    if issues:
        raise ValueError("; ".join(str(i) for i in issues))
