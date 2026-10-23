#
"""设置项编辑器：把 ``setting.schema`` 的 ``widget`` 提示映射成 Qt 控件。

输入的 item 就是 ``dump_setting_schema()`` 里的叶子 dict，本模块只认这些键：

``widget`` / ``type`` / ``value`` / ``default`` / ``choices`` / ``constraints``

约定
====

* 编辑器只负责「显示值」和「给一个可以交给后端的新值」，**不落盘**；
  写回统一走 :class:`albuswall.ui.builtin.setting.backend.SettingBackend`，
  由 ``SettingItem.coerce`` 做类型转换（UI 传字符串也安全）。
* 空输入：``str`` 类型给 ``""``，其余类型给 ``None``（后端会写成空值），
  这样「有默认值但没显式配置」的节点不会因为编辑被写成 ``"None"``。
* 所有编辑器的「提交」都走 ``editingFinished`` / ``toggled`` 一类语义，
  **不会**每敲一个字符就写盘。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from PySide6.QtCore import QCoreApplication, Qt, Signal
from PySide6.QtGui import QDoubleValidator, QIntValidator
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QHBoxLayout,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QWidget,
)

__all__ = [
    "BoolEditor",
    "ChoiceEditor",
    "Editor",
    "FloatEditor",
    "IntEditor",
    "LineEditor",
    "MultilineEditor",
    "PathEditor",
    "create_editor",
    "resolve_widget",
]

_INT_MIN, _INT_MAX = -2_147_483_648, 2_147_483_647

#: 翻译上下文（编辑器不是 QObject，用 ``QCoreApplication.translate`` 代替 tr）
_TR_CONTEXT = "SettingEditors"


def _tr(text: str) -> str:
    """模块级翻译入口（等价于 ``QObject.tr``，上下文固定）。"""
    return QCoreApplication.translate(_TR_CONTEXT, text)


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def _str_value(value: Any) -> str:
    """值 → 编辑器里的文本。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list, tuple)):
        try:
            return json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def _choice_text(value: Any, labels: Mapping[str, str]) -> str:
    """选项值 → 展示文本；``choice_labels`` 优先（例如语言代码 → 母语名）。"""
    label = labels.get(str(value)) if labels else None
    return str(label) if label else _str_value(value)


def _empty_value(item: dict[str, Any]) -> Any:
    """空输入要写回的值：str 写 ``""``，其余给 ``None``。"""
    return "" if (item.get("type") or "") == "str" else None


def _constraints(item: dict[str, Any]) -> dict[str, Any]:
    constraints = item.get("constraints")
    return constraints if isinstance(constraints, dict) else {}


def resolve_widget(item: dict[str, Any]) -> str:
    """把 ``widget``（可能是 ``auto``）收敛成具体的编辑器类型名。"""
    widget = (item.get("widget") or "auto").strip().lower()
    if widget and widget != "auto":
        return widget

    type_name = (item.get("type") or "").strip()
    constraints = _constraints(item)

    if type_name == "bool":
        return "bool"
    if type_name == "int":
        return "int"
    if type_name == "float":
        return "float"
    if type_name.startswith("dict"):
        return "multiline"
    if type_name.startswith(("list", "tuple", "set", "frozenset")):
        return "text"
    if type_name == "str":
        return "text"
    # 类型未知（值还没声明 / 还没读到）时，靠 constraints 猜数量级
    if constraints:
        step = constraints.get("step")
        if isinstance(step, float) and not float(step).is_integer():
            return "float"
        return "int"
    return "text"


def _same(a: Any, b: Any) -> bool:
    """松散比较：``1`` 与 ``1.0``、``"1"`` 与自己都算相同。"""
    if a == b:
        return True
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return float(a) == float(b)
    return _str_value(a) == _str_value(b)


def _resolve_choices(item: dict[str, Any]) -> list[Any]:
    """候选值：优先动态（``choices_provider``，每次建控件都现查），否则静态。

    动态来源失败时退回静态候选并记日志 —— 比如主题目录读不了，也不该让
    整行建不出来。
    """
    provider = item.get("choices_provider")
    if callable(provider):
        try:
            dynamic = [choice for choice in provider()]
        except Exception:  # noqa: BLE001
            from albuswall.log import getLogger

            getLogger(__name__).exception(
                "choices_provider failed for %r", item.get("key")
            )
            dynamic = []
        if dynamic:
            return dynamic
    return list(item.get("choices") or ())


# --------------------------------------------------------------------------- #
# 基类
# --------------------------------------------------------------------------- #
class Editor:
    """设置项编辑器的统一接口。"""

    widget: QWidget

    def __init__(self, item: dict[str, Any]) -> None:
        self._item = item

    # -- 子类实现 -------------------------------------------------- #
    def connect_commit(self, slot: Callable[[], None]) -> None:
        raise NotImplementedError

    def value(self) -> Any:
        raise NotImplementedError

    def set_value(self, value: Any) -> None:
        raise NotImplementedError

    # -- 通用 ------------------------------------------------------ #
    def set_read_only(self, read_only: bool) -> None:
        self.widget.setEnabled(not read_only)
        if read_only:
            self.widget.setToolTip(_tr("This item is read-only here."))

    def current(self) -> Any:
        """当前展示的值（用于「有没有改过」的判断）。"""
        return self._item.get("value")


# --------------------------------------------------------------------------- #
# 文本
# --------------------------------------------------------------------------- #
class LineEditor(Editor):
    """单行文本 / 数字文本 / 密码。

    ``numeric`` 为 ``"int"`` / ``"float"`` 时按数字解析（配合 validator）；
    为 ``None`` 时原样交出字符串。
    """

    def __init__(
            self,
            item: dict[str, Any],
            *,
            numeric: str | None = None,
            password: bool = False,
            placeholder: str = "",
    ) -> None:
        super().__init__(item)
        self._numeric = numeric
        self._edit = QLineEdit()
        self._edit.setObjectName("SettingRowEditor")
        self._edit.setClearButtonEnabled(True)

        if password:
            self._edit.setEchoMode(QLineEdit.EchoMode.Password)
        if numeric == "int":
            self._edit.setValidator(QIntValidator(_INT_MIN, _INT_MAX, self._edit))
        elif numeric == "float":
            validator = QDoubleValidator(self._edit)
            validator.setNotation(QDoubleValidator.Notation.StandardNotation)
            self._edit.setValidator(validator)
        if placeholder:
            self._edit.setPlaceholderText(placeholder)

        self.widget = self._edit
        self.set_value(item.get("value"))

    def connect_commit(self, slot: Callable[[], None]) -> None:
        self._edit.editingFinished.connect(slot)

    def value(self) -> Any:
        text = self._edit.text().strip()
        if not text:
            return _empty_value(self._item)
        if self._numeric == "int":
            try:
                return int(float(text))
            except ValueError as exc:
                raise ValueError(
                    _tr("Expected an integer, got {text!r}").format(
                        text=text,
                    )
                ) from exc
        if self._numeric == "float":
            try:
                return float(text)
            except ValueError as exc:
                raise ValueError(
                    _tr("Expected a number, got {text!r}").format(
                        text=text,
                    )
                ) from exc
        return self._edit.text()

    def set_value(self, value: Any) -> None:
        blocked = self._edit.blockSignals(True)
        try:
            self._edit.setText(_str_value(value))
        finally:
            self._edit.blockSignals(blocked)


class _CommitPlainText(QPlainTextEdit):
    """失焦 / Ctrl+Enter 时提交的多行文本框（QPlainTextEdit 没有 editingFinished）。"""

    committed = Signal()

    def focusOutEvent(self, event):  # noqa: N802 - Qt 命名
        super().focusOutEvent(event)
        self.committed.emit()

    def keyPressEvent(self, event):  # noqa: N802 - Qt 命名
        if (
                event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                and event.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            self.committed.emit()
            return
        super().keyPressEvent(event)


class MultilineEditor(Editor):
    """多行文本；``dict`` 类型按 JSON 编辑。"""

    def __init__(self, item: dict[str, Any]) -> None:
        super().__init__(item)
        self._is_json = (item.get("type") or "").startswith("dict")
        self._edit = _CommitPlainText()
        self._edit.setObjectName("SettingRowEditor")
        self._edit.setMinimumHeight(78)
        self._edit.setTabChangesFocus(True)
        if self._is_json:
            self._edit.setPlaceholderText('{"key": value}')
        self.widget = self._edit
        self.set_value(item.get("value"))

    def connect_commit(self, slot: Callable[[], None]) -> None:
        self._edit.committed.connect(slot)

    def value(self) -> Any:
        text = self._edit.toPlainText().strip()
        if not text:
            return _empty_value(self._item)
        if not self._is_json:
            return self._edit.toPlainText()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(
                _tr("JSON parse error: {message}").format(
                    message=exc.msg,
                )
            ) from exc
        if not isinstance(parsed, dict):
            raise ValueError(_tr("A JSON object {…} is required here."))
        return parsed

    def set_value(self, value: Any) -> None:
        blocked = self._edit.blockSignals(True)
        try:
            self._edit.setPlainText(_str_value(value))
        finally:
            self._edit.blockSignals(blocked)


# --------------------------------------------------------------------------- #
# 数值
# --------------------------------------------------------------------------- #
class IntEditor(Editor):
    """整数微调框；``minimum`` / ``maximum`` / ``step`` 来自 constraints。"""

    def __init__(self, item: dict[str, Any]) -> None:
        super().__init__(item)
        constraints = _constraints(item)
        self._spin = QSpinBox()
        self._spin.setObjectName("SettingRowEditor")
        minimum = constraints.get("minimum")
        maximum = constraints.get("maximum")
        step = constraints.get("step")
        self._spin.setRange(
            _INT_MIN if minimum is None else int(minimum),
            _INT_MAX if maximum is None else int(maximum),
        )
        if step:
            self._spin.setSingleStep(max(1, int(step)))
        self.widget = self._spin
        self.set_value(item.get("value"))

    def connect_commit(self, slot: Callable[[], None]) -> None:
        self._spin.editingFinished.connect(slot)

    def value(self) -> Any:
        return int(self._spin.value())

    def set_value(self, value: Any) -> None:
        blocked = self._spin.blockSignals(True)
        try:
            if value is None:
                self._spin.setValue(self._spin.minimum())
            else:
                self._spin.setValue(int(float(value)))
        finally:
            self._spin.blockSignals(blocked)


class FloatEditor(Editor):
    """浮点微调框。"""

    def __init__(self, item: dict[str, Any]) -> None:
        super().__init__(item)
        constraints = _constraints(item)
        self._spin = QDoubleSpinBox()
        self._spin.setObjectName("SettingRowEditor")
        self._spin.setDecimals(3)
        minimum = constraints.get("minimum")
        maximum = constraints.get("maximum")
        step = constraints.get("step")
        self._spin.setRange(
            -1e9 if minimum is None else float(minimum),
            1e9 if maximum is None else float(maximum),
        )
        self._spin.setSingleStep(float(step) if step else 1.0)
        self.widget = self._spin
        self.set_value(item.get("value"))

    def connect_commit(self, slot: Callable[[], None]) -> None:
        self._spin.editingFinished.connect(slot)

    def value(self) -> Any:
        return float(self._spin.value())

    def set_value(self, value: Any) -> None:
        blocked = self._spin.blockSignals(True)
        try:
            if value is None:
                self._spin.setValue(self._spin.minimum())
            else:
                self._spin.setValue(float(value))
        finally:
            self._spin.blockSignals(blocked)


# --------------------------------------------------------------------------- #
# 布尔 / 枚举
# --------------------------------------------------------------------------- #
class BoolEditor(Editor):
    """勾选框；当前值未设置时用三态表示「跟随默认」。"""

    def __init__(self, item: dict[str, Any]) -> None:
        super().__init__(item)
        self._box = QCheckBox()
        self._box.setObjectName("SettingRowEditor")
        if item.get("value") is None:
            self._box.setTristate(True)
            self._box.setToolTip(
                _tr("Partially checked = not set explicitly (follows the "
                    "default).")
            )
        self.widget = self._box
        self.set_value(item.get("value"))

    def connect_commit(self, slot: Callable[[], None]) -> None:
        self._box.toggled.connect(lambda _checked: slot())

    def value(self) -> Any:
        state = self._box.checkState()
        if state == Qt.CheckState.PartiallyChecked:
            return None
        return state == Qt.CheckState.Checked

    def set_value(self, value: Any) -> None:
        blocked = self._box.blockSignals(True)
        try:
            if value is None and self._box.isTristate():
                self._box.setCheckState(Qt.CheckState.PartiallyChecked)
            elif value is None:
                self._box.setCheckState(Qt.CheckState.Unchecked)
            else:
                self._box.setCheckState(
                    Qt.CheckState.Checked if value
                    else Qt.CheckState.Unchecked
                )
        finally:
            self._box.blockSignals(blocked)


class ChoiceEditor(Editor):
    """下拉枚举；当前值未设置时多一条「（默认）」。"""

    _UNSET = object()

    def __init__(self, item: dict[str, Any]) -> None:
        super().__init__(item)
        self._combo = QComboBox()
        self._combo.setObjectName("SettingRowEditor")
        self.widget = self._combo

        self._choices = _resolve_choices(item)
        current = item.get("value")

        if current is None:
            self._combo.addItem(_tr("(default)"), self._UNSET)
        if current is not None and not any(
                _same(current, choice) for choice in self._choices
        ):
            # 当前值不在候选里（比如手改过配置文件）：保留可见，别悄悄改掉
            self._choices.append(current)

        labels = item.get("choice_labels") or {}
        for choice in self._choices:
            self._combo.addItem(_choice_text(choice, labels), choice)

        self.set_value(current)

    def connect_commit(self, slot: Callable[[], None]) -> None:
        self._combo.currentIndexChanged.connect(lambda _index: slot())

    def value(self) -> Any:
        data = self._combo.currentData()
        return None if data is self._UNSET else data

    def set_value(self, value: Any) -> None:
        blocked = self._combo.blockSignals(True)
        try:
            if value is None:
                index = self._combo.findData(self._UNSET)
            else:
                index = -1
                for i in range(self._combo.count()):
                    if _same(self._combo.itemData(i), value):
                        index = i
                        break
            if index >= 0:
                self._combo.setCurrentIndex(index)
        finally:
            self._combo.blockSignals(blocked)


# --------------------------------------------------------------------------- #
# 路径
# --------------------------------------------------------------------------- #
class PathEditor(Editor):
    """路径输入：文本框 + 浏览按钮。``kind`` 取 ``"file"`` / ``"dir"``。"""

    def __init__(self, item: dict[str, Any], *, kind: str = "dir") -> None:
        super().__init__(item)
        self._kind = kind

        self._container = QWidget()
        self._container.setObjectName("SettingRowPathEditor")
        layout = QHBoxLayout(self._container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self._edit = QLineEdit()
        self._edit.setObjectName("SettingRowEditor")
        self._edit.setClearButtonEnabled(True)
        self._edit.setPlaceholderText(
            _tr("(default directory)") if kind == "dir"
            else _tr("(default file)")
        )

        self._browse = QPushButton("…")
        self._browse.setObjectName("SettingRowBrowseButton")
        self._browse.setCursor(Qt.CursorShape.PointingHandCursor)
        self._browse.setToolTip(_tr("Browse…"))
        self._browse.clicked.connect(self._on_browse)

        layout.addWidget(self._edit, 1)
        layout.addWidget(self._browse, 0)

        self.widget = self._container
        self.set_value(item.get("value"))

    # -- 浏览 ------------------------------------------------------ #
    def _on_browse(self) -> None:
        current = self._edit.text().strip()
        start = current or ""
        if self._kind == "dir":
            chosen = QFileDialog.getExistingDirectory(
                self._container, _tr("Select directory"), start,
            )
        else:
            chosen, _ = QFileDialog.getOpenFileName(
                self._container, _tr("Select file"), start,
            )
        if chosen:
            self.set_value(chosen)
            # 浏览是明确的用户动作，直接提交
            self._edit.editingFinished.emit()

    # -- Editor ---------------------------------------------------- #
    def connect_commit(self, slot: Callable[[], None]) -> None:
        self._edit.editingFinished.connect(slot)

    def value(self) -> Any:
        text = self._edit.text().strip()
        return text or _empty_value(self._item)

    def set_value(self, value: Any) -> None:
        blocked = self._edit.blockSignals(True)
        try:
            self._edit.setText(_str_value(value))
        finally:
            self._edit.blockSignals(blocked)

    def set_read_only(self, read_only: bool) -> None:
        super().set_read_only(read_only)
        self._browse.setEnabled(not read_only)


# --------------------------------------------------------------------------- #
# 工厂
# --------------------------------------------------------------------------- #
def create_editor(item: dict[str, Any]) -> Editor:
    """按 item 的 ``widget`` 提示造一个编辑器。

    注意：数值项如果当前值为 ``None``（「跟随默认」），不用微调框 ——
    微调框没有「空」状态，会误导成「值就是最小值」。这种情况下退化成
    带校验的单行输入，提示语里写明默认值。
    """
    custom = _custom_editor(item)
    if custom is not None:
        return custom

    widget = resolve_widget(item)
    value = item.get("value")

    if widget == "bool":
        return BoolEditor(item)
    if widget == "choice":
        return ChoiceEditor(item)
    if widget == "multiline":
        return MultilineEditor(item)
    if widget in ("dir", "file", "path"):
        return PathEditor(item, kind="file" if widget == "file" else "dir")
    if widget == "secret":
        return LineEditor(item, password=True)
    if widget == "int":
        if value is None:
            return LineEditor(
                item, numeric="int", placeholder=_default_hint(item),
            )
        return IntEditor(item)
    if widget == "float":
        if value is None:
            return LineEditor(
                item, numeric="float", placeholder=_default_hint(item),
            )
        return FloatEditor(item)
    return LineEditor(item)


def _custom_editor(item: dict[str, Any]) -> Editor | None:
    """补丁里 ``editor=`` 指定的自定义编辑器（失败退回默认控件）。"""
    patch = item.get("patch")
    factory = getattr(patch, "editor", None) if patch is not None else None
    if not callable(factory):
        return None
    try:
        editor = factory(item)
    except Exception:  # noqa: BLE001
        from albuswall.log import getLogger

        getLogger(__name__).exception(
            "custom editor failed for %r", item.get("key")
        )
        return None
    if isinstance(editor, Editor):
        return editor
    from albuswall.log import getLogger

    getLogger(__name__).warning(
        "custom editor for %r returned %r, expected an Editor",
        item.get("key"), type(editor).__name__,
    )
    return None


def _default_hint(item: dict[str, Any]) -> str:
    default = item.get("default")
    if default is None:
        return _tr("(default / auto)")
    return _tr("Default: {value}").format(value=_str_value(default))
