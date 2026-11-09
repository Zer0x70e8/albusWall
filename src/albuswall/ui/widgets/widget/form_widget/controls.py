# -*- coding: utf-8 -*-
"""RowControl → 控件 的构建 / 读 / 写注册表。

单一职责：只回答「这个形态用什么 widget、怎么把值读出来、怎么写回去」。
布局、装饰（重置按钮 / 状态点 / 错误行）、读写状态都由 form_view 决定。

``ControlSpec.changed_signal`` 描述「值变化」信号名，供 ValidationMode.ON_CHANGE
与状态点刷新使用；为 None 表示需要逐个子控件绑定（RADIO）或无需绑定（SUBFORM）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Optional, Union

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QDoubleSpinBox, QKeySequenceEdit,
    QLineEdit, QPlainTextEdit, QRadioButton, QSlider, QSpinBox, QVBoxLayout,
    QHBoxLayout, QWidget,
)

from .fields import ColorField, PathField
from .row_format import BrowseKind, RowControl


# ----------------------------------------------------------------------
# 值 ↔ 编辑器文本
# ----------------------------------------------------------------------
def format_editor_text(value: Any) -> str:
    """raw 值 → 编辑器文本（只用于初始展示）。

    - None → ""
    - str  → 原样（不加引号）
    - 其它 → json.dumps(ensure_ascii=False, indent=2, sort_keys=True)
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def normalize_items(items: Any) -> list[tuple[str, Any]]:
    """items 统一成 ``[(text, value), ...]``。

    接受 ``["a", "b"]``、``[("北京", "bj")]``、``[(图标, "文本", value)]`` 之外的
    任意二元序列；非二元项视为 (str(item), item)。
    """
    out: list[tuple[str, Any]] = []
    for item in items or []:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            text, val = item
        else:
            text, val = item, item
        out.append((str(text), val))
    return out


# ----------------------------------------------------------------------
# 控件规格
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class ControlSpec:
    """一个 RowControl 的构建 / 读写三件套。

    options 的键见 :meth:`FormView._construct_row` 里构造的那张表；
    自定义控件会额外收到调用方透传的 ``**extra``。
    """

    build: Callable[[QWidget, dict[str, Any]], QWidget]
    read: Callable[[QWidget], Any]
    write: Callable[[QWidget, Any], None]
    changed_signal: Optional[str] = None
    changed_inner: bool = False


# ---- 通用读写 ----
def _read_text(w: Any) -> Any:
    return w.text()


def _write_text(w: Any, raw: Any) -> None:
    w.setText("" if raw is None else str(raw))


def _read_plain(w: Any) -> Any:
    return w.toPlainText()


def _write_plain(w: Any, raw: Any) -> None:
    w.setPlainText("" if raw is None else str(raw))


def _read_number(w: Any) -> Any:
    return w.value()


def _write_number(w: Any, raw: Any) -> None:
    if raw is None:
        return
    try:
        w.setValue(float(raw) if isinstance(w, QDoubleSpinBox) else int(float(raw)))
    except (TypeError, ValueError):
        pass


def _read_slider(w: Any) -> Any:
    return w.value()


def _write_slider(w: Any, raw: Any) -> None:
    if raw is None:
        return
    try:
        w.setValue(int(round(float(raw))))
    except (TypeError, ValueError):
        pass


def _read_combo(w: Any) -> Any:
    return w.currentData()


def _write_combo(w: Any, raw: Any) -> None:
    idx = w.findData(raw)
    if idx < 0 and raw is not None:
        w.addItem(f"⚠ {raw}", raw)
        idx = w.count() - 1
    if idx >= 0:
        w.setCurrentIndex(idx)


def _read_checkbox(w: Any) -> Any:
    return w.isChecked()


def _write_checkbox(w: Any, raw: Any) -> None:
    w.setChecked(bool(raw))


def _read_radio(w: Any) -> Any:
    for rb, val in getattr(w, "_radio_buttons", []):
        if rb.isChecked():
            return val
    return None


def _write_radio(w: Any, raw: Any) -> None:
    for rb, val in getattr(w, "_radio_buttons", []):
        rb.setChecked(val == raw)


def _read_keyseq(w: Any) -> Any:
    return w.keySequence().toString()


def _write_keyseq(w: Any, raw: Any) -> None:
    w.setKeySequence(QKeySequence("" if raw is None else str(raw)))


def _read_subform(w: Any) -> Any:
    return getattr(w, "_subform_value", None)


def _write_subform(w: Any, raw: Any) -> None:
    w._subform_value = raw


# ----------------------------------------------------------------------
# 构建器
# ----------------------------------------------------------------------
def _build_kv(parent: QWidget, options: dict[str, Any]) -> QWidget:
    edit = QLineEdit(parent)
    edit.setText(format_editor_text(options["value"]))
    ph = options.get("placeholder")
    if ph:
        edit.setPlaceholderText(str(ph))
    return edit


def _build_password(parent: QWidget, options: dict[str, Any]) -> QWidget:
    edit = QLineEdit(parent)
    edit.setEchoMode(QLineEdit.EchoMode.Password)
    if options["value"] is not None:
        edit.setText(str(options["value"]))
    ph = options.get("placeholder")
    if ph:
        edit.setPlaceholderText(str(ph))
    return edit


def _build_multiline(parent: QWidget, options: dict[str, Any]) -> QWidget:
    edit = QPlainTextEdit(parent)
    edit.setFixedHeight(100)
    edit.setPlainText(format_editor_text(options["value"]))
    ph = options.get("placeholder")
    if ph:
        edit.setPlaceholderText(str(ph))
    return edit


def _build_number(parent: QWidget, options: dict[str, Any]) -> QWidget:
    decimals = int(options.get("decimals") or 0)
    if decimals > 0:
        spin = QDoubleSpinBox(parent)
        spin.setDecimals(decimals)
    else:
        spin = QSpinBox(parent)
    if options.get("minimum") is not None:
        spin.setMinimum(options["minimum"])
    if options.get("maximum") is not None:
        spin.setMaximum(options["maximum"])
    if options.get("step") is not None:
        spin.setSingleStep(options["step"])
    value = options["value"]
    if value is not None:
        _write_number(spin, value)
    return spin


def _build_slider(parent: QWidget, options: dict[str, Any]) -> QWidget:
    minimum = int(round(float(options.get("minimum") or 0)))
    maximum = int(round(float(options.get("maximum") or 100)))
    step = int(round(float(options.get("step") or 1)))
    orientation = options.get("orientation") or Qt.Orientation.Horizontal
    slider = QSlider(orientation, parent)
    slider.setMinimum(minimum)
    slider.setMaximum(maximum)
    slider.setSingleStep(step)
    slider.setPageStep(max(step, (maximum - minimum) // 10 or step))
    if options["value"] is not None:
        _write_slider(slider, options["value"])
    return slider


def _build_combo(parent: QWidget, options: dict[str, Any]) -> QWidget:
    combo = QComboBox(parent)
    items = normalize_items(options.get("items"))
    for text, val in items:
        combo.addItem(text, val)
    value = options["value"]
    if value is not None:
        _write_combo(combo, value)
    elif items:
        combo.setCurrentIndex(0)
    return combo


def _build_radio(parent: QWidget, options: dict[str, Any]) -> QWidget:
    orientation = options.get("orientation") or Qt.Orientation.Vertical
    w = QWidget(parent)
    lay = (QHBoxLayout(w) if orientation == Qt.Orientation.Horizontal
           else QVBoxLayout(w))
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(8)

    group = QButtonGroup(w)
    buttons: list[tuple[QRadioButton, Any]] = []
    for text, val in normalize_items(options.get("items")):
        rb = QRadioButton(text, w)
        group.addButton(rb)
        lay.addWidget(rb)
        buttons.append((rb, val))
    w._radio_buttons = buttons  # noqa: SLF001 - 读回约定

    value = options["value"]
    for rb, val in buttons:
        if val == value:
            rb.setChecked(True)
            break
    return w


def _build_checkbox(parent: QWidget, options: dict[str, Any]) -> QWidget:
    cb = QCheckBox(parent)
    txt = options.get("text")
    if txt is not None:
        cb.setText(str(txt))
    cb.setChecked(bool(options["value"]))
    return cb


def _build_color(parent: QWidget, options: dict[str, Any]) -> QWidget:
    field = ColorField(parent)
    ph = options.get("placeholder")
    if ph:
        field.edit.setPlaceholderText(str(ph))
    field.setText(format_editor_text(options["value"]))
    return field


def _build_path(parent: QWidget, options: dict[str, Any]) -> QWidget:
    kind = options.get("browse_kind") or BrowseKind.FILE
    field = PathField(parent, browse_kind=kind)
    if options["value"] is not None:
        field.setText(str(options["value"]))
    ph = options.get("placeholder")
    if ph:
        field.edit.setPlaceholderText(str(ph))
    return field


def _build_keysequence(parent: QWidget, options: dict[str, Any]) -> QWidget:
    edit = QKeySequenceEdit(parent)
    if options["value"] is not None:
        value = options["value"]
        edit.setKeySequence(
            value if isinstance(value, QKeySequence) else QKeySequence(str(value))
        )
    return edit


def _build_subform(parent: QWidget, options: dict[str, Any]) -> QWidget:
    """SUBFORM 的容器由 FormView 提供（需要递归构建子表单）。"""
    builder = options.get("_subform_builder")
    if builder is None:  # pragma: no cover - 只在 FormView 之外误用时出现
        raise RuntimeError("SUBFORM 必须由 FormView 构建")
    return builder(parent, options)


CONTROL_SPECS: dict[RowControl, ControlSpec] = {
    RowControl.KV: ControlSpec(_build_kv, _read_text, _write_text,
                               "textChanged", changed_inner=True),
    RowControl.PASSWORD: ControlSpec(_build_password, _read_text, _write_text,
                                     "textChanged", changed_inner=True),
    RowControl.MULTILINE: ControlSpec(_build_multiline, _read_plain, _write_plain,
                                      "textChanged"),
    RowControl.NUMBER: ControlSpec(_build_number, _read_number, _write_number,
                                   "valueChanged"),
    RowControl.SLIDER: ControlSpec(_build_slider, _read_slider, _write_slider,
                                   "valueChanged"),
    RowControl.COMBO: ControlSpec(_build_combo, _read_combo, _write_combo,
                                  "currentIndexChanged"),
    RowControl.RADIO: ControlSpec(_build_radio, _read_radio, _write_radio),
    RowControl.CHECKBOX: ControlSpec(_build_checkbox, _read_checkbox,
                                     _write_checkbox, "toggled"),
    RowControl.COLOR: ControlSpec(_build_color, _read_text, _write_text,
                                  "textChanged", changed_inner=True),
    RowControl.PATH: ControlSpec(_build_path, _read_text, _write_text,
                                 "textChanged", changed_inner=True),
    RowControl.KEYSEQUENCE: ControlSpec(_build_keysequence, _read_keyseq,
                                        _write_keyseq, "keySequenceChanged"),
    RowControl.SUBFORM: ControlSpec(_build_subform, _read_subform, _write_subform),
}


# ----------------------------------------------------------------------
# 自然默认值（只有调用方没传 default= 时才作为 restore 的兜底）
# ----------------------------------------------------------------------
_TEXT_LIKE = frozenset({
    RowControl.KV, RowControl.PASSWORD, RowControl.MULTILINE,
    RowControl.COLOR, RowControl.PATH, RowControl.KEYSEQUENCE,
})


def natural_default(
        control: Union[RowControl, str], options: dict[str, Any],
) -> tuple[bool, Any]:
    """返回 ``(是否有自然默认, 值)``。

    - 文本类 → ""
    - NUMBER / SLIDER → minimum（未传 → 0）
    - CHECKBOX → False
    - COMBO / RADIO → items[0] 的 value（items 空则无）
    - SUBFORM / LABEL / SEPARATOR → 无
    """
    if control in _TEXT_LIKE:
        return True, ""
    if control in (RowControl.NUMBER, RowControl.SLIDER):
        minimum = options.get("minimum")
        return True, minimum if minimum is not None else 0
    if control is RowControl.CHECKBOX:
        return True, False
    if control in (RowControl.COMBO, RowControl.RADIO):
        items = normalize_items(options.get("items"))
        return (True, items[0][1]) if items else (False, None)
    return False, None
