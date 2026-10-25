"""参数编辑器工厂：按 ``inspect.Signature`` 往 ``QFormLayout`` 里塞控件。"""
from __future__ import annotations

import inspect
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from PySide6.QtGui import QDoubleValidator, QIntValidator
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QLineEdit,
    QWidget,
)

from ..model import Action

__all__ = ["Form", "build_form", "clear_form"]


@dataclass
class Form:
    """一组参数控件 + 读取函数。"""

    widgets: dict[str, QWidget] = field(default_factory=dict)
    kinds: dict[str, str] = field(default_factory=dict)
    required: set[str] = field(default_factory=set)

    def values(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, widget in self.widgets.items():
            kind = self.kinds.get(name, "str")
            if isinstance(widget, QCheckBox):
                result[name] = widget.isChecked()
                continue
            text = widget.text().strip()  # type: ignore[attr-defined]
            if not text:
                if name in self.required:
                    result[name] = ""
                continue
            try:
                result[name] = {
                    "int": int, "float": float,
                }.get(kind, str)(text)
            except ValueError:
                result[name] = text
        return result


def clear_form(layout: QFormLayout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget() if item is not None else None
        if widget is not None:
            widget.setParent(None)
            widget.deleteLater()


def _kind(param: inspect.Parameter) -> str:
    annotation = param.annotation
    text = (getattr(annotation, "__name__", None) or str(annotation)).lower()
    if "bool" in text or isinstance(param.default, bool):
        return "bool"
    if "int" in text and "point" not in text:
        return "int"
    if "float" in text:
        return "float"
    return "str"


def build_form(
        action: Action,
        values: Mapping[str, Any] | None,
        layout: QFormLayout,
) -> Form:
    """往 ``layout`` 里生成参数控件，返回可读取当前值的 :class:`Form`。"""
    clear_form(layout)
    form = Form()
    current = dict(values or {})
    defaults = action.default_args
    if action.signature is None:
        return form
    params = list(action.signature.parameters.values())
    if action.takes_context:
        params = params[1:]
    for param in params:
        kind = _kind(param)
        prefill = current.get(param.name, defaults.get(param.name, param.default))
        if kind == "bool":
            widget: QWidget = QCheckBox()
            checked = prefill if isinstance(prefill, bool) else bool(
                prefill is not None and prefill is not inspect.Parameter.empty
                and prefill not in ("", "0", "false", "False"))
            widget.setChecked(checked)  # type: ignore[attr-defined]
        else:
            widget = QLineEdit()
            widget.setClearButtonEnabled(True)
            if kind == "int":
                widget.setValidator(QIntValidator(widget))
            elif kind == "float":
                validator = QDoubleValidator(widget)
                validator.setNotation(QDoubleValidator.Notation.StandardNotation)
                widget.setValidator(validator)
            if prefill is not None and prefill is not inspect.Parameter.empty:
                widget.setText(str(prefill))
            widget.setPlaceholderText(
                "" if prefill is inspect.Parameter.empty else str(prefill))
        layout.addRow(param.name, widget)
        form.widgets[param.name] = widget
        form.kinds[param.name] = kind
        if param.default is inspect.Parameter.empty:
            form.required.add(param.name)
    return form
