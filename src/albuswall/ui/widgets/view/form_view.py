# -*- coding: utf-8 -*-
"""表单视图（FormView）。

基于 12 栅格布局的表单视图，栅格划分与浏览器栅格系统一致：
**12 栅格是比例不是像素**，列宽随视口变化。

结构：
    FormView(QScrollArea)
    └─ content(QWidget) ← Grid12Layout
       ├─ label  ── 默认 0..9 栅格
       └─ container ── 默认 9..12 栅格
          ├─ top: [field][🔒][↺]
          └─ error_label（常驻占位，高 18px）

控件形态由 RowFormat 枚举决定；值语义（transform / validator / default）
是每行钩子。分派走注册表 `self._registry: dict[RowFormat, _FormatSpec]`。
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable, NamedTuple, Optional, Union

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QColorDialog,
    QDoubleSpinBox, QFileDialog, QFrame, QHBoxLayout, QKeySequenceEdit,
    QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton,
    QRadioButton, QScrollArea, QSizePolicy, QSlider, QSpinBox,
    QVBoxLayout, QWidget,
)

from ..layout.grid12_layout import Grid12Layout
from ..enum.row_format import RowFormat
from ..widget.row_handle import RowHandle, ValidationError, format_editor_text


# ======================================================================
# 辅助控件
# ======================================================================
class _SeparatorLine(QFrame):
    """固定高度、水平拉伸的分割线。"""

    def __init__(self, parent=None, thickness: int = 1, color: str = "#d0d0d0"):
        super().__init__(parent)
        self._thickness = max(1, int(thickness))
        self.setFixedHeight(self._thickness)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setStyleSheet(
            f"QFrame {{ background-color: {color}; border: none; }}"
        )

    def sizeHint(self) -> QSize:
        return QSize(0, self._thickness)

    def minimumSizeHint(self) -> QSize:
        return QSize(0, self._thickness)


# noinspection pep8-naming
class _ColorField(QWidget):
    """颜色字段：输入框 + 预览色块 + 拾色按钮。

    窄列修复：给 composite 一个最小宽度，内部 edit 也有最小宽度，
    按钮保持 28px，不会被压成 0。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(96)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self.edit = QLineEdit(self)
        self.edit.setMinimumWidth(40)
        self.preview = QFrame(self)
        self.preview.setFixedSize(24, 24)
        self.button = QPushButton("…", self)
        self.button.setFixedWidth(28)

        layout.addWidget(self.edit, 1)
        layout.addWidget(self.preview, 0)
        layout.addWidget(self.button, 0)

        self.edit.textChanged.connect(self._refresh_preview)
        self.button.clicked.connect(self._pick_color)
        self._refresh_preview("")

    def _refresh_preview(self, text: str) -> None:
        text = text.strip()
        color = QColor.fromString(text) if text else QColor()
        if color.isValid():
            hex_argb = color.name(QColor.NameFormat.HexArgb)
            self.preview.setStyleSheet(
                f"QFrame {{ background-color: {hex_argb}; "
                f"border: 1px solid #888; }}"
            )
        else:
            self.preview.setStyleSheet(
                "QFrame { background-color: transparent; "
                "border: 1px dashed #d00; }"
            )

    def _pick_color(self) -> None:
        text = self.edit.text().strip()
        current = QColor.fromString(text) if text else QColor()
        if not current.isValid():
            current = QColor("#ffffff")
        picked = QColorDialog.getColor(
            current, self, "选择颜色",
            QColorDialog.ColorDialogOption.ShowAlphaChannel,
        )
        if picked.isValid():
            if picked.alpha() < 255:
                self.edit.setText(
                    f"rgba({picked.red()}, {picked.green()}, {picked.blue()}, "
                    f"{picked.alpha() / 255:.3f})"
                )
            else:
                self.edit.setText(picked.name().upper())

    def text(self) -> str:
        return self.edit.text()

    def setText(self, text: str) -> None:
        self.edit.setText(text)


# noinspection pep8-naming
class _PathField(QWidget):
    """路径字段：可编辑输入框 + 浏览按钮。

    kind: "file" | "directory" | "auto"
    auto 在浏览时按当前文本运行时判定：已是目录 → 选目录，否则选文件。
    """

    def __init__(self, parent=None, kind: str = "file"):
        super().__init__(parent)
        self._kind = kind
        self.setMinimumWidth(72)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self.edit = QLineEdit(self)
        self.edit.setMinimumWidth(40)
        self.button = QPushButton("…", self)
        self.button.setFixedWidth(28)
        layout.addWidget(self.edit, 1)
        layout.addWidget(self.button, 0)
        self.button.clicked.connect(self._browse)

    def _browse(self) -> None:
        start = self.edit.text().strip()
        if self._kind == "directory":
            path = QFileDialog.getExistingDirectory(self, "选择目录", start)
        elif self._kind == "auto":
            if start and os.path.isdir(start):
                path = QFileDialog.getExistingDirectory(self, "选择目录", start)
            else:
                path, _ = QFileDialog.getOpenFileName(self, "选择文件", start)
        else:
            path, _ = QFileDialog.getOpenFileName(self, "选择文件", start)
        if path:
            self.edit.setText(path)

    def text(self) -> str:
        return self.edit.text()

    def setText(self, text: str) -> None:
        self.edit.setText(text)


# ======================================================================
# 注册表数据结构
# ======================================================================
class _FormatSpec(NamedTuple):
    build: Callable[[Any, dict], QWidget]
    read: Callable[[QWidget], Any]
    write: Callable[[QWidget, Any], None]


# ----------------------------------------------------------------------
# 各形态的 read / write（模块级）
# ----------------------------------------------------------------------
def _read_lineedit(w): return w.text()


def _write_lineedit(w, raw):
    w.setText("" if raw is None else str(raw))


def _read_number(w): return w.value()


def _write_number(w, raw):
    if raw is None:
        return
    try:
        w.setValue(raw)
    except (TypeError, ValueError):
        pass


def _read_multiline(w): return w.toPlainText()


def _write_multiline(w, raw):
    w.setPlainText("" if raw is None else str(raw))


def _read_enum(w): return w.currentData()


def _write_enum(w, raw):
    idx = w.findData(raw)
    if idx < 0:
        w.addItem(f"⚠ {raw}", raw)
        idx = w.count() - 1
    w.setCurrentIndex(idx)


def _read_checkbox(w): return w.isChecked()


def _write_checkbox(w, raw):
    w.setChecked(bool(raw))


def _read_radio(w):
    for rb, val in getattr(w, "_radio_buttons", []):
        if rb.isChecked():
            return val
    return None


def _write_radio(w, raw):
    for rb, val in getattr(w, "_radio_buttons", []):
        rb.setChecked(val == raw)


def _read_keyseq(w): return w.keySequence().toString()


def _write_keyseq(w, raw):
    w.setKeySequence(QKeySequence("" if raw is None else str(raw)))


def _read_slider(w): return w.value()


def _write_slider(w, raw):
    if raw is None:
        return
    try:
        w.setValue(int(round(float(raw))))
    except (TypeError, ValueError):
        pass


def _read_textfield(w): return w.text()


def _write_textfield(w, raw):
    w.setText("" if raw is None else str(raw))


def _read_subform(w): return getattr(w, "_subform_value", None)


def _write_subform(w, raw):
    w._subform_value = raw
    lbl = getattr(w, "_subform_label", None)
    if lbl is not None:
        lbl.setText(format_editor_text(raw))


# ======================================================================
# 表单视图
# ======================================================================
class FormView(QScrollArea):
    """12 栅格表单视图（可滚动）。"""

    GRID_COLS = 12

    FORMAT_ALIASES: dict[str, RowFormat] = {
        member.name.lower(): member for member in RowFormat
    }

    # ------------------------------------------------------------------
    # 格式注册
    # ------------------------------------------------------------------
    @classmethod
    def register_format(cls, name: str, row_format: RowFormat) -> None:
        if not isinstance(row_format, RowFormat):
            raise TypeError("row_format 必须是 RowFormat 枚举")
        key = str(name).strip().lower()
        if not key:
            raise ValueError("别名不能为空字符串")
        cls.FORMAT_ALIASES[key] = row_format

    @classmethod
    def supported_formats(cls) -> list[str]:
        return sorted(cls.FORMAT_ALIASES)

    @classmethod
    def _resolve_format(cls, row_format) -> RowFormat:
        if isinstance(row_format, RowFormat):
            return row_format
        if isinstance(row_format, str):
            try:
                return cls.FORMAT_ALIASES[row_format.strip().lower()]
            except KeyError:
                raise ValueError(
                    f"未知的行格式：{row_format!r}；"
                    f"可用：{', '.join(cls.supported_formats())}"
                )
        raise TypeError(
            f"row_format 必须是 RowFormat 枚举或字符串，"
            f"实际为 {type(row_format).__name__}"
        )

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
        self._registry: dict[RowFormat, _FormatSpec] = self._build_registry()

    # ------------------------------------------------------------------
    # 注册表
    # ------------------------------------------------------------------
    def _build_registry(self) -> dict[RowFormat, _FormatSpec]:
        return {
            RowFormat.KV: _FormatSpec(self._build_kv,
                                      _read_lineedit, _write_lineedit),
            RowFormat.NUMBER: _FormatSpec(self._build_number,
                                          _read_number, _write_number),
            RowFormat.MULTILINE: _FormatSpec(self._build_multiline,
                                             _read_multiline, _write_multiline),
            RowFormat.ENUM: _FormatSpec(self._build_enum,
                                        _read_enum, _write_enum),
            RowFormat.CHECKBOX: _FormatSpec(self._build_checkbox,
                                            _read_checkbox, _write_checkbox),
            RowFormat.RADIO: _FormatSpec(self._build_radio,
                                         _read_radio, _write_radio),
            RowFormat.COLOR: _FormatSpec(self._build_color,
                                         _read_textfield, _write_textfield),
            RowFormat.KEYSEQUENCE: _FormatSpec(self._build_keysequence,
                                               _read_keyseq, _write_keyseq),
            RowFormat.PASSWORD: _FormatSpec(self._build_password,
                                            _read_lineedit, _write_lineedit),
            RowFormat.SLIDER: _FormatSpec(self._build_slider,
                                          _read_slider, _write_slider),
            RowFormat.FILE: _FormatSpec(self._build_file,
                                        _read_textfield, _write_textfield),
            RowFormat.DIRECTORY: _FormatSpec(self._build_directory,
                                             _read_textfield, _write_textfield),
            RowFormat.READONLY: _FormatSpec(self._build_readonly,
                                            _read_lineedit, _write_lineedit),
            RowFormat.SUBFORM: _FormatSpec(self._build_subform,
                                           _read_subform, _write_subform),
        }

    # ------------------------------------------------------------------
    # 行信息
    # ------------------------------------------------------------------
    @property
    def row_count(self) -> int:
        return len(self._handles)

    def rows(self) -> list[dict]:
        """按显示顺序返回行描述，含稳定 ``row_id``。"""
        return [
            {
                "row_id": h.row_id,
                "row": h.grid_row,
                "key": h.key,
                "label": h._label_widget,
                "field": h._field,
                "format": h._fmt,
                "handle": h,
            }
            for h in self._handles
        ]

    # ------------------------------------------------------------------
    # 添加
    # ------------------------------------------------------------------
    def add_row(
            self,
            label=None,
            value=None,
            row_format: Union[RowFormat, str] = RowFormat.KV,
            *,
            key: Optional[str] = None,
            default=None,
            transform: Optional[Callable[[Any], Any]] = None,
            validator: Optional[Callable[[Any], tuple]] = None,
            browse_kind: Optional[str] = None,
            display_hint: Optional[str] = None,
            text: Optional[str] = None,
            items: Optional[list] = None,
            current_value: Any = None,
            orientation=None,
            minimum=None,
            maximum=None,
            step=None,
            decimals=None,
            placeholder: Optional[str] = None,
            height: Optional[int] = None,
            label_span: int = 9,
            field_span: int = 3,
            live_validate: bool = False,  # noqa: ARG002  留位，不实现
            **extra,
    ) -> RowHandle:
        fmt = self._resolve_format(row_format)
        label_text = "" if label is None else (
            label.text() if isinstance(label, QLabel) else str(label)
        )

        # ---- 整行形态 ----
        if fmt is RowFormat.SEPARATOR:
            # Bug 2 修复：height 是形参不进 extra，必须显式拒绝
            if height is not None:
                raise TypeError(
                    "SEPARATOR 不支持 height 参数（请用 thickness 控制粗细）"
                )
            return self._add_separator(**extra)
        if fmt is RowFormat.LABEL:
            # Bug 2 修复：显式把 height 透传给 _add_label
            return self._add_label(label, height=height, **extra)
        if extra:
            raise TypeError(f"未知参数：{', '.join(sorted(extra))}")

        if label_span < 1 or field_span < 1:
            raise ValueError("label_span 与 field_span 都必须 >= 1")
        if label_span + field_span > self.GRID_COLS:
            raise ValueError("label_span + field_span 超出 12 栅格")

        options = {
            "browse_kind": browse_kind,
            "display_hint": display_hint,
            "text": text,
            "items": items,
            "current_value": current_value,
            "orientation": orientation,
            "minimum": minimum,
            "maximum": maximum,
            "step": step,
            "decimals": decimals,
            "placeholder": placeholder,
            "height": height,
        }

        # ---- label ----
        if isinstance(label, QWidget):
            label_widget = label
        else:
            label_widget = QLabel(label_text, self._content)
            label_widget.setAlignment(
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
            )

        # ---- field（走注册表） ----
        spec = self._registry[fmt]
        field_widget = spec.build(value, options)

        if height is not None:
            field_widget.setFixedHeight(int(height))

        return self._finalize_row(
            fmt=fmt,
            label_widget=label_widget,
            field_widget=field_widget,
            read_fn=spec.read,
            write_fn=spec.write,
            label_span=label_span,
            field_span=field_span,
            label_text=label_text,
            value=value,
            default=default,
            transform=transform,
            validator=validator,
            key=key,
            is_value_row=True,
        )

    # ------------------------------------------------------------------
    # 整行：分割线 / 纯文本
    # ------------------------------------------------------------------
    def _add_separator(self, **extra) -> RowHandle:
        # 双层防御：直接调用 _add_separator(height=...) 也拒绝
        if "height" in extra:
            raise TypeError(
                "SEPARATOR 不支持 height 参数（请用 thickness 控制粗细）"
            )
        thickness = int(extra.pop("thickness", 1))
        color = str(extra.pop("color", "#d0d0d0"))
        if extra:
            raise TypeError(f"分割线不支持的参数：{', '.join(sorted(extra))}")
        sep = _SeparatorLine(self._content, thickness=thickness, color=color)
        grid_row = len(self._handles)
        self._grid.addWidget(sep, grid_row, 0, self.GRID_COLS)
        return self._make_fullrow_handle(sep, grid_row, RowFormat.SEPARATOR)

    def _add_label(self, label=None, **extra) -> RowHandle:
        height = extra.pop("height", None)
        align = extra.pop(
            "align",
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
        )
        word_wrap = bool(extra.pop("word_wrap", False))
        if extra:
            raise TypeError(f"label 不支持的参数：{', '.join(sorted(extra))}")

        if isinstance(label, QWidget):
            widget = label
        else:
            widget = QLabel("" if label is None else str(label), self._content)
            widget.setAlignment(align)
            widget.setWordWrap(word_wrap)
        if height is not None:
            widget.setFixedHeight(int(height))

        grid_row = len(self._handles)
        self._grid.addWidget(widget, grid_row, 0, self.GRID_COLS)
        return self._make_fullrow_handle(widget, grid_row, RowFormat.LABEL)

    def _make_fullrow_handle(self, widget, grid_row, fmt) -> RowHandle:
        row_id = self._next_row_id
        self._next_row_id += 1
        handle = RowHandle(
            form=self, row_id=row_id, key=None,
            label_widget=widget, field_widget=widget, container=widget,
            lock_icon=None, restore_btn=None, error_label=None,
            read_fn=lambda w: None, write_fn=lambda w, v: None,
            initial_raw=None, default_raw=None,
            transform=None, validator=None, fmt=fmt,
            label_text="", is_value_row=False,
        )
        handle._grid_row = grid_row
        self._handles.append(handle)
        return handle

    # ------------------------------------------------------------------
    # 复合行容器：field + extras + error 占位
    # ------------------------------------------------------------------
    def _finalize_row(
            self,
            *,
            fmt,
            label_widget,
            field_widget,
            read_fn,
            write_fn,
            label_span,
            field_span,
            label_text,
            value,
            default,
            transform,
            validator,
            key,
            is_value_row,
    ) -> RowHandle:
        lock_icon = QLabel("🔒", self._content)
        lock_icon.setFixedSize(16, 16)
        lock_icon.setStyleSheet("color: #888; background: transparent;")
        lock_icon.setVisible(False)

        restore_btn = QPushButton("↺", self._content)
        restore_btn.setFixedSize(20, 20)
        restore_btn.setVisible(False)

        error_label = QLabel("", self._content)
        error_label.setFixedHeight(18)
        error_label.setStyleSheet("color: #d9534f; font-size: 11px;")
        error_label.setSizePolicy(QSizePolicy.Policy.Ignored,
                                  QSizePolicy.Policy.Fixed)

        top = QWidget(self._content)
        top_layout = QHBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(4)
        top_layout.addWidget(field_widget, 1)
        top_layout.addWidget(lock_icon, 0)
        top_layout.addWidget(restore_btn, 0)

        container = QWidget(self._content)
        vbox = QVBoxLayout(container)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(0)
        vbox.addWidget(top, 0)
        vbox.addWidget(error_label, 0)

        grid_row = len(self._handles)
        self._grid.addWidget(label_widget, grid_row, 0, label_span)
        self._grid.addWidget(container, grid_row, label_span, field_span)

        row_id = self._next_row_id
        self._next_row_id += 1
        handle = RowHandle(
            form=self, row_id=row_id, key=key,
            label_widget=label_widget, field_widget=field_widget,
            container=container,
            lock_icon=lock_icon, restore_btn=restore_btn,
            error_label=error_label,
            read_fn=read_fn, write_fn=write_fn,
            initial_raw=value, default_raw=default,
            transform=transform, validator=validator,
            fmt=fmt, label_text=label_text,
            is_value_row=is_value_row,
        )
        handle._grid_row = grid_row
        self._handles.append(handle)
        return handle

    # ------------------------------------------------------------------
    # 删除（按句柄；row_id 稳定）
    # ------------------------------------------------------------------
    def _remove_handle(self, handle: RowHandle) -> None:
        if handle not in self._handles:
            return
        row = handle._grid_row
        self._grid.remove_row(row)
        self._handles.remove(handle)
        for h in self._handles:
            if h._grid_row > row:
                h._grid_row -= 1
        # 销毁 widget
        for w in (handle._label_widget, handle._container):
            if w is not None:
                w.setParent(None)
                w.deleteLater()

    def clear(self) -> None:
        for h in list(self._handles):
            self._remove_handle(h)

    # ------------------------------------------------------------------
    # 批量提交
    # ------------------------------------------------------------------
    def collect(self) -> Optional[dict]:
        """逐行 get_value；首个非法 → 弹窗「回滚 / 重新修改」。

        回滚 → set_value(initial_value()) + 清错 + continue（submitted 走
        transform 跳过 validator）；重新修改 → focus 首个非法行并返回 None。
        全合法 → {key: submitted}（无 key 用 row_id 兜底）。
        """
        submitted: dict = {}
        for handle in list(self._handles):
            if not handle.is_value_row:
                continue
            try:
                submitted[handle.key or handle.row_id] = handle.get_value()
            except ValidationError:
                msg = handle._error_label.text() if handle._error_label else ""
                box = QMessageBox(self)
                box.setIcon(QMessageBox.Icon.Warning)
                box.setWindowTitle("校验失败")
                box.setText(f"「{handle._label_text}」{msg or '值不合法'}")
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
                    # Bug 1 修复：submitted 走 transform（跳过 validator），
                    # 与"初始值可信"契约一致。
                    try:
                        submitted[handle.key or handle.row_id] = (
                            handle.submitted_from_raw(init)
                        )
                    except ValueError as exc:
                        # transform 对 init 也失败：按开发者可信原则不阻塞，
                        # 记 raw 并标红提示。
                        handle.set_error(f"初始值无法转换：{exc}")
                        submitted[handle.key or handle.row_id] = init
                    continue
                handle.focus()
                return None
        return submitted

    # ------------------------------------------------------------------
    # 各形态 builder
    # ------------------------------------------------------------------
    def _build_kv(self, value, options) -> QLineEdit:
        edit = QLineEdit(self._content)
        edit.setText(format_editor_text(value, options.get("display_hint")))
        ph = options.get("placeholder")
        if ph:
            edit.setPlaceholderText(str(ph))
        return edit

    def _build_number(self, value, options):
        decimals = int(options.get("decimals") or 0)
        if decimals > 0:
            spin = QDoubleSpinBox(self._content)
            spin.setDecimals(decimals)
        else:
            spin = QSpinBox(self._content)
        if options.get("minimum") is not None:
            spin.setMinimum(options["minimum"])
        if options.get("maximum") is not None:
            spin.setMaximum(options["maximum"])
        if options.get("step") is not None:
            spin.setSingleStep(options["step"])
        if value is not None:
            try:
                spin.setValue(
                    float(value) if isinstance(spin, QDoubleSpinBox)
                    else int(float(value))
                )
            except (TypeError, ValueError):
                pass
        return spin

    def _build_multiline(self, value, options) -> QPlainTextEdit:
        edit = QPlainTextEdit(self._content)
        edit.setFixedHeight(100)
        edit.setPlainText(format_editor_text(value, options.get("display_hint")))
        ph = options.get("placeholder")
        if ph:
            edit.setPlaceholderText(str(ph))
        return edit

    def _build_enum(self, value, options) -> QComboBox:
        combo = QComboBox(self._content)
        items = options.get("items")
        current = options.get("current_value")
        if items is None:
            if isinstance(value, (list, tuple)):
                items = list(value)
                if current is None and items:
                    current = items[0]
            else:
                items = []
        for item in items:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                txt, val = item
            else:
                txt, val = str(item), item
            combo.addItem(str(txt), val)
        if current is None:
            current = value
        if current is not None:
            idx = combo.findData(current)
            if idx < 0:
                combo.addItem(f"⚠ {current}", current)
                idx = combo.count() - 1
            combo.setCurrentIndex(idx)
        return combo

    def _build_checkbox(self, value, options) -> QCheckBox:
        cb = QCheckBox(self._content)
        txt = options.get("text")
        if txt is not None:
            cb.setText(str(txt))
        cb.setChecked(bool(value))
        return cb

    def _build_radio(self, value, options) -> QWidget:
        orientation = options.get("orientation") or Qt.Orientation.Horizontal
        w = QWidget(self._content)
        lay = (QHBoxLayout(w) if orientation == Qt.Orientation.Horizontal
               else QVBoxLayout(w))
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        group = QButtonGroup(w)
        items = options.get("items") or []
        current = options.get("current_value", value)
        buttons: list[tuple[QRadioButton, Any]] = []
        for item in items:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                txt, val = item
            else:
                txt, val = str(item), item
            rb = QRadioButton(str(txt), w)
            group.addButton(rb)
            lay.addWidget(rb)
            buttons.append((rb, val))
        w._radio_buttons = buttons
        for rb, val in buttons:
            if val == current:
                rb.setChecked(True)
                break
        return w

    def _build_color(self, value, options) -> _ColorField:
        field = _ColorField(self._content)
        ph = options.get("placeholder")
        if ph:
            field.edit.setPlaceholderText(str(ph))
        field.setText(format_editor_text(value))
        return field

    def _build_keysequence(self, value, options) -> QKeySequenceEdit:
        edit = QKeySequenceEdit(self._content)
        if value is not None:
            edit.setKeySequence(
                value if isinstance(value, QKeySequence) else QKeySequence(str(value))
            )
        return edit

    def _build_password(self, value, options) -> QLineEdit:
        edit = QLineEdit(self._content)
        edit.setEchoMode(QLineEdit.EchoMode.Password)
        if value is not None:
            edit.setText(str(value))
        ph = options.get("placeholder")
        if ph:
            edit.setPlaceholderText(str(ph))
        return edit

    def _build_slider(self, value, options) -> QSlider:
        minimum = int(round(float(options.get("minimum") or 0)))
        maximum = int(round(float(options.get("maximum") or 100)))
        step = int(round(float(options.get("step") or 1)))
        slider = QSlider(Qt.Orientation.Horizontal, self._content)
        slider.setMinimum(minimum)
        slider.setMaximum(maximum)
        slider.setSingleStep(step)
        slider.setPageStep(max(step, (maximum - minimum) // 10 or step))
        if value is not None:
            try:
                slider.setValue(int(round(float(value))))
            except (TypeError, ValueError):
                pass
        return slider

    def _build_file(self, value, options) -> _PathField:
        return self._build_path(value, options, "file")

    def _build_directory(self, value, options) -> _PathField:
        return self._build_path(value, options, "directory")

    def _build_path(self, value, options, default_kind: str) -> _PathField:
        kind = options.get("browse_kind") or default_kind
        field = _PathField(self._content, kind=kind)
        if value is not None:
            field.setText(str(value))
        ph = options.get("placeholder")
        if ph:
            field.edit.setPlaceholderText(str(ph))
        return field

    def _build_readonly(self, value, options) -> QLineEdit:
        edit = QLineEdit(self._content)
        edit.setReadOnly(True)
        if value is not None:
            edit.setText(str(value))
        edit.setStyleSheet(
            "QLineEdit { background-color: #f5f5f5; color: #333; "
            "border: 1px solid #ddd; border-radius: 3px; padding: 2px 4px; }"
        )
        return edit

    def _build_subform(self, value, options) -> QWidget:
        """极简子表单：只保存 raw dict/list 并显示摘要。

        递归构建 / 双向同步按决策挂起，不实现。
        """
        w = QWidget(self._content)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lbl = QLabel(format_editor_text(value), w)
        lbl.setWordWrap(True)
        lay.addWidget(lbl)
        w._subform_label = lbl
        w._subform_value = value
        return w


# ======================================================================
# 演示（愿望代码 + 两处回归断言）
# ======================================================================
if __name__ == "__main__":
    import sys
    import json
    import fnmatch
    from PySide6.QtWidgets import (
        QApplication, QLabel, QPushButton, QVBoxLayout, QWidget,
    )
    from PySide6.QtCore import Qt


    # ---------------- 外部依赖桩：路径解析器 ----------------
    class _Resolver:
        def resolve(self, raw):
            if isinstance(raw, str) and raw.startswith("${path:icons}"):
                return "/abs/path" + raw[len("${path:icons}"):]
            return raw


    resolver = _Resolver()

    app = QApplication(sys.argv)
    window = QWidget()
    window.setWindowTitle("FormView —— 全功能示例")
    window.resize(900, 980)

    outer = QVBoxLayout(window)
    outer.setContentsMargins(20, 20, 20, 20)

    form = FormView(window)


    # 小工具：插入整行说明
    def section(title: str) -> None:
        form.add_row(f"— {title} —", row_format="label",
                     align=Qt.AlignmentFlag.AlignCenter, height=24)


    # =================================================================
    # 1) KV：无 value / 无 default / 有 default 三种
    # =================================================================
    section("KV 文本")
    h_kv_empty = form.add_row("备注（无 value 无 default）", key="kv_empty")
    h_kv_value = form.add_row("姓名（有 value 无 default）", "张三", key="name")
    h_kv_default = form.add_row("昵称（有 value 有 default）", "小张",
                                key="nick", default="匿名")
    # h_kv_default 的 extras 槽位里出现 ↺，点击恢复为 "匿名"
    assert h_kv_default.initial_value() == "小张"
    assert h_kv_default.default_value() == "匿名"
    assert h_kv_empty.default_value() is None  # 无 default → restore_default no-op

    # =================================================================
    # 2) NUMBER：int / float / 无 default / 有 default
    # =================================================================
    section("NUMBER 数值")
    h_age = form.add_row("年龄（int，无 default）", 25, row_format="number",
                         key="age", minimum=0, maximum=150)
    h_weight = form.add_row("体重（float，有 default）", 60.5, row_format="number",
                            key="weight", decimals=2, minimum=0.0,
                            maximum=500.0, step=0.1, default=50.0)

    # =================================================================
    # 3) MULTILINE：普通 / JSON 语义走 transform
    # =================================================================
    section("MULTILINE 多行")
    h_note = form.add_row("备注（普通）", "第一行\n第二行", row_format="multiline",
                          key="note")
    h_specs = form.add_row("规格（JSON 语义 + transform=json.loads）",
                           {"small": 128}, row_format="multiline",
                           key="specs", display_hint="json",
                           transform=json.loads)

    # =================================================================
    # 4) ENUM：显示 label 提交 value / 当前值不在选项里追加 ⚠
    # =================================================================
    section("ENUM 下拉")
    h_city = form.add_row("城市（当前值在选项内）", "sh", row_format="enum",
                          key="city",
                          items=[("北京", "bj"), ("上海", "sh")],
                          current_value="sh")
    h_city_bad = form.add_row("城市（当前值不在选项里 → ⚠ 条目）", "gz",
                              row_format="enum", key="city_bad",
                              items=[("北京", "bj"), ("上海", "sh")],
                              current_value="gz")
    h_gender = form.add_row("性别（有 default）", "f", row_format="enum",
                            key="gender",
                            items=[("男", "m"), ("女", "f")],
                            current_value="f", default="m")

    # =================================================================
    # 5) CHECKBOX：text 是复选框旁文案
    # =================================================================
    section("CHECKBOX 复选框")
    h_vip = form.add_row("会员", True, row_format="checkbox", key="vip",
                         text="启用会员")
    h_notify = form.add_row("通知（无 value 无 default）", None,
                            row_format="checkbox", key="notify",
                            text="接收邮件通知")

    # =================================================================
    # 6) RADIO：items=[(text, value)]
    # =================================================================
    section("RADIO 互斥")
    h_theme = form.add_row("主题", "dark", row_format="radio", key="theme",
                           items=[("浅色", "light"), ("深色", "dark")],
                           current_value="dark")
    h_align = form.add_row("对齐（竖排 + 有 default）", "left",
                           row_format="radio", key="align",
                           items=[("左", "left"), ("中", "center"), ("右", "right")],
                           current_value="left", default="center",
                           orientation=Qt.Orientation.Vertical)

    # =================================================================
    # 7) COLOR / KEYSEQUENCE / PASSWORD / SLIDER / READONLY
    # =================================================================
    section("其它输入形态")
    h_color = form.add_row("主题色", "#3366FF", row_format="color", key="color")
    h_hotkey = form.add_row("快捷键", "Ctrl+S", row_format="keysequence",
                            key="hotkey")
    h_pwd = form.add_row("密码", "", row_format="password", key="pwd",
                         placeholder="请输入密码")
    h_volume = form.add_row("音量", 30, row_format="slider", key="volume",
                            minimum=0, maximum=100, step=5)
    h_uid = form.add_row("ID（只读）", "0x9F3A", row_format="readonly",
                         key="uid")

    # =================================================================
    # 8) FILE / DIRECTORY / SUBFORM
    # =================================================================
    section("FILE / DIRECTORY / SUBFORM")
    h_icon = form.add_row("图标（browse_kind=auto + transform）",
                          "${path:icons}/ui.svg", row_format="file",
                          key="icon", browse_kind="auto",
                          transform=lambda raw: resolver.resolve(raw))
    h_logo = form.add_row("Logo（validator 走提交值）", "logo.svg",
                          row_format="file", key="logo",
                          validator=lambda v: (fnmatch.fnmatch(v, "*.svg"),
                                               "必须是 .svg 文件"))
    h_dir = form.add_row("日志目录（DIRECTORY）", "/var/log",
                         row_format="directory", key="logdir")
    h_thumb = form.add_row("缩略图（SUBFORM）",
                           {"small": 128, "large": 512},
                           row_format="subform", key="thumb")

    # 初始值跳过校验：这里故意放一个 .txt（非法），add_row 不报错
    h_bad_init = form.add_row("非法初始值（初始不校验，collect 才暴露）",
                              "not-a-svg.txt", row_format="file",
                              key="bad_init",
                              validator=lambda v: (fnmatch.fnmatch(v, "*.svg"),
                                                   "必须是 .svg 文件"))
    # 已确认：add_row 时不会触发 validator
    h_bad_init.clear_error()

    # =================================================================
    # 9) extras：禁用 + 锁图标 + tooltip / 错误态 / 清错
    # =================================================================
    section("extras：禁用 / 错误态")
    h_specs.set_enabled(False, reason="当前模式不支持编辑规格")
    # 右侧出现 🔒，tooltip = reason；field 置灰

    h_kv_value.set_error("姓名不能为空")  # 红框 + 错误占位显示
    h_kv_value.clear_error()  # 清掉

    # =================================================================
    # 10) 历史栈：set_value / undo / can_undo / history / restore_default
    # =================================================================
    section("历史栈 / 默认值恢复")
    h_age.set_value(30)
    h_age.set_value(35)
    assert h_age.get_value() == 35
    assert h_age.can_undo() is True
    h_age.undo()
    assert h_age.get_value() == 30, "undo 应回到 30"
    assert h_age.history()[-1] == 30, "history[-1] 即当前 raw"

    # 有 default 的恢复：h_weight.default=50.0
    h_weight.set_value(88.8)
    h_weight.restore_default()
    assert h_weight.get_value() == 50.0, "restore_default 应回到 50.0"

    # 无 default 的恢复：no-op
    before = h_age.get_value()
    h_age.restore_default()
    assert h_age.get_value() == before, "无 default 时 restore_default 应为 no-op"

    # 清空历史
    h_age.clear_history()
    assert h_age.can_undo() is False

    # can_redo 恒 False（留位）
    assert h_age.can_redo() is False

    # =================================================================
    # 11) 删中间行：其他句柄仍有效
    # =================================================================
    section("删除与句柄稳定性")
    h_city.remove()  # 删中间行
    assert h_theme.get_value() == "dark", "删行后 h_theme 仍指向正确行"
    assert h_kv_value.get_value() == "张三"

    # =================================================================
    # 12) 整行：LABEL（align / height）/ SEPARATOR（thickness / color）
    # =================================================================
    form.add_row("—— 以上为已展示形态 ——", row_format="label",
                 align=Qt.AlignmentFlag.AlignCenter, height=28)
    form.add_row(row_format="separator", thickness=1, color="#d0d0d0")

    # SEPARATOR 不接受 height —— 显式拒绝
    try:
        form.add_row(row_format="separator", height=28)
    except TypeError:
        pass
    else:
        raise AssertionError("SEPARATOR 应拒绝 height")

    # =================================================================
    # 13) 提交：collect() 按钮
    # =================================================================
    outer.addWidget(form)


    def do_collect():
        result = form.collect()
        if result is None:
            print("[collect] 用户选择「重新修改」，已 focus 首个非法行并中止")
            return
        printable = {k: (v if not isinstance(v, dict) else dict(v))
                     for k, v in result.items()}
        print("[collect] 全合法，提交值：")
        print(json.dumps(printable, ensure_ascii=False,
                         default=str, indent=2))


    submit_btn = QPushButton("提交（form.collect()）")
    submit_btn.clicked.connect(do_collect)
    outer.addWidget(submit_btn)

    hint = QLabel("提示：h_bad_init 的初始值是非法 .txt，"
                  "点提交会触发 collect 首个非法弹窗。")
    hint.setWordWrap(True)
    outer.addWidget(hint)
    outer.addStretch(1)

    window.show()
    sys.exit(app.exec())
