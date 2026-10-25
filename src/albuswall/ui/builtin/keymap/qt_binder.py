"""Qt 绑定层：把内核里的键装成 ``QShortcut``，并让焦点控件自己消费它该消费的键。

只有本模块与 ``focus`` 碰 Qt。分发规则全部委托给
:class:`~albuswall.ui.builtin.keymap.dispatch.DispatchPolicy`。
"""
from __future__ import annotations

import itertools
import weakref
from collections.abc import Callable
from functools import partial
from typing import Any

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QKeyEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractItemView,
    QAbstractSlider,
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QKeySequenceEdit,
    QLineEdit,
    QPlainTextEdit,
    QTextEdit,
    QWidget,
)

import albuswall
from albuswall.log import getLogger

from .bindings import normalize_key
from .dispatch import DispatchPolicy
from .model import Scope, registry

__all__ = [
    "QtBinder", "TextInputShortcutGuard", "attach", "binder", "detach",
    "install_text_input_guard",
]

_logger = getLogger(f"{albuswall.__name__}.ui.keymap.qt_binder")

_NAV_KEYS = frozenset({
    Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down,
    Qt.Key.Key_Home, Qt.Key.Key_End, Qt.Key.Key_PageUp, Qt.Key.Key_PageDown,
    Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space,
    Qt.Key.Key_Backspace, Qt.Key.Key_Delete, Qt.Key.Key_Tab,
    Qt.Key.Key_Backtab,
})
#: 声明了 ``keymap_passthrough`` 的自绘控件（如缩略图网格）自己消费的键。
#: 刻意**不含** Space / Delete：网格不用它们，必须留给快捷键
#: （``preview.space`` / ``asset.trash``）。
_PASSTHROUGH_KEYS = frozenset({
    Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down,
    Qt.Key.Key_Home, Qt.Key.Key_End, Qt.Key.Key_PageUp, Qt.Key.Key_PageDown,
    Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Tab, Qt.Key.Key_Backtab,
})
_COMMAND_MODIFIERS = (
    Qt.KeyboardModifier.ControlModifier
    | Qt.KeyboardModifier.AltModifier
    | Qt.KeyboardModifier.MetaModifier
)


def _is_text_input(widget: QObject | None) -> bool:
    return isinstance(widget, (QLineEdit, QTextEdit, QPlainTextEdit))


def _is_editing_sequence(widget: QObject | None) -> bool:
    node = widget
    while node is not None:
        if isinstance(node, QKeySequenceEdit):
            return True
        try:
            node = node.parentWidget()  # type: ignore[union-attr]
        except AttributeError:
            return False
    return False


def _event_key(event: QKeyEvent) -> str:
    try:
        text = QKeySequence(event.keyCombination()).toString(
            QKeySequence.SequenceFormat.PortableText)
        return normalize_key(text)
    except ValueError:
        return ""


def _key_actions(key: str):
    return tuple(action for action, _binding in registry.bindings(key))


def _widget_owns_key(widget: QObject, event: QKeyEvent) -> bool:
    """焦点控件是否应该自己消费这个键（从而不让 QShortcut 抢走）。"""
    if isinstance(widget, QKeySequenceEdit) or _is_editing_sequence(widget):
        return True
    key = _event_key(event)
    if _is_text_input(widget):
        # 输入框里只有显式允许的动作才放行给快捷键；其余交还输入框
        return not any(a.allow_in_text_input or a.is_global for a in _key_actions(key))
    if event.modifiers() & _COMMAND_MODIFIERS:
        return False
    if isinstance(widget, (QAbstractItemView, QAbstractSlider, QAbstractSpinBox, QComboBox)):
        return event.key() in _NAV_KEYS
    if isinstance(widget, QAbstractButton):
        # Space 走按钮自身的原生激活；Return 交给 ui.activate
        return event.key() == Qt.Key.Key_Space
    if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
        return True
    return (isinstance(widget, QWidget) and bool(widget.property("keymap_passthrough"))
            and event.key() in _PASSTHROUGH_KEYS)


class TextInputShortcutGuard(QObject):
    """让焦点控件在 ``ShortcutOverride`` 阶段决定「这个键归谁」。"""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._installed: weakref.WeakSet[QObject] = weakref.WeakSet()

    def install_on(self, widget: QObject | None) -> None:
        if widget is None or widget in self._installed:
            return
        widget.installEventFilter(self)
        self._installed.add(widget)

    def on_focus_changed(self, _old: QObject | None, new: QObject | None) -> None:
        self.install_on(new)

    def eventFilter(self, obj, event):  # noqa: N802
        if (event.type() != QEvent.Type.ShortcutOverride
                or not isinstance(event, QKeyEvent)):
            return False
        if _widget_owns_key(obj, event):
            event.accept()
            return True
        return False


class QtBinder:
    """维护每个已 attach 窗口上的 ``QShortcut`` 生命周期。"""

    def __init__(self) -> None:
        self._owners: dict[int, weakref.ref[QWidget]] = {}
        self._owner_ids: weakref.WeakKeyDictionary[QWidget, int] = (
            weakref.WeakKeyDictionary())
        self._shortcuts: dict[int, list[QShortcut]] = {}
        self._oids = itertools.count(1)
        self._unsubscribe: Callable[[], None] | None = None

    def attach(self, owner: QWidget) -> None:
        if self._unsubscribe is None:
            self._unsubscribe = registry.subscribe(self.rebuild_all)
            registry.set_focus_provider(QApplication.focusWidget)
        oid = self._owner_ids.get(owner)
        if oid is not None:
            return
        oid = next(self._oids)
        self._owner_ids[owner] = oid
        self._owners[oid] = weakref.ref(owner)
        owner.destroyed.connect(partial(self._forget, oid))
        self._rebuild(oid, owner)

    def detach(self, owner: QWidget) -> None:
        oid = self._owner_ids.pop(owner, None)
        if oid is None:
            return
        for shortcut in self._shortcuts.pop(oid, []):
            shortcut.setEnabled(False)
            shortcut.deleteLater()
        self._owners.pop(oid, None)

    def rebuild_all(self) -> None:
        for oid, ref in list(self._owners.items()):
            owner = ref()
            if owner is not None:
                self._rebuild(oid, owner)

    def _forget(self, oid: int, *_args: Any) -> None:
        self._shortcuts.pop(oid, None)
        self._owners.pop(oid, None)

    def _rebuild(self, oid: int, owner: QWidget) -> None:
        for shortcut in self._shortcuts.pop(oid, []):
            shortcut.setEnabled(False)
            shortcut.deleteLater()

        shortcuts: list[QShortcut] = []
        for key, bindings in registry.effective_bindings().items():
            actions = [registry.get_action(b.action) for b in bindings]
            actions = [a for a in actions if a is not None]
            if not actions:
                continue
            sequence = QKeySequence.fromString(
                key, QKeySequence.SequenceFormat.PortableText)
            if sequence.isEmpty():
                _logger.warning("shortcut %r cannot be parsed, skipped", key)
                continue
            shortcut = QShortcut(sequence, owner)
            if any(a.scope is Scope.GLOBAL for a in actions):
                shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
            shortcut.activated.connect(
                partial(self._fire, key, weakref.ref(owner)))
            shortcuts.append(shortcut)
        self._shortcuts[oid] = shortcuts

    def _fire(self, key: str, ref: weakref.ref[QWidget]) -> None:
        owner = ref()
        if owner is None or not registry.enabled:
            return
        focus = QApplication.focusWidget()
        focus_owned = (
            focus.window() is owner if focus is not None
            else QApplication.activeWindow() in (None, owner)
        )
        policy = DispatchPolicy(
            focus_owned=focus_owned,
            in_text_input=_is_text_input(focus),
            modal_open=QApplication.activeModalWidget() is not None,
            editing_sequence=_is_editing_sequence(focus),
        )
        ctx = registry.context(window=owner, focus=focus)
        registry.dispatch(key, ctx, lambda action: policy.allows(action, ctx))


binder = QtBinder()
_guard: TextInputShortcutGuard | None = None


def attach(owner: QWidget) -> None:
    binder.attach(owner)


def detach(owner: QWidget) -> None:
    binder.detach(owner)


def install_text_input_guard(app: QObject | None = None) -> TextInputShortcutGuard:
    """安装焦点守卫（全局单例，多次调用幂等）。"""
    global _guard
    if _guard is not None:
        return _guard
    application = QApplication.instance()
    if application is None:
        raise RuntimeError("QApplication 未创建，无法安装输入框守卫")
    _guard = TextInputShortcutGuard(application)
    application.focusChanged.connect(_guard.on_focus_changed)  # type: ignore[attr-defined]
    _guard.install_on(QApplication.focusWidget())
    return _guard
