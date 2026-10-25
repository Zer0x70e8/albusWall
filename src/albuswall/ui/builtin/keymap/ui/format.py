"""展示格式化（Qt）：``signature_text`` / 面包屑 / 按键文本 / 排序。

内核的 ``Action`` 不携带任何 UI 字符串，所有「设置页长什么样」都在这里。
"""
from __future__ import annotations

import inspect
from typing import Any

from PySide6.QtGui import QKeySequence

from ..components import component_path
from ..model import Action, Binding

__all__ = [
    "args_text", "breadcrumb", "component_label", "key_text",
    "signature_text",
]


def key_text(key: str) -> str:
    """规范键文本 → 当前平台展示文本（macOS 上显示 ⌘）。"""
    if not key:
        return ""
    sequence = QKeySequence.fromString(
        key, QKeySequence.SequenceFormat.PortableText)
    if sequence.isEmpty():
        return key
    return sequence.toString(QKeySequence.SequenceFormat.NativeText)


def component_label(component_id: str) -> str:
    chain = component_path(component_id)
    return chain[-1].label if chain else component_id


def breadcrumb(component_id: str) -> str:
    return " › ".join(c.label for c in component_path(component_id)) or component_id


def _annotation_name(annotation: Any) -> str:
    if annotation is inspect.Parameter.empty:
        return ""
    text = getattr(annotation, "__name__", None) or str(annotation)
    return text.replace("typing.", "")


def signature_text(action: Action) -> str:
    """``(page: str = '')`` 形式的参数摘要（不重复 ``ctx``）。"""
    if action.signature is None:
        return "()"
    params = list(action.signature.parameters.values())
    if action.takes_context:
        params = params[1:]
    parts: list[str] = []
    for param in params:
        text = param.name
        annotation = _annotation_name(param.annotation)
        if annotation:
            text += f": {annotation}"
        if param.default is not inspect.Parameter.empty:
            text += f" = {param.default!r}"
        parts.append(text)
    return f"({', '.join(parts)})"


def args_text(binding: Binding) -> str:
    return ", ".join(f"{name}={value!r}" for name, value in binding.args)

