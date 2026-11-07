"""一个设置项的行控件（标签 + 编辑控件 + 状态）。

这仍然是**被动视图**：它不认识配置器、不发写请求，只把 binder 的状态画出来，
把用户改动交给 binder。所有「什么时候写盘」的决定都在视图外部。

行的结构（从左到右）::

    ● 标签 [高级] [默认]      [编辑控件]   ↺   ⚠
      说明文字
      [group]key · type · file=…

* ``●`` 是 dirty 徽标（``#SettingRowDirtyBadge``）；
* ``↺`` 是「重置为默认」（只改 UI 态，提交时才算数）；
* ``⚠`` 常驻占位、只在有错误时显示字符，**不撑行高**；
* 禁用的行统一走「控件禁用 + 原因 tooltip」，reason 枚举来自
  :mod:`albuswall.ui.setting.contract`；
* **值类别 → 控件**由 :mod:`albuswall.ui.setting.editors` 的注册表决定，
  本模块不再按 kind 写 if/elif。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import strings
from .binder import LeafBinder
from .contract import DisabledReason, SettingWidget
from .editors import create_editor, format_editor_text, looks_like_file

__all__ = ["SettingRow"]


class SettingRow(QWidget):
    """一个叶子行；一个 binder 对应一个 row（重建时会换 row，binder 不变）。"""

    def __init__(self, binder: LeafBinder, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.binder = binder
        self.leaf: dict[str, Any] = binder.leaf
        self.key: str = binder.key
        self.setObjectName("SettingRow")
        self.setProperty("settingKey", self.key)
        self._control_kind = self.leaf["widget"]

        self._build_ui()
        self._build_control()
        self._sync_control()
        self._refresh_state()
        self.binder.add_listener(self._on_binder_event)

    # ------------------------------------------------------------------
    # 构建
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        outer = QHBoxLayout(self)
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(8)

        text_box = QVBoxLayout()
        text_box.setContentsMargins(0, 0, 0, 0)
        text_box.setSpacing(1)

        title_line = QHBoxLayout()
        title_line.setContentsMargins(0, 0, 0, 0)
        title_line.setSpacing(6)

        self._dirty_badge = QLabel("●", self)
        self._dirty_badge.setObjectName("SettingRowDirtyBadge")
        self._dirty_badge.setFixedWidth(10)
        self._dirty_badge.setToolTip(strings.TOOLTIP_DIRTY)

        self._advanced_badge = QLabel(strings.BADGE_ADVANCED, self)
        self._advanced_badge.setObjectName("SettingRowAdvancedBadge")
        self._advanced_badge.setVisible(bool(self.leaf.get("advanced")))

        self._default_badge = QLabel(strings.BADGE_DEFAULT, self)
        self._default_badge.setObjectName("SettingRowDefaultBadge")
        self._default_badge.setToolTip(strings.TOOLTIP_DEFAULT)

        self._lock_icon = QLabel("🔒", self)
        self._lock_icon.setObjectName("SettingRowLockIcon")
        self._lock_icon.setFixedWidth(16)
        self._lock_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._lock_icon.setVisible(not self.binder.editable)

        self._label = QLabel(
            str(self.leaf.get("label") or self.leaf.get("name")), self,
        )
        self._label.setObjectName("SettingRowLabel")
        self._label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse,
        )
        title_line.addWidget(self._dirty_badge)
        title_line.addWidget(self._label)
        title_line.addWidget(self._advanced_badge)
        title_line.addWidget(self._default_badge)
        title_line.addWidget(self._lock_icon)
        title_line.addStretch(1)
        text_box.addLayout(title_line)

        self._description = QLabel(str(self.leaf.get("description") or ""), self)
        self._description.setObjectName("SettingRowDescription")
        self._description.setWordWrap(True)
        self._description.setVisible(bool(self.leaf.get("description")))
        text_box.addWidget(self._description)

        self._hint = QLabel(self)
        self._hint.setObjectName("SettingRowHint")
        self._hint.setWordWrap(True)
        self._hint.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse,
        )
        text_box.addWidget(self._hint)

        outer.addLayout(text_box, 1)

        self._control_host = QWidget(self)
        control_layout = QHBoxLayout(self._control_host)
        control_layout.setContentsMargins(0, 0, 0, 0)
        control_layout.setSpacing(4)
        self._control_layout = control_layout
        self._control_host.setMinimumWidth(180)
        self._control_host.setMaximumWidth(460)
        outer.addWidget(self._control_host, 0)

        self._reset_button = QToolButton(self)
        self._reset_button.setObjectName("SettingRowResetButton")
        self._reset_button.setText("↺")
        self._reset_button.setToolTip(strings.TOOLTIP_RESET)
        self._reset_button.setAutoRaise(True)
        self._reset_button.clicked.connect(self._on_reset_clicked)
        outer.addWidget(self._reset_button, 0)

        # 固定尺寸的占位图标：没有错误时也占位，保证行高不变
        self._error_icon = QLabel("", self)
        self._error_icon.setObjectName("SettingRowErrorIcon")
        self._error_icon.setFixedSize(16, 16)
        self._error_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(self._error_icon, 0)

    def _build_control(self) -> None:
        """按值类别造编辑器（具体控件由 :mod:`editors` 决定）。"""
        self._editor = create_editor(self.leaf, self._control_host)
        self._control = self._editor.widget
        self._control_layout.addWidget(self._control, 1)
        self._editor.on_change(self._on_editor_changed)

        if self._control_kind == SettingWidget.PATH.value:
            self._browse_button = QToolButton(self._control_host)
            self._browse_button.setObjectName("SettingRowBrowseButton")
            self._browse_button.setText("…")
            self._browse_button.setToolTip(strings.TOOLTIP_BROWSE)
            self._browse_button.clicked.connect(self._on_browse_clicked)
            self._control_layout.addWidget(self._browse_button, 0)
        else:
            self._browse_button = None

    # ------------------------------------------------------------------
    # 编辑器 → binder
    # ------------------------------------------------------------------
    def _on_editor_changed(self, value: Any) -> None:
        self.binder.set_from_ui(value)
        self._refresh_error_icon()

    def _on_browse_clicked(self) -> None:
        current = format_editor_text(self.binder.value)
        if looks_like_file(current):
            chosen, _filter = QFileDialog.getOpenFileName(
                self, strings.PICK_FILE, current,
            )
        else:
            chosen = QFileDialog.getExistingDirectory(
                self, strings.PICK_DIRECTORY, current,
            )
        if chosen:
            self.binder.set_from_ui(chosen)
            self._sync_control()

    # ------------------------------------------------------------------
    # binder → 编辑器
    # ------------------------------------------------------------------
    def _sync_control(self) -> None:
        self._editor.set_value(self.binder.value)

    # ------------------------------------------------------------------
    # 状态刷新
    # ------------------------------------------------------------------
    def _on_binder_event(self, _binder: LeafBinder, event: str) -> None:
        if event in ("external", "reset"):
            self._sync_control()
        if event in ("error", "external", "reset"):
            self._refresh_error_icon()
        self._refresh_state()

    def _refresh_state(self) -> None:
        binder = self.binder
        self._dirty_badge.setVisible(binder.dirty)
        self._default_badge.setVisible(not binder.present)
        self._label.setProperty("usingDefault", not binder.present)
        self._label.style().unpolish(self._label)
        self._label.style().polish(self._label)
        self._reset_button.setVisible(binder.editable)
        self._reset_button.setEnabled(binder.editable and not binder.is_default)
        self._lock_icon.setVisible(not binder.editable)
        self._editor.set_editable(binder.editable)
        self._update_tooltip()
        self._update_hint()
        self._refresh_error_icon()

    def _update_tooltip(self) -> None:
        binder = self.binder
        if binder.editable:
            self._set_row_tooltip("")
            return
        parts = [binder.reason.text]
        if binder.reason_detail:
            parts.append(binder.reason_detail)
        self._set_row_tooltip(" — ".join(part for part in parts if part))

    def _set_row_tooltip(self, text: str) -> None:
        self.setToolTip(text)
        self._label.setToolTip(text)
        self._control.setToolTip(text)
        self._lock_icon.setToolTip(text)

    def _update_hint(self) -> None:
        leaf = self.leaf
        parts = [str(leaf.get("path") or self.key)]
        type_text = str(leaf.get("type") or "")
        if type_text:
            parts.append(type_text)
        target = leaf.get("target_file")
        parts.append(
            f"file={Path(target).name}" if target else strings.HINT_NO_TARGET,
        )
        if not leaf.get("present"):
            parts.append(strings.HINT_USING_DEFAULT)
        if leaf.get("interpolated"):
            parts.append(strings.HINT_INTERPOLATED)
        if leaf.get("read_error"):
            parts.append(
                strings.HINT_READ_ERROR.format(detail=leaf["read_error"]),
            )
        self._hint.setText(" · ".join(parts))

    def _refresh_error_icon(self) -> None:
        message = self._editor.error or self.binder.error
        if message is None and self.binder.reason is DisabledReason.READ_ERROR:
            detail = self.binder.reason_detail
            message = (
                strings.HINT_READ_ERROR.format(detail=detail)
                if detail else strings.READ_ERROR_FALLBACK
            )
        if message:
            self._error_icon.setText("⚠")
            self._error_icon.setToolTip(message)
            self._error_icon.setAccessibleName(message)
        else:
            self._error_icon.setText("")
            self._error_icon.setToolTip("")
            self._error_icon.setAccessibleName("")
        if self.property("hasError") is not bool(message):
            self.setProperty("hasError", bool(message))
            self.style().unpolish(self)
            self.style().polish(self)

    def _on_reset_clicked(self) -> None:
        self.binder.reset_to_default()

    # ------------------------------------------------------------------
    # 供视图 / 测试用
    # ------------------------------------------------------------------
    @property
    def control(self) -> QWidget:
        """当前编辑控件（``QLineEdit`` / ``QSpinBox`` / …）。"""
        return self._control

    @property
    def editor(self) -> Any:
        return self._editor

    @property
    def control_kind(self) -> str:
        return self._control_kind

    @property
    def error_icon(self) -> QLabel:
        return self._error_icon

    @property
    def reset_button(self) -> QToolButton:
        return self._reset_button

    @property
    def local_error(self) -> str | None:
        """编辑器本地的错误（例如 JSON 还没写对时的解析信息）。"""
        return self._editor.error

    def detach(self) -> None:
        """视图重建时解绑监听，避免 binder 持有已删除的 row。"""
        self.binder.remove_listener(self._on_binder_event)

