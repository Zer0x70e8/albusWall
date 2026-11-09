# -*- coding: utf-8 -*-
"""add_row 的 @overload 静态分发与实现。

静态层（@overload）：按 control 的 Literal 分发，IDE 层面做参数窄化。
动态层（实现函数）：签名宽松，运行时靠 row_guard.assert_row_args 保底。

挂载方式：``FormView.add_row = add_row``（见 form_view.py），
因此实现体里的 ``self`` 就是 FormView，真正的组装委托给
``self._construct_row(...)``。
"""

from __future__ import annotations

from typing import (
    TYPE_CHECKING, Any, Callable, Literal, Optional, Union, overload,
)

from .row_format import (
    RowControl,
    RowAccess,
    BrowseKind,
    ResetPosition,
    ValidationMode,
    RowSpec,
)
from .row_guard import assert_row_args
from .row_handle import RowHandle

if TYPE_CHECKING:
    from PySide6.QtWidgets import QLabel  # 或 PyQt


# ============================================================
# @overload：按 control 的 Literal 分发
# ============================================================

# ---- KV / PASSWORD / KEYSEQUENCE / COLOR（默认组，最宽松）----
@overload
def add_row(
    self,
    label: Union[str, "QLabel", None] = None,
    value: Any = None,
    *,
    control: Optional[Literal[
        RowControl.KV, RowControl.PASSWORD,
        RowControl.KEYSEQUENCE, RowControl.COLOR,
    ]] = RowControl.KV,
    spec: None = None,
    access: RowAccess = RowAccess.READWRITE,
    reset_position: Optional[ResetPosition] = ResetPosition.INLINE,
    ident: Optional[str] = None,
    default: Any = None,
    transform: Optional[Callable[[Any], Any]] = None,
    validator: Optional[Callable[[Any], tuple]] = None,
    validation_mode: ValidationMode = ValidationMode.ON_COMMIT,
    placeholder: Optional[str] = None,
    label_span: int = 9,
    field_span: int = 3,
) -> RowHandle: ...


# ---- MULTILINE ----
@overload
def add_row(
    self,
    label: Union[str, "QLabel", None] = None,
    value: Any = None,
    *,
    control: Literal[RowControl.MULTILINE],
    spec: None = None,
    access: RowAccess = RowAccess.READWRITE,
    reset_position: Optional[ResetPosition] = ResetPosition.INLINE,
    ident: Optional[str] = None,
    default: Any = None,
    transform: Optional[Callable[[Any], Any]] = None,
    validator: Optional[Callable[[Any], tuple]] = None,
    validation_mode: ValidationMode = ValidationMode.ON_COMMIT,
    placeholder: Optional[str] = None,
    height: Optional[int] = None,
    label_span: int = 9,
    field_span: int = 3,
) -> RowHandle: ...


# ---- NUMBER ----
@overload
def add_row(
    self,
    label: Union[str, "QLabel", None] = None,
    value: Any = None,
    *,
    control: Literal[RowControl.NUMBER],
    spec: None = None,
    access: RowAccess = RowAccess.READWRITE,
    reset_position: Optional[ResetPosition] = ResetPosition.INLINE,
    ident: Optional[str] = None,
    default: Any = None,
    transform: Optional[Callable[[Any], Any]] = None,
    validator: Optional[Callable[[Any], tuple]] = None,
    validation_mode: ValidationMode = ValidationMode.ON_COMMIT,
    minimum: Optional[float] = None,
    maximum: Optional[float] = None,
    step: Optional[float] = None,
    decimals: Optional[int] = None,
    label_span: int = 9,
    field_span: int = 3,
) -> RowHandle: ...


# ---- SLIDER ----
@overload
def add_row(
    self,
    label: Union[str, "QLabel", None] = None,
    value: Any = None,
    *,
    control: Literal[RowControl.SLIDER],
    spec: None = None,
    access: RowAccess = RowAccess.READWRITE,
    reset_position: Optional[ResetPosition] = ResetPosition.INLINE,
    ident: Optional[str] = None,
    default: Any = None,
    transform: Optional[Callable[[Any], Any]] = None,
    validator: Optional[Callable[[Any], tuple]] = None,
    validation_mode: ValidationMode = ValidationMode.ON_COMMIT,
    minimum: Optional[float] = None,
    maximum: Optional[float] = None,
    step: Optional[float] = None,
    orientation: Any = None,
    label_span: int = 9,
    field_span: int = 3,
) -> RowHandle: ...


# ---- COMBO ----
@overload
def add_row(
    self,
    label: Union[str, "QLabel", None] = None,
    value: Any = None,
    *,
    control: Literal[RowControl.COMBO],
    spec: None = None,
    access: RowAccess = RowAccess.READWRITE,
    reset_position: Optional[ResetPosition] = ResetPosition.INLINE,
    ident: Optional[str] = None,
    default: Any = None,
    transform: Optional[Callable[[Any], Any]] = None,
    validator: Optional[Callable[[Any], tuple]] = None,
    validation_mode: ValidationMode = ValidationMode.ON_COMMIT,
    items: list,
    label_span: int = 9,
    field_span: int = 3,
) -> RowHandle: ...


# ---- RADIO ----
@overload
def add_row(
    self,
    label: Union[str, "QLabel", None] = None,
    value: Any = None,
    *,
    control: Literal[RowControl.RADIO],
    spec: None = None,
    access: RowAccess = RowAccess.READWRITE,
    reset_position: Optional[ResetPosition] = ResetPosition.INLINE,
    ident: Optional[str] = None,
    default: Any = None,
    transform: Optional[Callable[[Any], Any]] = None,
    validator: Optional[Callable[[Any], tuple]] = None,
    validation_mode: ValidationMode = ValidationMode.ON_COMMIT,
    items: list,
    orientation: Any = None,
    label_span: int = 9,
    field_span: int = 3,
) -> RowHandle: ...


# ---- CHECKBOX / LABEL ----
@overload
def add_row(
    self,
    label: Union[str, "QLabel", None] = None,
    value: Any = None,
    *,
    control: Literal[RowControl.CHECKBOX, RowControl.LABEL],
    spec: None = None,
    access: RowAccess = RowAccess.READWRITE,
    reset_position: Optional[ResetPosition] = ResetPosition.INLINE,
    ident: Optional[str] = None,
    default: Any = None,
    transform: Optional[Callable[[Any], Any]] = None,
    validator: Optional[Callable[[Any], tuple]] = None,
    validation_mode: ValidationMode = ValidationMode.ON_COMMIT,
    text: Optional[str] = None,
    wrap: Optional[bool] = None,
    label_span: int = 9,
    field_span: int = 3,
) -> RowHandle: ...


# ---- PATH ----
@overload
def add_row(
    self,
    label: Union[str, "QLabel", None] = None,
    value: Any = None,
    *,
    control: Literal[RowControl.PATH],
    spec: None = None,
    access: RowAccess = RowAccess.READWRITE,
    reset_position: Optional[ResetPosition] = ResetPosition.INLINE,
    ident: Optional[str] = None,
    default: Any = None,
    transform: Optional[Callable[[Any], Any]] = None,
    validator: Optional[Callable[[Any], tuple]] = None,
    validation_mode: ValidationMode = ValidationMode.ON_COMMIT,
    browse_kind: Optional[BrowseKind] = None,
    placeholder: Optional[str] = None,
    label_span: int = 9,
    field_span: int = 3,
) -> RowHandle: ...


# ---- SUBFORM：schema / rows 二选一（两个 overload）----
@overload
def add_row(
    self,
    label: Union[str, "QLabel", None] = None,
    value: Any = None,
    *,
    control: Literal[RowControl.SUBFORM],
    spec: None = None,
    access: RowAccess = RowAccess.READWRITE,
    reset_position: Optional[ResetPosition] = ResetPosition.INLINE,
    ident: Optional[str] = None,
    default: Any = None,
    transform: Optional[Callable[[Any], Any]] = None,
    validator: Optional[Callable[[Any], tuple]] = None,
    validation_mode: ValidationMode = ValidationMode.ON_COMMIT,
    schema: dict,
    rows: None = None,
    label_span: int = 9,
    field_span: int = 3,
) -> RowHandle: ...


@overload
def add_row(
    self,
    label: Union[str, "QLabel", None] = None,
    value: Any = None,
    *,
    control: Literal[RowControl.SUBFORM],
    spec: None = None,
    access: RowAccess = RowAccess.READWRITE,
    reset_position: Optional[ResetPosition] = ResetPosition.INLINE,
    ident: Optional[str] = None,
    default: Any = None,
    transform: Optional[Callable[[Any], Any]] = None,
    validator: Optional[Callable[[Any], tuple]] = None,
    validation_mode: ValidationMode = ValidationMode.ON_COMMIT,
    schema: None = None,
    rows: list,   # list[RowHandle]
    label_span: int = 9,
    field_span: int = 3,
) -> RowHandle: ...


# ---- SEPARATOR（最小签名）----
@overload
def add_row(
    self,
    label: None = None,
    value: None = None,
    *,
    control: Literal[RowControl.SEPARATOR],
    spec: None = None,
    access: None = None,
    reset_position: None = None,
    ident: None = None,
    default: None = None,
    transform: None = None,
    validator: None = None,
    validation_mode: None = None,
    label_span: int = 9,
    field_span: int = 3,
) -> RowHandle: ...


# ---- spec 模式：三轴禁，值语义参数全收 ----
# 注意：reset_position 在 spec overload 里保持 `None = None`
#      ——IDE 层面只允许 None / 省略；运行时 None 与默认 INLINE 都视为「不覆盖 spec」。
@overload
def add_row(
    self,
    label: Union[str, "QLabel", None] = None,
    value: Any = None,
    *,
    spec: RowSpec,
    control: None = None,
    access: None = None,
    reset_position: None = None,
    ident: Optional[str] = None,
    default: Any = None,
    transform: Optional[Callable[[Any], Any]] = None,
    validator: Optional[Callable[[Any], tuple]] = None,
    validation_mode: ValidationMode = ValidationMode.ON_COMMIT,
    text: Optional[str] = None,
    placeholder: Optional[str] = None,
    height: Optional[int] = None,
    minimum: Optional[float] = None,
    maximum: Optional[float] = None,
    step: Optional[float] = None,
    decimals: Optional[int] = None,
    items: Optional[list] = None,
    browse_kind: Optional[BrowseKind] = None,
    schema: Optional[dict] = None,
    rows: Optional[list] = None,
    orientation: Any = None,
    wrap: Optional[bool] = None,
    label_span: int = 9,
    field_span: int = 3,
) -> RowHandle: ...


# ---- 自定义 str 控件：兜底，**extra 全放行 ----
@overload
def add_row(
    self,
    label: Union[str, "QLabel", None] = None,
    value: Any = None,
    *,
    control: str,
    spec: None = None,
    access: RowAccess = RowAccess.READWRITE,
    reset_position: Optional[ResetPosition] = ResetPosition.INLINE,
    ident: Optional[str] = None,
    default: Any = None,
    transform: Optional[Callable[[Any], Any]] = None,
    validator: Optional[Callable[[Any], tuple]] = None,
    validation_mode: ValidationMode = ValidationMode.ON_COMMIT,
    label_span: int = 9,
    field_span: int = 3,
    **extra: Any,
) -> RowHandle: ...


# ============================================================
# 实现（签名宽松；overload 只做静态窄化，运行时靠 assert_row_args 保底）
# ============================================================
def add_row(
    self: Any,
    label: Any = None,
    value: Any = None,
    *,
    spec: Any = None,
    control: Any = None,
    access: Any = None,
    reset_position: Any = ResetPosition.INLINE,
    ident: Any = None,
    default: Any = None,
    transform: Any = None,
    validator: Any = None,
    validation_mode: Any = ValidationMode.ON_COMMIT,
    text: Any = None,
    placeholder: Any = None,
    height: Any = None,
    minimum: Any = None,
    maximum: Any = None,
    step: Any = None,
    decimals: Any = None,
    items: Any = None,
    browse_kind: Any = None,
    schema: Any = None,
    rows: Any = None,
    orientation: Any = None,
    wrap: Any = None,
    label_span: Any = 9,
    field_span: Any = 3,
    **extra: Any,
) -> RowHandle:
    # ---- 归一 spec ----
    if spec is not None:
        if control is not None or access is not None:
            raise ValueError("spec 与 control/access 互斥")
        # reset_position=None 是合法值（"无按钮，default 保留给程序化重置"），
        # 与默认 INLINE 同视为「不覆盖 spec 三轴」；只有 BELOW 才算显式覆盖 → 报错。
        if reset_position not in (None, ResetPosition.INLINE):
            raise ValueError("spec 与 reset_position 互斥")
        resolved = spec
    else:
        resolved = RowSpec(
            control=control if control is not None else RowControl.KV,
            access=access if access is not None else RowAccess.READWRITE,
            reset_position=reset_position,
        )

    # ---- 守卫（公开函数，逻辑单一来源）----
    assert_row_args(
        resolved,
        label=label, value=value, ident=ident, default=default,
        transform=transform, validator=validator,
        validation_mode=validation_mode,
        text=text, placeholder=placeholder, height=height,
        minimum=minimum, maximum=maximum, step=step, decimals=decimals,
        items=items, browse_kind=browse_kind,
        schema=schema, rows=rows, orientation=orientation,
        wrap=wrap,
        label_span=label_span, field_span=field_span,
        **extra,
    )

    # ---- 真正构建（FormView 负责布局 / 装饰 / 句柄装配）----
    return self._construct_row(
        resolved,
        label=label,
        value=value,
        ident=ident,
        default=default,
        transform=transform,
        validator=validator,
        validation_mode=validation_mode,
        text=text,
        placeholder=placeholder,
        height=height,
        minimum=minimum,
        maximum=maximum,
        step=step,
        decimals=decimals,
        items=items,
        browse_kind=browse_kind,
        schema=schema,
        rows=rows,
        orientation=orientation,
        wrap=wrap,
        label_span=label_span,
        field_span=field_span,
        extra=extra,
    )
