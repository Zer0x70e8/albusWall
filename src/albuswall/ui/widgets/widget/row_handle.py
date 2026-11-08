# -*- coding: utf-8 -*-
"""行句柄、校验错误与值回显工具。"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QPushButton, QWidget

if TYPE_CHECKING:
    from ..view.form_view import FormView


# ----------------------------------------------------------------------
# 异常 & 值回显
# ----------------------------------------------------------------------
class ValidationError(ValueError):
    """get_value / collect 时 transform 或 validator 失败。"""


def format_editor_text(value: Any, display_hint: Optional[str] = None) -> str:
    """值 → 编辑器文本。

    - None → ""
    - str  → 原样（不加引号）
    - 其它 → json.dumps(ensure_ascii=False, indent=2, sort_keys=True)
    - display_hint == "json" 时用紧凑序列化
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if display_hint == "json":
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                          sort_keys=True)
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


# ----------------------------------------------------------------------
# 行句柄
# ----------------------------------------------------------------------
class RowHandle:
    """一行的稳定句柄。

    - ``row_id``：自增、不复用；
    - 值语义：``set_value`` 只接 raw，``get_value`` 返回 transform 之后的
      submitted；``initial_value()`` 为 add_row(value=) 的原始值（跳过校验）。
    - 历史栈：每行独立，只记 ``set_value`` 调用，只 undo 不 redo。
    """

    HISTORY_LIMIT = 50

    def __init__(
        self,
        form: "FormView",
        row_id: int,
        key: Optional[str],
        *,
        label_widget: QWidget,
        field_widget: QWidget,
        container: QWidget,
        lock_icon: Optional[QLabel],
        restore_btn: Optional[QPushButton],
        error_label: Optional[QLabel],
        read_fn: Callable[[QWidget], Any],
        write_fn: Callable[[QWidget, Any], None],
        initial_raw: Any,
        default_raw: Any,
        transform: Optional[Callable[[Any], Any]],
        validator: Optional[Callable[[Any], tuple]],
        fmt,
        label_text: str,
        is_value_row: bool = True,
    ):
        self._form = form
        self.row_id = row_id
        self.key = key
        self._label_widget = label_widget
        self._field = field_widget
        self._container = container
        self._lock_icon = lock_icon
        self._restore_btn = restore_btn
        self._error_label = error_label
        self._read_fn = read_fn
        self._write_fn = write_fn
        self._initial_raw = initial_raw
        self._default_raw = default_raw
        self._transform = transform
        self._validator = validator
        self._fmt = fmt
        self._label_text = label_text
        self.is_value_row = is_value_row

        self._enabled = True
        self._grid_row = 0  # 由 FormView 设置

        # 历史栈：初始值作为 base
        self._history: list[Any] = [initial_raw]
        self._index = 0

        if restore_btn is not None:
            if default_raw is not None:
                restore_btn.setVisible(True)
                restore_btn.setToolTip("恢复默认值")
                restore_btn.clicked.connect(self.restore_default)
            else:
                restore_btn.setVisible(False)
        if lock_icon is not None:
            lock_icon.setVisible(False)
        if error_label is not None:
            error_label.setText("")  # 常驻占位：保持可见但空

    # ------------------------------------------------------------------
    # 值
    # ------------------------------------------------------------------
    def _read_raw(self) -> Any:
        return self._read_fn(self._field)

    def _write_raw(self, raw: Any) -> None:
        self._write_fn(self._field, raw)

    def get_value(self) -> Any:
        """读取并转换当前 raw。失败时抛 ValidationError 且自动 set_error。"""
        raw = self._read_raw()
        try:
            submitted = self._transform(raw) if self._transform else raw
        except ValueError as exc:
            msg = str(exc) or "值转换失败"
            self.set_error(msg)
            raise ValidationError(msg) from exc
        if self._validator is not None:
            ok, msg = self._validator(submitted)
            if not ok:
                msg = msg or "校验失败"
                self.set_error(msg)
                raise ValidationError(msg)
        self.clear_error()
        return submitted

    def submitted_from_raw(self, raw: Any) -> Any:
        """回滚用：走 transform 跳过 validator（初始值可信）。"""
        return self._transform(raw) if self._transform else raw

    def set_value(self, raw: Any) -> None:
        """只接 raw，transform 单方向；同时入本行历史栈。"""
        self._write_raw(raw)
        self._push_history(raw)

    def initial_value(self) -> Any:
        return self._initial_raw

    def default_value(self) -> Any:
        return self._default_raw

    def restore_default(self) -> None:
        if self._default_raw is None:
            return
        self.set_value(self._default_raw)

    # ------------------------------------------------------------------
    # 历史
    # ------------------------------------------------------------------
    def _push_history(self, raw: Any) -> None:
        if self._history[self._index] == raw:
            return
        del self._history[self._index + 1:]
        self._history.append(raw)
        self._index += 1
        overflow = len(self._history) - self.HISTORY_LIMIT
        if overflow > 0:
            del self._history[:overflow]
            self._index -= overflow

    def history(self) -> list:
        """含当前值，[-1] 即当前 raw。"""
        return list(self._history[: self._index + 1])

    def can_undo(self) -> bool:
        return self._index > 0

    def undo(self) -> None:
        if not self.can_undo():
            return
        self._index -= 1
        self._write_raw(self._history[self._index])

    def clear_history(self) -> None:
        self._history = [self._read_raw()]
        self._index = 0

    def can_redo(self) -> bool:
        """留位：本期不实现 redo，恒 False。"""
        return False

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    def focus(self) -> None:
        self._field.setFocus(Qt.FocusReason.OtherFocusReason)

    def set_error(self, msg: str) -> None:
        msg = str(msg)
        if self._error_label is not None:
            self._error_label.setText(msg)
            self._error_label.setToolTip(msg)
        self._apply_invalid(True)

    def clear_error(self) -> None:
        if self._error_label is not None:
            self._error_label.setText("")
            self._error_label.setToolTip("")
        self._apply_invalid(False)

    def _apply_invalid(self, invalid: bool) -> None:
        targets = [self._field]
        inner = getattr(self._field, "edit", None)
        if inner is not None:
            targets.append(inner)
        for w in targets:
            w.setProperty("invalid", invalid)
            style = w.style()
            style.unpolish(w)
            style.polish(w)

    def set_enabled(self, enabled: bool, reason: Optional[str] = None) -> None:
        self._enabled = bool(enabled)
        self._field.setEnabled(self._enabled)
        if self._lock_icon is not None:
            self._lock_icon.setVisible(not self._enabled)
            self._lock_icon.setToolTip(
                (reason or "") if not self._enabled else ""
            )

    def remove(self) -> None:
        self._form._remove_handle(self)

    # ------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def grid_row(self) -> int:
        return self._grid_row
