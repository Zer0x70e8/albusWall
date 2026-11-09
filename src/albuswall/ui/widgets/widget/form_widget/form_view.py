# -*- coding: utf-8 -*-
"""表单视图（FormView）。

基于 12 栅格布局的表单视图，栅格划分与浏览器栅格系统一致：
**12 栅格是比例不是像素**，列宽随视口变化。

结构：
    FormView(QScrollArea)
    └─ content(QWidget) ← Grid12Layout
       ├─ label  ── 默认 0..9 栅格（CHECKBOX / 整行除外）
       │            TagLabel：描述文本 + 状态点（绿=默认值 / 淡蓝=已修改）
       └─ container ── 默认 9..12 栅格
          ├─ top: [field][🔒][↺]
          └─ error_label（常驻占位，高 18px）

四轴正交：
    RowControl     形态（widget 类 + 隐式布局契约）
    RowAccess      读写状态（READWRITE / READONLY / DISABLED）
    ResetPosition  重置按钮位置（INLINE / BELOW，存在性由 default 决定）
    ValidationMode 校验时机（ON_COMMIT / ON_CHANGE）

值语义（transform / validator / default）是每行的钩子，不进枚举。
"""

from __future__ import annotations

from typing import Any, Callable, ClassVar, Optional

from PySide6.QtCore import QObject, Qt
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QMessageBox, QPushButton, QScrollArea,
    QSizePolicy, QVBoxLayout, QWidget,
)

from ...layout.grid12_layout import Grid12Layout
from ..tag_label import TagLabel
from .controls import (
    CONTROL_SPECS, ControlSpec, format_editor_text, natural_default,
)
from .fields import SeparatorLine
from .row_builder import add_row
from .row_format import (
    BrowseKind, ResetPosition, RowAccess, RowControl, RowSpec, ValidationMode,
)
from .row_guard import GRID_COLS as _GRID_COLS
from .row_handle import RowHandle, ValidationError


def _centered(widget: QWidget, parent: QWidget) -> QWidget:
    """把控件垂直居中放进一个容器（栅格行高由同行控件决定）。"""
    holder = QWidget(parent)
    vbox = QVBoxLayout(holder)
    vbox.setContentsMargins(0, 0, 0, 0)
    vbox.setSpacing(0)
    vbox.addStretch(1)
    vbox.addWidget(widget, 0)
    vbox.addStretch(1)
    return holder


def _label_text(label: Any) -> str:
    if label is None:
        return ""
    if isinstance(label, QLabel):
        return label.text()
    return str(label)


def _default_custom_read(widget: QWidget) -> Any:
    text = getattr(widget, "text", None)
    if callable(text):
        return text()
    value = getattr(widget, "value", None)
    if callable(value):
        return value()
    return None


def _default_custom_write(widget: QWidget, raw: Any) -> None:
    set_text = getattr(widget, "setText", None)
    if callable(set_text):
        set_text("" if raw is None else str(raw))
        return
    set_value = getattr(widget, "setValue", None)
    if callable(set_value) and raw is not None:
        set_value(raw)


def _iter_schema_rows(schema: Any) -> list[dict[str, Any]]:
    """schema → add_row 关键字参数列表。

    允许两种写法：
        {"昵称": {"control": RowControl.KV}, "年龄": {"control": RowControl.NUMBER}}
        [{"ident": "nick", "control": RowControl.KV}, ...]
    """
    if isinstance(schema, dict):
        out: list[dict] = []
        for ident, decl in schema.items():
            decl = dict(decl) if isinstance(decl, dict) else {
                "label": str(ident), "value": decl,
            }
            decl.setdefault("ident", ident)
            decl.setdefault("label", str(ident))
            out.append(decl)
        return out
    if isinstance(schema, (list, tuple)):
        return [dict(decl) for decl in schema if isinstance(decl, dict)]
    raise ValueError("schema 必须是 dict 或 list[dict]")


class FormView(QScrollArea):
    """12 栅格表单视图（可滚动）。"""

    #: 栅格列数（与 row_guard 的 span 校验同源）
    GRID_COLS = _GRID_COLS

    #: 已注册的自定义控件：name -> ControlSpec
    _CUSTOM_CONTROLS: ClassVar[dict[str, ControlSpec]] = {}

    # 实现体在 row_builder（@overload 静态窄化 + 单一实现）
    add_row = add_row

    # ------------------------------------------------------------------
    # 自定义控件注册
    # ------------------------------------------------------------------
    @classmethod
    def register_control(
            cls,
            name: str,
            build: Callable[[QWidget, dict], QWidget],
            *,
            read: Optional[Callable[[QWidget], Any]] = None,
            write: Optional[Callable[[QWidget, Any], None]] = None,
            changed_signal: Optional[str] = None,
    ) -> None:
        """注册 ``control="name"`` 的自定义控件。

        build(parent, options) 里的 options 含 value / label / ident 以及
        调用方透传的 ``**extra``；read / write 缺省时按 text() / setText() 兜底。
        """
        key = str(name).strip().lower()
        if not key:
            raise ValueError("自定义控件名不能为空字符串")
        cls._CUSTOM_CONTROLS[key] = ControlSpec(
            build=build,
            read=read or _default_custom_read,
            write=write or _default_custom_write,
            changed_signal=changed_signal,
        )

    @classmethod
    def unregister_control(cls, name: str) -> None:
        cls._CUSTOM_CONTROLS.pop(str(name).strip().lower(), None)

    @classmethod
    def supported_controls(cls) -> list[str]:
        """全部可用 control 名（内置小写 + 自定义）。"""
        builtin = sorted(member.name.lower() for member in RowControl)
        return sorted(set(builtin) | set(cls._CUSTOM_CONTROLS))

    # ------------------------------------------------------------------
    def __init__(self, parent=None, h_spacing: int = 8, v_spacing: int = 8):
        super().__init__(parent)
        # 宽度契约：可缩放、纵向滚动条写死 AlwaysOn、横向 AlwaysOff、
        # 无边框；20px 边距由外部 parent 提供（滚动区外）。
        self.setWidgetResizable(True)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Preferred)

        self._content = QWidget()
        self._content.setSizePolicy(QSizePolicy.Policy.Expanding,
                                    QSizePolicy.Policy.Preferred)
        self._grid = Grid12Layout(self._content,
                                  h_spacing=h_spacing, v_spacing=v_spacing)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self.setWidget(self._content)

        # 错误态边框（作用于内部 edit）
        self._content.setStyleSheet(
            'QLineEdit[invalid="true"], QPlainTextEdit[invalid="true"], '
            'QComboBox[invalid="true"], QSpinBox[invalid="true"], '
            'QDoubleSpinBox[invalid="true"] {'
            '  border: 1px solid #d9534f; border-radius: 3px;'
            '}'
        )

        self._handles: list[RowHandle] = []
        self._next_row_id = 1

    # ------------------------------------------------------------------
    # 行信息
    # ------------------------------------------------------------------
    @property
    def row_count(self) -> int:
        return len(self._handles)

    def handles(self) -> list[RowHandle]:
        return list(self._handles)

    def rows(self) -> list[dict]:
        """按显示顺序返回行描述，含稳定 ``row_id``。"""
        return [
            {
                "row_id": h.row_id,
                "row": h.grid_row,
                "ident": h.ident,
                "label": h.label_widget,
                "field": h.field,
                "control": h.control,
                "spec": h.spec,
                "handle": h,
            }
            for h in self._handles
        ]

    def handle_of(self, ident: str) -> Optional[RowHandle]:
        """按 ident 找句柄（无 ident 的行不参与）。"""
        for h in self._handles:
            if h.ident == ident:
                return h
        return None

    def raw_values(self) -> dict:
        """``{ident 或 row_id: 当前 raw}``，不做 transform / validator。"""
        return {
            (h.ident or h.row_id): h.raw()
            for h in self._handles
            if h.is_value_row
        }

    def set_value_map(self, mapping: dict) -> None:
        """按 ident 批量写值（子表单回填 / 外部同步）。"""
        for ident, raw in (mapping or {}).items():
            handle = self.handle_of(ident)
            if handle is not None:
                handle.set_value(raw)

    # ------------------------------------------------------------------
    # 构建入口（row_builder.add_row 委托到这里）
    # ------------------------------------------------------------------
    def _construct_row(
            self,
            spec: RowSpec,
            *,
            label: Any = None,
            value: Any = None,
            ident: Optional[str] = None,
            default: Any = None,
            transform: Optional[Callable[[Any], Any]] = None,
            validator: Optional[Callable[[Any], tuple]] = None,
            validation_mode: Optional[ValidationMode] = None,
            text: Optional[str] = None,
            placeholder: Optional[str] = None,
            height: Optional[int] = None,
            minimum: Optional[float] = None,
            maximum: Optional[float] = None,
            step: Optional[float] = None,
            decimals: Optional[int] = None,
            items: Optional[list] = None,
            browse_kind: Optional[BrowseKind] = None,
            schema: Any = None,
            rows: Optional[list] = None,
            orientation: Any = None,
            wrap: Optional[bool] = None,
            label_span: int = 9,
            field_span: int = 3,
            extra: Optional[dict[str, Any]] = None,
    ) -> RowHandle:
        extra = dict(extra or {})
        control = spec.control

        options = {
            "value": value,
            "label": _label_text(label),
            "ident": ident,
            "placeholder": placeholder,
            "height": height,
            "text": text,
            "wrap": wrap,
            "items": items,
            "minimum": minimum,
            "maximum": maximum,
            "step": step,
            "decimals": decimals,
            "orientation": orientation,
            "browse_kind": browse_kind,
            "schema": schema,
            "rows": rows,
            "_subform_builder": self._build_subform_container,
        }

        # ---- 整行形态：无 label 列、无装饰 ----
        if spec.is_full_width:
            return self._construct_full_row(
                spec, options, label=label, height=height,
            )

        # ---- 形态 → ControlSpec ----
        if spec.is_custom:
            cspec = self._resolve_custom(str(control))
            options.update(extra)
        elif isinstance(control, RowControl):
            cspec = CONTROL_SPECS[control]
        else:  # pragma: no cover - RowSpec 已保证二者之一
            raise TypeError(f"control 必须是 RowControl 或 str：{control!r}")

        # ---- field ----
        field_widget = cspec.build(self._content, options)
        if height is not None and control is not RowControl.LABEL:
            field_widget.setFixedHeight(int(height))

        read_fn, write_fn = cspec.read, cspec.write
        if control is RowControl.SUBFORM:
            read_fn, write_fn = _read_subform_container, _write_subform_container

        # ---- 重置按钮（存在性由 default 隐式决定）----
        want_reset = default is not None and spec.reset_position is not None
        reset_btn = QPushButton("↺", self._content) if want_reset else None
        if reset_btn is not None:
            reset_btn.setFixedSize(20, 20)
            reset_btn.setVisible(False)

        # ---- 自然默认（仅当调用方没传 default=）----
        natural = None
        if default is None and not spec.is_custom:
            has_nat, nat = natural_default(control, options)
            if has_nat:
                natural = nat

        handle = self._assemble_row(
            spec=spec,
            cspec=cspec,
            label=label,
            options=options,
            field_widget=field_widget,
            read_fn=read_fn,
            write_fn=write_fn,
            reset_btn=reset_btn,
            ident=ident,
            value=value,
            default=default,
            natural_default=natural,
            transform=transform,
            validator=validator,
            validation_mode=validation_mode,
            label_span=label_span,
            field_span=field_span,
        )
        return handle

    # ------------------------------------------------------------------
    def _resolve_custom(self, name: str) -> ControlSpec:
        cspec = self._CUSTOM_CONTROLS.get(name.strip().lower())
        if cspec is None:
            raise ValueError(
                f"未注册的自定义控件：{name!r}；"
                f"可用：{', '.join(sorted(self._CUSTOM_CONTROLS)) or '（无）'}"
            )
        return cspec

    # ------------------------------------------------------------------
    # 整行：SEPARATOR / LABEL
    # ------------------------------------------------------------------
    def _construct_full_row(
            self,
            spec: RowSpec,
            options: dict[str, Any],
            *,
            label: Any,
            height: Optional[int],
    ) -> RowHandle:
        control = spec.control
        if control is RowControl.SEPARATOR:
            widget = SeparatorLine(self._content)
        else:  # LABEL
            content = options.get("text")
            if content is None:
                content = options.get("label") or ""
            if isinstance(label, QLabel):
                widget = label
            elif isinstance(label, QWidget):
                widget = label
                widget.setParent(self._content)
            else:
                widget = QLabel(str(content), self._content)
                widget.setAlignment(
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                )
                widget.setWordWrap(bool(options.get("wrap")))
            if height is not None:
                widget.setFixedHeight(int(height))

        grid_row = len(self._handles)
        self._grid.addWidget(widget, grid_row, 0, self.GRID_COLS)
        return self._register_handle(
            RowHandle(
                self, self._next_row_id, None,
                spec=spec,
                label_widget=widget,
                field_widget=widget,
                container=widget,
                read_fn=lambda w: None,
                write_fn=lambda w, v: None,
                label_text=_label_text(label) or options.get("text") or "",
                is_value_row=False,
            ),
            grid_row,
        )

    # ------------------------------------------------------------------
    # 行内 / CHECKBOX 装配
    # ------------------------------------------------------------------
    def _assemble_row(
            self,
            *,
            spec: RowSpec,
            cspec: ControlSpec,
            label: Any,
            options: dict[str, Any],
            field_widget: QWidget,
            read_fn: Callable[[QWidget], Any],
            write_fn: Callable[[QWidget, Any], None],
            reset_btn: Optional[QPushButton],
            ident: Optional[str],
            value: Any,
            default: Any,
            natural_default: Any,
            transform: Optional[Callable[[Any], Any]],
            validator: Optional[Callable[[Any], tuple]],
            validation_mode: Optional[ValidationMode],
            label_span: int,
            field_span: int,
    ) -> RowHandle:
        lock_icon = QLabel("🔒", self._content)
        lock_icon.setObjectName("lockIcon")
        lock_icon.setFixedSize(16, 16)
        lock_icon.setStyleSheet("color: #888; background: transparent;")
        lock_icon.setVisible(False)

        error_label = QLabel("", self._content)
        error_label.setFixedHeight(18)
        error_label.setStyleSheet("color: #d9534f; font-size: 11px;")
        error_label.setSizePolicy(QSizePolicy.Policy.Ignored,
                                  QSizePolicy.Policy.Fixed)

        is_checkbox = spec.control is RowControl.CHECKBOX
        label_text = _label_text(label) or (options.get("text") or "")

        # ---- 描述 label = 状态点载体（TagLabel）----
        # 有 label 列时它就是左侧描述 label；CHECKBOX / 自定义 label 控件时
        # 退化成控件行首的纯圆点。
        if is_checkbox or isinstance(label, QWidget):
            state_label = TagLabel("", parent=self._content)
            state_label.setSizePolicy(QSizePolicy.Policy.Maximum,
                                      QSizePolicy.Policy.Preferred)
            badge_in_label = False
        else:
            state_label = TagLabel(label_text, parent=self._content)
            state_label.setSizePolicy(QSizePolicy.Policy.Preferred,
                                      QSizePolicy.Policy.Minimum)
            badge_in_label = True

        # ---- 控件行（+ 内联重置按钮）----
        top = QWidget(self._content)
        top_layout = QHBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(4)
        if not badge_in_label:
            top_layout.addWidget(state_label, 0, Qt.AlignmentFlag.AlignVCenter)
        top_layout.addWidget(field_widget, 1)
        top_layout.addWidget(lock_icon, 0)
        if reset_btn is not None and spec.reset_position is ResetPosition.INLINE:
            top_layout.addWidget(reset_btn, 0)

        container = QWidget(self._content)
        vbox = QVBoxLayout(container)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(0)
        vbox.addWidget(top, 0)

        # ---- 下方重置按钮（右对齐）----
        if reset_btn is not None and spec.reset_position is ResetPosition.BELOW:
            btn_row = QHBoxLayout()
            btn_row.setContentsMargins(0, 0, 0, 0)
            btn_row.addStretch(1)
            btn_row.addWidget(reset_btn, 0)
            vbox.addLayout(btn_row)

        vbox.addWidget(error_label, 0)

        # ---- 栅格摆放 ----
        grid_row = len(self._handles)
        if is_checkbox:
            # 行内例外：控件在前、text 在后，无 label 列
            span = min(self.GRID_COLS, label_span + field_span)
            self._grid.addWidget(container, grid_row, 0, span)
            label_widget = None
        elif isinstance(label, QWidget):
            label_widget = label
            label_widget.setParent(self._content)
            self._grid.addWidget(label_widget, grid_row, 0, label_span)
            self._grid.addWidget(container, grid_row, label_span, field_span)
        else:
            label_widget = _centered(state_label, self._content)
            self._grid.addWidget(label_widget, grid_row, 0, label_span)
            self._grid.addWidget(container, grid_row, label_span, field_span)

        handle = RowHandle(
            self, self._next_row_id, ident,
            spec=spec,
            label_widget=label_widget,
            field_widget=field_widget,
            container=container,
            read_fn=read_fn,
            write_fn=write_fn,
            initial_raw=value,
            default_raw=default,
            natural_default=natural_default,
            transform=transform,
            validator=validator,
            validation_mode=validation_mode or ValidationMode.ON_COMMIT,
            changed_target=self._changed_target(field_widget, cspec),
            changed_signal=cspec.changed_signal,
            reset_btn=reset_btn,
            lock_icon=lock_icon,
            error_label=error_label,
            badge=state_label,
            label_text=label_text,
            is_value_row=True,
        )
        handle.bind_grid_row(grid_row)
        self.apply_access(handle, spec.access)
        self._handles.append(handle)
        handle.refresh_state()  # 初始状态点 / 按钮可用性
        return handle

    @staticmethod
    def _changed_target(field_widget: QWidget, cspec: ControlSpec) -> QObject:
        if cspec.changed_inner:
            inner = getattr(field_widget, "edit", None)
            if inner is not None:
                return inner
        return field_widget

    def _register_handle(self, handle: RowHandle, grid_row: int) -> RowHandle:
        handle.bind_grid_row(grid_row)
        self._handles.append(handle)
        self.apply_access(handle, handle.spec.access)
        return handle

    # ------------------------------------------------------------------
    # 读写状态
    # ------------------------------------------------------------------
    def apply_access(
            self,
            handle: RowHandle,
            access: RowAccess,
            reason: Optional[str] = None,
    ) -> None:
        """把 RowAccess 落到控件上。

        READONLY 优先用控件的 setReadOnly / 复合字段的只读模式，
        控件不支持时退化为“禁用但保留正常外观”。
        """
        field = handle.field
        ro_mode = getattr(field, "setReadOnlyMode", None)

        if access is RowAccess.DISABLED:
            field.setEnabled(False)
        elif access is RowAccess.READONLY:
            field.setEnabled(True)
            if callable(ro_mode):
                ro_mode(True)
            elif hasattr(field, "setReadOnly"):
                field.setReadOnly(True)
            else:
                field.setEnabled(False)   # 无只读能力的控件只能禁用
        else:
            field.setEnabled(True)
            if callable(ro_mode):
                ro_mode(False)
            elif hasattr(field, "setReadOnly"):
                field.setReadOnly(False)

        lock_icon = handle.lock_icon
        if lock_icon is not None:
            locked = access is RowAccess.DISABLED
            lock_icon.setVisible(locked)
            lock_icon.setToolTip((reason or "") if locked else "")

    # ------------------------------------------------------------------
    # 子表单
    # ------------------------------------------------------------------
    def _build_subform_container(
            self, parent: QWidget, options: dict[str, Any],
    ) -> QWidget:
        """SUBFORM 容器：有 schema → 嵌套 FormView；有 rows → 只登记。"""
        container = QWidget(parent)
        vbox = QVBoxLayout(container)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(4)

        schema = options.get("schema")
        rows = options.get("rows")
        value = options.get("value")

        if schema is not None:
            inner = FormView(container)
            inner.setVerticalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAlwaysOff
            )
            for decl in _iter_schema_rows(schema):
                inner.add_row(**decl)
            if isinstance(value, dict):
                inner.set_value_map(value)
            vbox.addWidget(inner)
            container._subform_form = inner
        else:
            label = QLabel(format_editor_text(value), container)
            label.setWordWrap(True)
            vbox.addWidget(label)
            container._subform_label = label

        if rows:
            container._subform_rows = list(rows)
        container._subform_value = value
        return container

    # ------------------------------------------------------------------
    # 删除（按句柄；row_id 稳定）
    # ------------------------------------------------------------------
    def remove_handle(self, handle: RowHandle) -> None:
        if handle not in self._handles:
            return
        row = handle.grid_row
        self._grid.remove_row(row)
        self._handles.remove(handle)
        for h in self._handles:
            if h.grid_row > row:
                h.bind_grid_row(h.grid_row - 1)
        for w in (handle.label_widget, handle.container):
            if w is not None and w is not handle.field:
                w.setParent(None)
                w.deleteLater()

    def clear(self) -> None:
        for h in list(self._handles):
            self.remove_handle(h)

    # ------------------------------------------------------------------
    # 批量提交
    # ------------------------------------------------------------------
    def collect(self) -> Optional[dict]:
        """逐行 get_value；首个非法 → 弹窗「回滚 / 重新修改」。

        回滚 → set_value(initial_value()) + 清错 + continue（submitted 走
        transform 跳过 validator）；重新修改 → focus 首个非法行并返回 None。
        全合法 → {ident: submitted}（无 ident 用 row_id 兜底）。

        每一行提交成功都会 ``mark_clean()``：基线前移到当前值，
        因此 flush 之后再编辑才会重新标脏。
        """
        submitted: dict = {}
        for handle in list(self._handles):
            if not handle.is_value_row:
                continue
            key = handle.ident or handle.row_id
            try:
                submitted[key] = handle.get_value()
            except ValidationError:
                msg = handle.error_text
                box = QMessageBox(self)
                box.setIcon(QMessageBox.Icon.Warning)
                box.setWindowTitle("校验失败")
                box.setText(f"「{handle.label_text}」{msg or '值不合法'}")
                rollback_btn = box.addButton(
                    "回滚", QMessageBox.ButtonRole.AcceptRole)
                edit_btn = box.addButton(
                    "重新修改", QMessageBox.ButtonRole.RejectRole)
                box.setDefaultButton(edit_btn)
                box.exec()
                if box.clickedButton() is rollback_btn:
                    init = handle.initial_value()
                    handle.set_value(init)
                    handle.clear_error()
                    try:
                        submitted[key] = handle.submitted_from_raw(init)
                    except ValueError as exc:
                        handle.set_error(f"初始值无法转换：{exc}")
                        submitted[key] = init
                    handle.mark_clean()   # 回滚也是一次提交
                    continue
                handle.focus()
                return None
            else:
                handle.mark_clean()        # 提交成功：基线前移
        return submitted

    def mark_clean(self) -> None:
        """把全部行的提交基线前移到当前值。

        场景：外部已把值写入配置文件（flush），但值的收集不是通过
        ``collect()`` 完成时，用它同步「已修改」判定的基准。
        """
        for h in self._handles:
            if h.is_value_row:
                h.mark_clean()

    # ------------------------------------------------------------------
    def values(self) -> dict:
        """全量 get_value（不弹窗）；第一个非法就抛 ValidationError。"""
        return {
            (h.ident or h.row_id): h.get_value()
            for h in self._handles
            if h.is_value_row
        }


# ----------------------------------------------------------------------
# SUBFORM 的值读写（放到模块级，避免与 ControlSpec 互引用）
# ----------------------------------------------------------------------
def _read_subform_container(container: QWidget) -> Any:
    inner = getattr(container, "_subform_form", None)
    if inner is not None:
        return inner.raw_values()
    return getattr(container, "_subform_value", None)


def _write_subform_container(container: QWidget, raw: Any) -> None:
    container._subform_value = raw
    inner = getattr(container, "_subform_form", None)
    if inner is not None and isinstance(raw, dict):
        inner.set_value_map(raw)
    label = getattr(container, "_subform_label", None)
    if label is not None:
        label.setText(format_editor_text(raw))


__all__ = ["FormView"]
