# -*- coding: utf-8 -*-
"""行句柄（RowHandle）与校验错误。

一句柄一行：值语义（transform / validator / default）、历史栈、状态点、
读写状态、错误提示全部挂在这里；FormView 只负责摆放与组装。

历史栈的纯逻辑在 :mod:`.row_history`（无 Qt 依赖，可单测），这里只做接线。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, ClassVar, Optional

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QLabel, QPushButton, QWidget

from .row_format import RowAccess, RowSpec, ValidationMode
from .row_history import (
    ACTION_MODIFY, ACTION_RESTORE, RowHistory, values_equal,
)

if TYPE_CHECKING:
    from ..tag_label import TagLabel
    from .form_view import FormView


#: 「未传参 / 读取失败」哨兵
_UNSET = object()


# ----------------------------------------------------------------------
class ValidationError(ValueError):
    """get_value / collect 时 transform 或 validator 失败。"""


# ----------------------------------------------------------------------
class RowHandle(QObject):
    """一行的稳定句柄。

    - ``row_id``：自增、不复用；
    - 值语义：``set_value`` 只接 raw，``get_value`` 返回 transform 之后的
      submitted；``initial_value()`` 为 add_row(value=) 的原始值（跳过校验）。
    - 历史栈：每行独立，元素为 ``(raw, action)``；只 undo 不 redo。
    - 状态点：描述 label 就是 TagLabel ——
        绿点   = 当前值等于**显式** default（没配 default 的行永远不会绿）；
        淡蓝点 = 与「上次提交基线」不同（标脏），且当前值不是显式默认值；
        无点   = 两者都不成立。
      基线 ``_baseline_raw`` 在 add_row 时等于初始值，之后由 ``mark_clean()``
      前移到最近一次成功提交（flush）的值 —— 这就是「与 flush 前不同」的判据。
    - 恢复默认按钮：可用性由「当前值 != 默认值」实时决定，历史回退 / undo
      之后同样会刷新。
    - 读写状态：``access`` ∈ RowAccess；DISABLED 会同时点亮锁图标。
    """

    # 状态点样式（只画圆点，胶囊背景设为透明）
    _STATE_DEFAULT: ClassVar[dict[str, str]] = {
        "text": "", "bg": "transparent", "dot": "#34A853",
    }
    _STATE_DIRTY: ClassVar[dict[str, str]] = {
        "text": "", "bg": "transparent", "dot": "#81D4FA",
    }
    # 无点：保留同尺寸的透明圆点，状态切换时描述文字不跳位
    _STATE_NONE: ClassVar[dict[str, str]] = {
        "text": "", "bg": "transparent", "dot": "transparent",
    }

    def __init__(
            self,
            form: Optional["FormView"],
            row_id: int,
            ident: Optional[str],
            *,
            spec: RowSpec,
            label_widget: Optional[QWidget],
            field_widget: QWidget,
            container: QWidget,
            read_fn: Callable[[QWidget], Any],
            write_fn: Callable[[QWidget, Any], None],
            initial_raw: Any = None,
            default_raw: Any = None,
            natural_default: Any = None,
            transform: Optional[Callable[[Any], Any]] = None,
            validator: Optional[Callable[[Any], tuple]] = None,
            validation_mode: ValidationMode = ValidationMode.ON_COMMIT,
            changed_target: Optional[QObject] = None,
            changed_signal: Optional[str] = None,
            reset_btn: Optional[QPushButton] = None,
            lock_icon: Optional[QLabel] = None,
            error_label: Optional[QLabel] = None,
            badge: Optional["TagLabel"] = None,
            label_text: str = "",
            is_value_row: bool = True,
    ):
        super().__init__(form if isinstance(form, QObject) else None)
        self._form = form
        self.row_id = row_id
        self.ident = ident
        self.spec = spec
        self._label_widget = label_widget
        self._field = field_widget
        self._container = container
        self._read_fn = read_fn
        self._write_fn = write_fn
        self._initial_raw = initial_raw
        self._baseline_raw = initial_raw   # 最近一次提交（flush）的值
        self._default_raw = default_raw
        self._natural_default = natural_default
        self._transform = transform
        self._validator = validator
        self.validation_mode = validation_mode
        self._changed_target = changed_target or field_widget
        self._changed_signal = changed_signal
        self._reset_btn = reset_btn
        self._lock_icon = lock_icon
        self._error_label = error_label
        self._badge = badge
        self._badge_state: Optional[str] = None
        self._label_text = label_text
        self.is_value_row = is_value_row

        self._access = spec.access
        self._access_reason: Optional[str] = None
        self._grid_row = 0  # 由 FormView 设置

        # 历史栈（纯逻辑在 row_history.RowHistory）
        self._history = RowHistory(initial_raw)

        if reset_btn is not None:
            reset_btn.setVisible(default_raw is not None)
            reset_btn.setToolTip("恢复默认值")
            reset_btn.clicked.connect(self.restore_default)
        if error_label is not None:
            error_label.setText("")  # 常驻占位：保持可见但空

        if self._lock_icon is not None:
            self._lock_icon.setVisible(False)

        self._bind_change_signals()
        if validation_mode is ValidationMode.ON_COMMIT and self.is_value_row:
            self._changed_target.installEventFilter(self)
        self.refresh_state()

    # ------------------------------------------------------------------
    # 值
    # ------------------------------------------------------------------
    def _read_raw(self) -> Any:
        return self._read_fn(self._field)

    def _write_raw(self, raw: Any) -> None:
        self._write_fn(self._field, raw)

    def raw(self) -> Any:
        """当前 raw（不做 transform / validator）。"""
        return self._read_raw()

    def get_value(self) -> Any:
        """读取并转换当前 raw。失败时抛 ValidationError 且自动 set_error。"""
        raw = self._read_raw()
        try:
            submitted = self._transform(raw) if self._transform else raw
        except (ValueError, TypeError, KeyError) as exc:
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

    def validate(self) -> bool:
        """不抛错版本的校验：返回是否通过，并同步错误态。"""
        try:
            self.get_value()
        except ValidationError:
            return False
        return True

    def submitted_from_raw(self, raw: Any) -> Any:
        """回滚用：走 transform 跳过 validator（初始值可信）。"""
        return self._transform(raw) if self._transform else raw

    def set_value(self, raw: Any) -> None:
        """只接 raw，transform 单方向；作为「修改」入历史栈。"""
        self._push_history(raw, ACTION_MODIFY)
        self._write_raw(raw)
        self.refresh_state()

    def initial_value(self) -> Any:
        return self._initial_raw

    def default_value(self) -> Any:
        return self._default_raw

    def baseline_value(self) -> Any:
        """最近一次成功提交（flush）时的 raw。"""
        return self._baseline_raw

    def mark_clean(self, raw: Any = _UNSET) -> None:
        """提交成功：把基线前移到当前值（或显式给定的值）。

        提交之后，状态点重新以新基线判断「是否已修改」——
        这就是 flush 后同值不再标脏的原因。
        """
        current = raw if raw is not _UNSET else self._current_raw()
        if current is not _UNSET:
            self._baseline_raw = current
        self.refresh_state()

    def is_dirty(self) -> bool:
        """当前值是否与最近一次提交（flush）不同。"""
        current = self._current_raw()
        if current is _UNSET:
            return False
        return not values_equal(current, self._baseline_raw)

    def is_default(self) -> bool:
        """当前值是否等于显式 ``default``（未配置 default 的行恒为 False）。"""
        has_default, target = self._explicit_default()
        if not has_default:
            return False
        current = self._current_raw()
        if current is _UNSET:
            return False
        return values_equal(current, target)

    def _explicit_default(self) -> tuple[bool, Any]:
        """(是否配置了 default, default)：只看调用方显式传入的 default。"""
        return self._default_raw is not None, self._default_raw

    def _restore_target(self) -> tuple[bool, Any]:
        """(是否有可恢复目标, 目标值)：显式 default 优先，否则自然默认。"""
        if self._default_raw is not None:
            return True, self._default_raw
        if self._natural_default is not None:
            return True, self._natural_default
        return False, None

    def restore_default(self) -> None:
        """优先恢复到显式 default；否则回退到格式的自然默认；都无 → no-op。"""
        has_target, target = self._restore_target()
        if not has_target:
            return
        self._push_history(target, ACTION_RESTORE)
        self._write_raw(target)
        self.refresh_state()

    # ------------------------------------------------------------------
    # 错误提示
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # 历史
    # ------------------------------------------------------------------
    def _push_history(self, raw: Any, action: str = ACTION_MODIFY) -> bool:
        """值或 action 任一变化才入栈，返回是否真的入栈。"""
        return self._history.push(raw, action)

    def history(self) -> list[Any]:
        """含当前值，[-1] 即当前 raw。"""
        return self._history.values()

    def history_action(self) -> str:
        """当前值因何而来：``init`` / ``modify`` / ``restore``。"""
        return self._history.current_action()

    def can_undo(self) -> bool:
        return self._history.can_undo()

    def undo(self) -> None:
        """历史回退：值 / 状态点 / 恢复默认按钮一起刷新。"""
        entry = self._history.undo()
        if entry is None:
            return
        self._write_raw(entry[0])
        self.refresh_state()

    def clear_history(self) -> None:
        current = self._current_raw()
        self._history.reset(_UNSET if current is _UNSET else current)
        self.refresh_state()

    def can_redo(self) -> bool:
        """留位：本期不实现 redo，恒 False。"""
        return self._history.can_redo()

    # ------------------------------------------------------------------
    # 状态点
    # ------------------------------------------------------------------
    def _bind_change_signals(self) -> None:
        """把「值变化」接到状态点刷新（+ ON_CHANGE 校验）。"""
        if not self.is_value_row:
            return
        target = self._changed_target
        names = [self._changed_signal] if self._changed_signal else []
        for name in names:
            signal = getattr(target, name, None)
            if signal is not None:
                signal.connect(self._on_field_changed)
                return
        # RADIO / 自定义控件：逐个按钮或逐个候选信号
        buttons = getattr(target, "_radio_buttons", None)
        if buttons:
            for rb, _ in buttons:
                rb.toggled.connect(self._on_field_changed)
            return
        for name in ("textChanged", "valueChanged", "currentIndexChanged",
                     "toggled", "keySequenceChanged"):
            signal = getattr(target, name, None)
            if signal is not None:
                signal.connect(self._on_field_changed)
                return

    def _on_field_changed(self, *args: object) -> None:
        self.refresh_state()
        if self.validation_mode is ValidationMode.ON_CHANGE:
            self.validate()

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: N802  Qt 覆盖
        """ON_COMMIT：失焦 / 回车时校验。"""
        if self.validation_mode is not ValidationMode.ON_COMMIT:
            return False
        if obj is self._changed_target:
            if event.type() == QEvent.Type.FocusOut:
                self.validate()
            elif (event.type() == QEvent.Type.KeyPress
                  and isinstance(event, QKeyEvent)
                  and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)):
                self.validate()
        return False

    def _current_raw(self) -> Any:
        """读当前 raw；控件已销毁等竞态返回 ``_UNSET``。"""
        try:
            return self._read_raw()
        except Exception:  # 控件销毁竞态
            return _UNSET

    def _refresh_badge(self) -> None:
        """状态点规则（纯值比较，不依赖历史动作）：

            - 当前值 == 默认值          → 绿点（若同时与基线不同，tooltip 注明）
            - 当前值 != 提交基线        → 淡蓝点（已修改 / 标脏）
            - 其他                      → 无点
        """
        if self._badge is None or not self.is_value_row:
            return

        current = self._current_raw()
        if current is _UNSET:
            return

        dirty = not values_equal(current, self._baseline_raw)
        has_default, target = self._explicit_default()
        at_default = has_default and values_equal(current, target)

        if at_default:
            self._show_badge(
                self._STATE_DEFAULT, "default",
                "默认值（已修改）" if dirty else "默认值",
            )
        elif dirty:
            self._show_badge(self._STATE_DIRTY, "dirty", "已修改")
        else:
            self._show_badge(self._STATE_NONE, None, "")

    def _show_badge(
            self, spec: dict[str, str], state: Optional[str], tooltip: str,
    ) -> None:
        badge = self._badge
        if badge is None:
            return
        self._badge_state = state
        badge.setTags([dict(spec)])
        badge.setToolTip(tooltip)
        badge.setVisible(True)

    def _refresh_reset_btn(self) -> None:
        """恢复默认按钮：当前值 == 默认值（或没有默认值）时禁用。"""
        btn = self._reset_btn
        if btn is None:
            return
        has_default, target = self._explicit_default()
        if not has_default:
            btn.setEnabled(False)
            btn.setToolTip("没有默认值可恢复")
            return
        current = self._current_raw()
        if current is _UNSET:
            return
        changed = not values_equal(current, target)
        btn.setEnabled(changed)
        btn.setToolTip("恢复默认值" if changed else "当前已是默认值")

    # ------------------------------------------------------------------
    # 读写状态
    # ------------------------------------------------------------------
    def bind_grid_row(self, row: int) -> None:
        """由 FormView 装配 / 删除行时调用。"""
        self._grid_row = int(row)

    def refresh_state(self) -> None:
        """刷新状态点 + 恢复默认按钮（任何值 / 历史 / 基线变化后调用）。"""
        self._refresh_badge()
        self._refresh_reset_btn()

    @property
    def badge_state(self) -> Optional[str]:
        """``"default"``（绿点）/ ``"dirty"``（淡蓝点）/ ``None``（无点）。"""
        return self._badge_state

    @property
    def badge_label(self) -> Optional["TagLabel"]:
        """承载状态点的 TagLabel（行内即左侧描述 label）。"""
        return self._badge

    @property
    def reset_btn(self) -> Optional[QPushButton]:
        """恢复默认按钮（无 default / reset_position=None 时为 None）。"""
        return self._reset_btn

    @property
    def lock_icon(self) -> Optional[QLabel]:
        return self._lock_icon

    def set_access(self, access: RowAccess, reason: Optional[str] = None) -> None:
        """切换 READWRITE / READONLY / DISABLED。"""
        self._access = access
        self._access_reason = reason
        if self._form is not None:
            self._form.apply_access(self, access, reason)
        else:  # pragma: no cover - 游离句柄
            self._field.setEnabled(access is not RowAccess.DISABLED)

    def set_enabled(self, enabled: bool, reason: Optional[str] = None) -> None:
        """便捷糖：``False`` ≡ DISABLED，``True`` ≡ READWRITE。"""
        self.set_access(
            RowAccess.READWRITE if enabled else RowAccess.DISABLED, reason
        )

    @property
    def access(self) -> RowAccess:
        return self._access

    @property
    def access_reason(self) -> Optional[str]:
        """最近一次切换读写状态时给的原因（锁图标 tooltip）。"""
        return self._access_reason

    @property
    def enabled(self) -> bool:
        return self._access is not RowAccess.DISABLED

    # ------------------------------------------------------------------
    def focus(self) -> None:
        self._field.setFocus(Qt.FocusReason.OtherFocusReason)

    def remove(self) -> None:
        if self._form is not None:
            self._form.remove_handle(self)

    # ------------------------------------------------------------------
    @property
    def control(self):
        return self.spec.control

    @property
    def grid_row(self) -> int:
        return self._grid_row

    @property
    def container(self) -> QWidget:
        return self._container

    @property
    def field(self) -> QWidget:
        return self._field

    @property
    def label_widget(self) -> Optional[QWidget]:
        return self._label_widget

    @property
    def error_text(self) -> str:
        return self._error_label.text() if self._error_label is not None else ""

    @property
    def label_text(self) -> str:
        return self._label_text


__all__ = [
    "RowHandle",
    "ValidationError",
]
