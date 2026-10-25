#
"""设置页里的「按键映射」视图。

内容来自两处**纯数据**来源，本模块只负责把它们画出来：

* ``registry.iter_actions()`` —— 全部可映射动作（内置 + 插件登记）；
* ``store.entries()`` —— 每个动作当前生效的按键 / 默认值 / 是否被覆盖。

界面结构::

    KeymapPage
    ├─ 说明 + 总开关
    ├─ 搜索框 + 「全部恢复默认」
    ├─ 状态行（写入文件 / 冲突 / 匹配数量）
    └─ KeymapRow × N                 ← 动作 ↔ 按键 的完整列表
         ├─ 动作名 + 动作 id
         ├─ QKeySequenceEdit         ← 点一下直接按新键
         └─ ↺ 恢复默认

搜索对「动作名 / 动作 id / 分组 / 当前按键 / 默认按键 / 说明」做全词匹配
（空格分隔的多个词必须全部命中），只隐藏不匹配的行，不重建控件。

写回走 :mod:`store`，因此修改**立即**生效：``dynamic.keymap`` 的观察者会
让 ``KeymapManager`` 重建按键表，不需要重启。
"""

from __future__ import annotations

from PySide6.QtCore import QCoreApplication, Qt, Signal
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QCheckBox,
    QGridLayout,
    QHBoxLayout,
    QKeySequenceEdit,
    QLabel,
    QLineEdit,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import store
from .actions import ensure_builtin_actions
from .keys import display_text

__all__ = [
    "PAGE_DESCRIPTION",
    "PAGE_ID",
    "PAGE_ORDER",
    "PAGE_TITLE",
    "KeymapPage",
    "KeymapRow",
    "build_keybindings",
    "register_keymap_page",
]

#: 翻译上下文（本模块没有 QObject 的固定上下文需求时用它）
_TR_CONTEXT = "KeymapPage"

#: 设置页注册信息（``setting/main.py`` 用它登记自定义页）
PAGE_ID = "keybindings"
PAGE_TITLE = "Key bindings"
PAGE_ORDER = 800
PAGE_DESCRIPTION = (
    "Every action of the application and the key it is bound to. Click a "
    "key field and press the new key, or clear it to unbind. Changes are "
    "saved immediately."
)


def _tr(text: str) -> str:
    return QCoreApplication.translate(_TR_CONTEXT, text)


# ══════════════════════════════════════════════════
# 单行
# ══════════════════════════════════════════════════
class KeymapRow(QWidget):
    """一条「动作 ↔ 按键」：动作名 + 按键编辑框 + 恢复默认。"""

    changed = Signal()

    def __init__(
            self,
            binding: store.Binding,
            parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("SettingKeymapRow")
        self._binding = binding
        self._tokens: list[str] = []

        self.label = QLabel(_tr(binding.label), self)
        self.label.setObjectName("SettingRowLabel")
        self.label.setToolTip(binding.id)

        self.identifier = QLabel(binding.id, self)
        self.identifier.setObjectName("SettingKeymapId")

        label_box = QWidget(self)
        label_layout = QHBoxLayout(label_box)
        label_layout.setContentsMargins(0, 0, 0, 0)
        label_layout.setSpacing(8)
        label_layout.addWidget(self.label)
        label_layout.addWidget(self.identifier)
        label_layout.addStretch()

        self.editor = QKeySequenceEdit(self)
        self.editor.setObjectName("SettingRowEditor")
        # 只允许单段组合键：多段序列需要待定状态机，分发层不支持
        self.editor.setMaximumSequenceLength(1)
        self.editor.setClearButtonEnabled(True)
        self.editor.setMinimumWidth(180)
        self.editor.setMaximumWidth(300)

        self.reset_button = QToolButton(self)
        self.reset_button.setObjectName("SettingRowResetButton")
        self.reset_button.setAutoRaise(True)
        self.reset_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reset_button.clicked.connect(self._on_reset)

        self.description = QLabel(self)
        self.description.setObjectName("SettingRowDescription")
        self.description.setWordWrap(True)

        self.error = QLabel("", self)
        self.error.setObjectName("SettingRowError")
        self.error.setWordWrap(True)
        self.error.hide()

        grid = QGridLayout(self)
        grid.setContentsMargins(0, 3, 0, 3)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(2)
        grid.setColumnStretch(0, 1)
        grid.addWidget(label_box, 0, 0)
        grid.addWidget(self.editor, 0, 1)
        grid.addWidget(self.reset_button, 0, 2)
        grid.addWidget(self.description, 1, 0, 1, 3)
        grid.addWidget(self.error, 2, 0, 1, 3)

        self._apply_binding(binding)
        self.editor.editingFinished.connect(self._on_commit)

    # ------------------------------------------------------------------ #
    # 数据 → 界面
    # ------------------------------------------------------------------ #
    def _apply_binding(self, binding: store.Binding) -> None:
        self._binding = binding
        blocked = self.editor.blockSignals(True)
        try:
            self.editor.setKeySequence(_to_key_sequence(binding.keys))
        finally:
            self.editor.blockSignals(blocked)

        self.description.setText(self._describe(binding))
        self.reset_button.setToolTip(_reset_tooltip(binding))
        self.reset_button.setVisible(True)
        if not binding.is_known:
            self.editor.setEnabled(False)
            self.label.setToolTip(
                _tr("Unknown action (not registered anymore): {id}").format(
                    id=binding.id
                )
            )

        self._tokens = _search_tokens(binding)
        self._sync_visual_state()

    def _describe(self, binding: store.Binding) -> str:
        parts: list[str] = []
        if binding.description:
            parts.append(_tr(binding.description))
        if binding.is_known:
            default = display_text(binding.default_keys) or _tr("(unbound)")
            parts.append(_tr("Default: {value}").format(value=default))
        if binding.overridden:
            parts.append(_tr("customized"))
        return "   ".join(parts)

    def _sync_visual_state(self) -> None:
        if not self._binding.is_known:
            self.reset_button.setText(_tr("Remove"))
            return
        self.reset_button.setText("\u21ba")  # ↺
        self.reset_button.setEnabled(self._binding.overridden)
        self.error.hide()
        self.error.setText("")

    def _show_error(self, message: str) -> None:
        self.error.setText(message)
        self.error.show()

    # ------------------------------------------------------------------ #
    # 编辑
    # ------------------------------------------------------------------ #
    def _on_commit(self) -> None:
        """用户按下了新键（或清空了编辑框）→ 写回子配置文件。"""
        sequence = self.editor.keySequence()
        text = sequence.toString(QKeySequence.SequenceFormat.PortableText)

        if sequence.count() > 1:
            self._reload()
            self._show_error(
                _tr("Only single-key shortcuts are supported; "
                    "{value} has more than one key.").format(
                    value=sequence.toString(
                        QKeySequence.SequenceFormat.NativeText),
                )
            )
            return

        try:
            store.set_binding(self._binding.id, text)
        except ValueError as exc:
            self._reload()
            self._show_error(str(exc))
            return

        self._reload()
        self.changed.emit()

    def _on_reset(self) -> None:
        store.reset_binding(self._binding.id)
        self._reload()
        self.changed.emit()

    def _reload(self) -> None:
        """从 store 重新读这一行（保持控件对象，不重建）。"""
        for binding in store.entries():
            if binding.id == self._binding.id:
                self._apply_binding(binding)
                return

    # ------------------------------------------------------------------ #
    # 搜索
    # ------------------------------------------------------------------ #
    def contains(self, tokens: list[str]) -> bool:
        """``tokens`` 是否全部命中本行（空 tokens = 命中）。"""
        if not tokens:
            return True
        haystack = " ".join(self._tokens)
        return all(token in haystack for token in tokens)

    def apply_filter(self, tokens: list[str]) -> bool:
        matched = self.contains(tokens)
        self.setVisible(matched)
        return matched


# ══════════════════════════════════════════════════
# 页面
# ══════════════════════════════════════════════════
class KeymapPage(QWidget):
    """设置页里的按键映射页（构造即建好全部行）。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("SettingKeymapPage")
        ensure_builtin_actions()

        self._rows: list[KeymapRow] = []

        self._build_ui()
        self.refresh()

    # ------------------------------------------------------------------ #
    # 结构
    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 4)
        layout.setSpacing(8)

        self.intro = QLabel(_tr(PAGE_DESCRIPTION), self)
        self.intro.setObjectName("SettingRowDescription")
        self.intro.setWordWrap(True)
        layout.addWidget(self.intro)

        self.enabled_box = QCheckBox(_tr("Enable key bindings"), self)
        self.enabled_box.setObjectName("SettingRowEditor")
        self.enabled_box.setToolTip(
            _tr("When off, the configured keys are ignored (the built-in "
                "button shortcuts still work).")
        )
        self.enabled_box.toggled.connect(self._on_enabled_toggled)
        layout.addWidget(self.enabled_box)

        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.setSpacing(8)

        self.search = QLineEdit(self)
        self.search.setObjectName("SettingKeymapSearch")
        self.search.setClearButtonEnabled(True)
        self.search.setPlaceholderText(_tr("Search actions or keys…"))
        self.search.textChanged.connect(lambda _text: self._apply_filter())
        toolbar.addWidget(self.search, 1)

        self.reset_all_button = QPushButton(_tr("Reset all"), self)
        self.reset_all_button.setObjectName("SettingRowActionButton")
        self.reset_all_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reset_all_button.clicked.connect(self._on_reset_all)
        toolbar.addWidget(self.reset_all_button, 0)
        layout.addLayout(toolbar)

        self.status = QLabel("", self)
        self.status.setObjectName("SettingRowDescription")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        container = QWidget(self)
        self.rows_layout = QVBoxLayout(container)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(0)
        layout.addWidget(container)

    # ------------------------------------------------------------------ #
    # 数据 → 行
    # ------------------------------------------------------------------ #
    def refresh(self) -> None:
        """按最新动作表 / 配置重建全部行（搜索词与开关状态保留）。"""
        ensure_builtin_actions()
        for row in self._rows:
            row.setParent(None)
            row.deleteLater()
        self._rows.clear()

        for binding in store.entries():
            row = KeymapRow(binding, self)
            row.changed.connect(self._on_row_changed)
            self.rows_layout.addWidget(row)
            self._rows.append(row)

        blocked = self.enabled_box.blockSignals(True)
        try:
            self.enabled_box.setChecked(store.is_enabled())
        finally:
            self.enabled_box.blockSignals(blocked)

        self._apply_filter()

    # ------------------------------------------------------------------ #
    # 交互
    # ------------------------------------------------------------------ #
    def _on_enabled_toggled(self, checked: bool) -> None:
        store.set_enabled(bool(checked))
        self._apply_filter()

    def _on_reset_all(self) -> None:
        store.reset_all()
        self.refresh()

    def _on_row_changed(self) -> None:
        self._apply_filter()

    # ------------------------------------------------------------------ #
    # 搜索 / 状态
    # ------------------------------------------------------------------ #
    def _apply_filter(self) -> None:
        tokens = _search_tokens_from_text(self.search.text())
        shown = sum(1 for row in self._rows if row.apply_filter(tokens))
        self._update_status(shown)

    def _update_status(self, shown: int) -> None:
        total = len(self._rows)
        parts: list[str] = [
            _tr("{shown} of {total} actions shown").format(
                shown=shown, total=total)
        ]

        conflicts = store.conflicts()
        if conflicts:
            details = "; ".join(
                f"{display_text(keys)} ({', '.join(actions)})"
                for keys, actions in sorted(conflicts.items())
            )
            parts.append(
                _tr("Key conflicts: {details}").format(details=details)
            )

        path = store.keymap_file()
        parts.append(
            _tr("Saved to {path}").format(path=str(path))
            if path is not None
            else _tr("Saved in memory only (the configuration system is "
                     "not ready yet).")
        )
        self.status.setText("  ·  ".join(parts))


# ══════════════════════════════════════════════════
# 辅助
# ══════════════════════════════════════════════════
def _to_key_sequence(portable_text: str) -> QKeySequence:
    text = str(portable_text or "").strip()
    if not text:
        return QKeySequence()
    return QKeySequence.fromString(
        text, QKeySequence.SequenceFormat.PortableText,
    )


def _search_tokens(binding: store.Binding) -> list[str]:
    fields = [
        binding.id,
        # 源文 + 当前译文的两种写法都参与搜索：中文界面下搜「收藏」也要命中
        binding.label,
        _tr(binding.label),
        binding.group,
        binding.description,
        _tr(binding.description),
        binding.keys,
        display_text(binding.keys),
        binding.default_keys,
        display_text(binding.default_keys),
    ]
    return " ".join(part for part in fields if part).casefold().split()


def _search_tokens_from_text(text: str) -> list[str]:
    return str(text or "").casefold().split()


def _reset_tooltip(binding: store.Binding) -> str:
    if not binding.is_known:
        return _tr("Remove this leftover binding")
    default = display_text(binding.default_keys) or _tr("(unbound)")
    return _tr("Reset to default: {value}").format(value=default)


# ══════════════════════════════════════════════════
# 注册
# ══════════════════════════════════════════════════
def build_keybindings(host: QWidget) -> None:
    """设置页自定义页的构建回调（第一次展开时才跑）。"""
    page = KeymapPage(host)
    layout = host.layout()
    if layout is None:  # 被独立调用时自己补一个布局
        layout = QVBoxLayout(host)
        layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(page)


def register_keymap_page() -> None:
    """把本页登记进设置页注册表（幂等）。"""
    from albuswall.ui.builtin.setting.pages import PageSpec, register_page

    register_page(
        PageSpec(
            id=PAGE_ID,
            title=PAGE_TITLE,
            order=PAGE_ORDER,
            description=PAGE_DESCRIPTION,
            build=build_keybindings,
        ),
        replace=True,
    )
