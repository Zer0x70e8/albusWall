"""叶子行的**编辑器**：值类别 → 控件 + 值同步，一处注册、一处兜底。

为什么单独一层
==============

一个叶子行原来把"值类别 → 控件"的逻辑写了四遍：建控件（if/elif）、回填
值（if/elif）、用户改动（五个 ``_on_*_changed``）、禁用处理（``isinstance``
判断）。加一种值类别要同时改四处，漏一处就出"控件建出来了但不回填"这种
半死不活的 bug。

现在每个值类别是一个 :class:`Editor` 子类，注册在 :data:`EDITORS`；行控件
只负责布局，**不再认识具体控件类型**。

约定
====

* ``set_value(value)``：外部值 → 控件（不回环，不触发回调）；
* ``read()``：控件 → typed 值（JSON 编辑器的非法文本由编辑器自己记
  :attr:`Editor.error`，并且**不**回调）；
* ``on_change(callback)``：合法改动时回调 typed 值；
* ``set_editable(flag)``：统一处理禁用 / 只读（不用 ``isinstance``）。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from PySide6.QtCore import QSignalBlocker
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QLineEdit,
    QPlainTextEdit,
    QSpinBox,
    QWidget,
)

from albuswall.common.enums import SettingWidget

from . import strings

__all__ = [
    "EDITORS",
    "BoolEditor",
    "ChoiceEditor",
    "Editor",
    "FloatEditor",
    "IntEditor",
    "JsonEditor",
    "PathEditor",
    "TextEditor",
    "create_editor",
    "format_editor_text",
]

#: QSpinBox / QDoubleSpinBox 能表示的安全范围（配置里出现更大的数就夹住）。
_INT_MIN = -2_000_000_000
_INT_MAX = 2_000_000_000
_FLOAT_MIN = -1.0e9
_FLOAT_MAX = 1.0e9


class Editor:
    """一个叶子编辑器的公共壳：控件 + 值 + 合法改动回调。"""

    def __init__(self, leaf: Mapping[str, Any], parent: QWidget) -> None:
        self.leaf: dict[str, Any] = dict(leaf)
        self.kind: str = str(leaf.get("widget") or SettingWidget.TEXT.value)
        self.error: str | None = None
        self._on_change: Callable[[Any], None] | None = None
        self._updating = False
        self.widget: QWidget = self._create_widget(parent)

    # ------------------------------------------------------------------
    # 子类实现
    # ------------------------------------------------------------------
    def _create_widget(self, parent: QWidget) -> QWidget:
        raise NotImplementedError

    def set_value(self, value: Any) -> None:
        raise NotImplementedError

    def read(self) -> Any:
        raise NotImplementedError

    # ------------------------------------------------------------------
    # 公共
    # ------------------------------------------------------------------
    def on_change(self, callback: Callable[[Any], None]) -> None:
        self._on_change = callback

    def _emit(self, value: Any) -> None:
        if self._updating or self._on_change is None:
            return
        self._on_change(value)

    def set_editable(self, editable: bool) -> None:
        self.widget.setEnabled(bool(editable))
        if isinstance(self.widget, (QLineEdit, QPlainTextEdit)):
            self.widget.setReadOnly(not editable)

    def focus_target(self) -> QWidget:
        """键盘焦点落点（默认就是控件本身）。"""
        return self.widget


class BoolEditor(Editor):
    def _create_widget(self, parent: QWidget) -> QWidget:
        checkbox = QCheckBox(parent)
        checkbox.setFixedWidth(24)
        checkbox.toggled.connect(lambda checked: self._emit(bool(checked)))
        return checkbox

    @property
    def _checkbox(self) -> QCheckBox:
        return self.widget  # type: ignore[return-value]

    def set_value(self, value: Any) -> None:
        with QSignalBlocker(self.widget):
            self._checkbox.setChecked(bool(value))

    def read(self) -> Any:
        return self._checkbox.isChecked()


class IntEditor(Editor):
    def _create_widget(self, parent: QWidget) -> QWidget:
        spin = QSpinBox(parent)
        constraints = self.leaf.get("constraints") or {}
        minimum = _num(constraints.get("minimum"), _INT_MIN)
        maximum = _num(constraints.get("maximum"), _INT_MAX)
        minimum_int = int(max(_INT_MIN, min(minimum, _INT_MAX)))
        maximum_int = int(min(_INT_MAX, max(maximum, _INT_MIN)))
        if maximum_int < minimum_int:
            maximum_int = minimum_int
        spin.setRange(minimum_int, maximum_int)
        step = _num(constraints.get("step"), 1)
        spin.setSingleStep(max(1, int(step)))
        spin.setKeyboardTracking(False)
        spin.valueChanged.connect(lambda value: self._emit(int(value)))
        return spin

    def set_value(self, value: Any) -> None:
        try:
            number = int(value)
        except (TypeError, ValueError):
            number = 0
        with QSignalBlocker(self.widget):
            self.widget.setValue(number)  # type: ignore[attr-defined]

    def read(self) -> Any:
        return int(self.widget.value())  # type: ignore[attr-defined]


class FloatEditor(Editor):
    def _create_widget(self, parent: QWidget) -> QWidget:
        spin = QDoubleSpinBox(parent)
        constraints = self.leaf.get("constraints") or {}
        minimum = _num(constraints.get("minimum"), _FLOAT_MIN)
        maximum = _num(constraints.get("maximum"), _FLOAT_MAX)
        minimum_float = max(_FLOAT_MIN, min(minimum, _FLOAT_MAX))
        maximum_float = min(_FLOAT_MAX, max(maximum, _FLOAT_MIN))
        if maximum_float < minimum_float:
            maximum_float = minimum_float
        spin.setRange(minimum_float, maximum_float)
        step = _num(constraints.get("step"), 0.1)
        step = max(step, 10 ** -6)
        spin.setSingleStep(step)
        spin.setDecimals(_decimals_for(step))
        spin.setKeyboardTracking(False)
        spin.valueChanged.connect(lambda value: self._emit(float(value)))
        return spin

    def set_value(self, value: Any) -> None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = 0.0
        with QSignalBlocker(self.widget):
            self.widget.setValue(number)  # type: ignore[attr-defined]

    def read(self) -> Any:
        return float(self.widget.value())  # type: ignore[attr-defined]


class ChoiceEditor(Editor):
    """下拉框：``choices`` + ``choice_labels``；当前值不在选项里时追加一条。"""

    def _create_widget(self, parent: QWidget) -> QWidget:
        combo = QComboBox(parent)
        labels = self.leaf.get("choice_labels") or {}
        for choice in self.leaf.get("choices") or ():
            combo.addItem(_choice_label(choice, labels), choice)
        combo.currentIndexChanged.connect(self._on_index_changed)
        return combo

    @property
    def _combo(self) -> QComboBox:
        return self.widget  # type: ignore[return-value]

    def _on_index_changed(self, index: int) -> None:
        if index < 0:
            return
        self._emit(self._combo.itemData(index))

    def set_value(self, value: Any) -> None:
        combo = self._combo
        with QSignalBlocker(combo):
            index = _find_choice(combo, value)
            if index < 0 and value is not None:
                # 声明里没有的当前值：追加一条，不能假装它是某个选项
                combo.addItem(f"⚠ {format_editor_text(value)}", value)
                index = combo.count() - 1
            combo.setCurrentIndex(index if index >= 0 else 0)

    def read(self) -> Any:
        index = self._combo.currentIndex()
        return None if index < 0 else self._combo.itemData(index)


class TextEditor(Editor):
    """单行文本；``interpolated`` 时回显原始串。"""

    def _create_widget(self, parent: QWidget) -> QWidget:
        line = QLineEdit(parent)
        line.textChanged.connect(lambda _text: self._emit(line.text()))
        return line

    def set_value(self, value: Any) -> None:
        if self.leaf.get("interpolated"):
            text = str(self.leaf.get("raw") or "")
        else:
            text = format_editor_text(value)
        with QSignalBlocker(self.widget):
            self.widget.setText(text)  # type: ignore[attr-defined]

    def read(self) -> Any:
        return self.widget.text()  # type: ignore[attr-defined]


class PathEditor(TextEditor):
    """路径：文本编辑器 + 行控件里的「…」按钮（按钮由行负责）。"""


class JsonEditor(Editor):
    """``list`` / ``dict``：多行 JSON 文本，非法 JSON 只记 :attr:`error`。"""

    def _create_widget(self, parent: QWidget) -> QWidget:
        box = QPlainTextEdit(parent)
        box.setFixedHeight(64)
        box.textChanged.connect(self._on_text_changed)
        return box

    @property
    def _box(self) -> QPlainTextEdit:
        return self.widget  # type: ignore[return-value]

    def _on_text_changed(self) -> None:
        try:
            value = self.read()
        except ValueError as exc:
            self.error = str(exc)
            return
        self.error = None
        self._emit(value)

    def set_value(self, value: Any) -> None:
        self.error = None
        with QSignalBlocker(self.widget):
            self._box.setPlainText(_json_text(value))

    def read(self) -> Any:
        text = self._box.toPlainText()
        if not text.strip():
            return [] if self.kind == SettingWidget.LIST.value else {}
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(
                strings.JSON_INVALID.format(
                    message=exc.msg, line=exc.lineno,
                ),
            ) from exc

    def set_editable(self, editable: bool) -> None:
        super().set_editable(editable)


#: 值类别 → 编辑器类。``auto`` / 未知类型都落到文本编辑器。
EDITORS: dict[str, type[Editor]] = {
    SettingWidget.BOOL.value: BoolEditor,
    SettingWidget.INT.value: IntEditor,
    SettingWidget.FLOAT.value: FloatEditor,
    SettingWidget.CHOICE.value: ChoiceEditor,
    SettingWidget.PATH.value: PathEditor,
    SettingWidget.LIST.value: JsonEditor,
    SettingWidget.DICT.value: JsonEditor,
    SettingWidget.TEXT.value: TextEditor,
    SettingWidget.AUTO.value: TextEditor,
}


def create_editor(leaf: Mapping[str, Any], parent: QWidget) -> Editor:
    """按叶子的 ``widget`` 造编辑器（未知类别退化为文本）。"""
    kind = str(leaf.get("widget") or SettingWidget.TEXT.value)
    return EDITORS.get(kind, TextEditor)(leaf, parent)


# ============================================================================
# 辅助
# ============================================================================

def format_editor_text(value: Any) -> str:
    """值的紧凑文本（字符串不加引号，其它走 JSON）。"""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return repr(value)


def _num(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _choice_label(choice: Any, labels: Mapping[Any, Any]) -> str:
    """选项的显示文案：``choice_labels`` 优先，支持值 / 字符串两种键。"""
    try:
        if choice in labels:
            return str(labels[choice])
    except TypeError:  # 不可哈希的选项
        pass
    text = str(choice)
    if text in labels:
        return str(labels[text])
    return text


def _find_choice(combo: QComboBox, value: Any) -> int:
    for index in range(combo.count()):
        try:
            if combo.itemData(index) == value:
                return index
        except Exception:  # noqa: BLE001  自定义对象比较失败
            continue
    return -1


def _json_text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    except (TypeError, ValueError):
        return repr(value)


def _decimals_for(step: float) -> int:
    text = f"{step:.6f}".rstrip("0")
    if "." not in text:
        return 0
    return min(6, len(text.split(".", 1)[1]))


def looks_like_file(path: str) -> bool:
    """路径文本更像文件（有后缀、不以分隔符结尾）还是目录。"""
    from pathlib import Path

    if not path:
        return False
    cleaned = path.rstrip("/\\")
    return bool(Path(cleaned).suffix) and not cleaned.endswith(("/", "\\"))

