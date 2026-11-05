#
"""把键位绑定表接到 Qt 事件上：``QKeyEvent`` → 槽（binder）。

这一层只做一件事：把按键规范化成 :class:`KeySequence`，在
:class:`~albuswall.ui.keymap.registry.ShortcutRegistry` 里查出它绑定的命令，
命中就调 :meth:`~albuswall.ui.keymap.registry.ShortcutRegistry.trigger`。
**不用** ``QShortcut``：它只能挂单步按键，多步和弦要自己攒状态。

挂在 ``QApplication`` 上的**事件过滤器**，规则如下（顺序即代码顺序）：

1. 只处理 ``KeyPress``；忽略自动重复、纯修饰键、非自发事件
   （``activation_keys`` 把回车改写成合成空格，那种事件不能再触发一遍）；
2. **只在焦点控件那一遍处理**：Qt 会把没被消费的按键向父控件冒泡，同一次
   按键可能以「接收者 ≠ 焦点控件」的形态再来一次，第二遍一律放行；
3. 作用域：只认 ``owner``（默认是主窗口）顶层窗口里的按键；
4. **导航键永不接管**（Tab / 方向键 / Home / End / PgUp / PgDn / Esc）：
   焦点链与 Esc 层级归控件和窗口，绑了也不生效（登记时告警）；
5. 焦点在**文本输入**上：无修饰键一律放行（打字）；
   焦点在 ``keymap_passthrough`` 控件上：空格 / 回车放行（按钮点击、网格
   快速预览）——修饰键组合照常触发；
6. **只吞能触发的键**：命中的绑定里至少有一个已登记槽才消费，占位绑定
   （命令还没实现 / 插件没加载）不改变现有键盘行为；
7. 和弦：先攒前缀并等下一键（默认 1.2s）；同一前缀既是完整键位又是更长
   和弦的开头时，**先等超时再退化成短键位**（``G`` 与 ``G, G`` 并存）；超时
   只放弃前缀，不做别的。

焦点与 Esc **完全不归本层**：``Window.keyPressEvent`` 的两级 Esc、
``utils/activation_keys.py`` 的回车→空格、网格自己的方向键与空格预览，
都因为上面第 4、5 条原样保留。

用法
====

界面 boot（``ui/bootstrap.py`` 的 phase 5）在登记完槽之后调
:func:`install_shortcut_binder`；重复调用幂等（同一个 ``QApplication`` 只有
一个 binder，owner / 注册表就地更新）。
"""

from __future__ import annotations

import logging
import weakref
from contextlib import suppress
from typing import TYPE_CHECKING

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtGui import QKeyEvent, QKeySequence
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QLineEdit,
    QPlainTextEdit,
    QTextEdit,
    QWidget,
)

from .errors import InvalidKeySequence
from .keys import (
    KeyAlias,
    is_navigation_step,
    is_widget_step,
    sequence_identity,
    step_identity,
)
from .sequence import KeySequence as _KeySequence

if TYPE_CHECKING:
    from .registry import ShortcutRegistry

__all__ = [
    "DEFAULT_CHORD_TIMEOUT_MS",
    "ShortcutBinder",
    "install_shortcut_binder",
    "uninstall_shortcut_binder",
]

logger = logging.getLogger(__package__)

#: 和弦两键之间的最长间隔；超过就放弃前缀（只放弃，不触发）。
DEFAULT_CHORD_TIMEOUT_MS = 1200

#: 文本输入控件：无修饰键的按键属于输入内容，不能接管。
_TEXT_INPUT_TYPES: tuple[type[QWidget], ...] = (
    QLineEdit,
    QPlainTextEdit,
    QTextEdit,
    QAbstractSpinBox,
)

#: 控件「让渡按键」的标记属性（网格 / 可勾选分组在 ``__init__`` 里设）。
_PASSTHROUGH_PROPERTY = "keymap_passthrough"

#: 纯修饰键 / 锁定键的键码：按下它们不算一次按键。
_MODIFIER_KEYS: frozenset[int] = frozenset({
    int(Qt.Key.Key_Shift),
    int(Qt.Key.Key_Control),
    int(Qt.Key.Key_Alt),
    int(Qt.Key.Key_Meta),
    int(Qt.Key.Key_AltGr),
    int(Qt.Key.Key_CapsLock),
    int(Qt.Key.Key_NumLock),
    int(Qt.Key.Key_ScrollLock),
    int(Qt.Key.Key_Super_L),
    int(Qt.Key.Key_Super_R),
    int(Qt.Key.Key_Hyper_L),
    int(Qt.Key.Key_Hyper_R),
})

#: ``id(app)`` 不安全，用弱引用表记住「这个应用已经装过 binder」。
_INSTALLED: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _is_text_input(widget: QWidget | None) -> bool:
    """``widget`` 是不是文本输入控件（含可编辑下拉框）。"""
    if widget is None:
        return False
    if isinstance(widget, _TEXT_INPUT_TYPES):
        return True
    return isinstance(widget, QComboBox) and widget.isEditable()


def _is_passthrough(widget: QWidget | None) -> bool:
    """``widget`` 或它的祖先是否声明「这组按键让渡给控件」。"""
    current = widget
    while current is not None:
        try:
            if current.property(_PASSTHROUGH_PROPERTY) is True:
                return True
        except RuntimeError:  # 控件已被销毁
            return False
        current = current.parentWidget()
    return False


class ShortcutBinder(QObject):
    """把 ``QKeyEvent`` 接到快捷键槽上的事件过滤器。

    Args:
        registry: 键位绑定表（键位 → 命令）。
        owner: 作用域窗口（默认不限：拦整个应用）；主窗口销毁时自动摘掉。
        chord_timeout_ms: 和弦两键之间的最长间隔（毫秒）。
        allow_synthetic: 是否处理非自发事件（``QKeyEvent`` 手工构造 /
            ``QApplication.sendEvent`` 投递）。默认 ``False``：只认真正从
            平台进来的按键，避免把合成事件（回车→空格）再触发一遍。测试与
            自动化注入需要打开。
    """

    def __init__(
            self,
            registry: ShortcutRegistry,
            *,
            owner: QWidget | None = None,
            chord_timeout_ms: int = DEFAULT_CHORD_TIMEOUT_MS,
            allow_synthetic: bool = False,
            parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._registry = registry
        self._owner: QWidget | None = None
        self._owner_gone = None
        #: owner 被销毁后不再接管按键（等新的 owner 装回来）。
        self._owner_dead = False
        self._chord_timeout_ms = max(1, int(chord_timeout_ms))
        self._allow_synthetic = bool(allow_synthetic)
        self._installed = False
        #: 已经匹配上的和弦前缀（每一步都是一个单步 ``KeySequence``）。
        self._pending: tuple[_KeySequence, ...] = ()
        #: 前缀本身也是完整键位时，等超时再退化的那些绑定。
        self._pending_exact: tuple[_KeySequence, ...] = ()
        #: 上一次被消费的按键（键码）：同一键在 ``KeyRelease`` 之前又来一次
        #: 说明是同一次按下的重复投递，不重复触发。
        self._held: int | None = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(self._chord_timeout_ms)
        self._timer.timeout.connect(self._on_chord_timeout)
        self.owner = owner

    # ------------------------------------------------------------------ #
    # 配置
    # ------------------------------------------------------------------ #
    @property
    def registry(self) -> ShortcutRegistry:
        """当前的绑定表。"""
        return self._registry

    @registry.setter
    def registry(self, value: ShortcutRegistry) -> None:
        self._registry = value
        self.reset()

    @property
    def owner(self) -> QWidget | None:
        """作用域窗口；``None`` = 不限（整个应用）。

        owner 被销毁后返回 ``None``，此时 :attr:`idle` 为 ``True``——binder
        不再接管任何按键（而不是把作用域放开到整个应用）。
        """
        return self._owner

    @owner.setter
    def owner(self, value: QWidget | None) -> None:
        if self._owner_gone is not None:
            with suppress(RuntimeError, TypeError):
                self._owner.destroyed.disconnect(self._owner_gone)
            self._owner_gone = None
        self._owner = value
        self._owner_dead = False
        if value is not None:
            self._owner_gone = value.destroyed.connect(
                self._on_owner_destroyed)
        self.reset()

    @property
    def idle(self) -> bool:
        """owner 已销毁、还没装新的：此时不接管任何按键。"""
        return self._owner_dead

    @property
    def chord_timeout_ms(self) -> int:
        return self._chord_timeout_ms

    @property
    def pending(self) -> tuple[str, ...]:
        """正在等的和弦前缀（文案形式，调试 / 测试用）。"""
        return tuple(str(step) for step in self._pending)

    @property
    def installed(self) -> bool:
        """是否已经挂上事件过滤器。"""
        return self._installed

    # ------------------------------------------------------------------ #
    # 安装 / 卸载
    # ------------------------------------------------------------------ #
    def install(self) -> ShortcutBinder:
        """挂到 ``QApplication`` 上；重复调用是 no-op。"""
        app = QApplication.instance()
        if app is None:
            raise RuntimeError("cannot install a shortcut binder without QApplication")
        if not self._installed:
            app.installEventFilter(self)
            self._installed = True
        _INSTALLED[app] = self
        return self

    def uninstall(self) -> None:
        """摘掉事件过滤器并清掉和弦状态（幂等）。"""
        app = QApplication.instance()
        if app is not None and self._installed:
            app.removeEventFilter(self)
        self._installed = False
        if app is not None and _INSTALLED.get(app) is self:
            del _INSTALLED[app]
        self.reset()

    def reset(self) -> None:
        """放弃正在输入的和弦。"""
        self._timer.stop()
        self._pending = ()
        self._pending_exact = ()
        self._held = None

    def _on_owner_destroyed(self, *_args: object) -> None:
        """作用域窗口没了：**不再接管**按键，但不在销毁信号里摘过滤器。

        在 ``destroyed`` 回调里 ``removeEventFilter`` 会让 Qt 在对象析构
        期间改事件过滤器链，容易踩到悬空指针（实测会段错误）。这里只把状态
        置成「没有作用域」，等界面重新装一个 owner 再恢复。
        """
        logger.debug("shortcuts: owner destroyed; the binder goes idle")
        self._owner = None
        self._owner_gone = None
        self._owner_dead = True
        self.reset()

    # ------------------------------------------------------------------ #
    # 事件过滤
    # ------------------------------------------------------------------ #
    def eventFilter(self, watched, event):  # noqa: N802 - Qt 命名
        etype = event.type()
        if etype in (
                QEvent.Type.ApplicationDeactivate,
                QEvent.Type.WindowDeactivate,
                QEvent.Type.ApplicationStateChange,
        ):
            # 切走窗口 / 应用：正在按的和弦作废，回来不能接上
            self.reset()
            return False
        if etype == QEvent.Type.KeyRelease:
            if self._held is not None and int(event.key()) == self._held:
                self._held = None
            return False
        if etype != QEvent.Type.KeyPress:
            return False
        if not isinstance(event, QKeyEvent):
            return False
        if event.isAutoRepeat():
            return False
        if not self._allow_synthetic and not event.spontaneous():
            # 合成事件（例如 activation_keys 把回车改写成空格）不再触发一遍
            return False
        if int(event.key()) in _MODIFIER_KEYS:
            return False

        key_code = int(event.key())

        widget = self._receiver_widget(watched)
        if widget is None or not self._in_scope(widget):
            return False
        # 冒泡副本 / 重复派发：只在焦点控件那一遍处理
        focus = QApplication.focusWidget()
        if focus is not None and widget is not focus:
            return False

        # 同一次按下的重复投递（QTest / 输入法 / 合成回放）：上一次被消费的
        # 键在 KeyRelease 之前又来一次，直接忽略，不要触发两遍。
        duplicate = self._held is not None and self._held == key_code
        self._held = None
        if duplicate:
            logger.debug("shortcuts: duplicated key press %#x ignored", key_code)
            return True

        sequence = self._sequence_from_event(event)
        if sequence is None:
            return False

        if self._pending and is_navigation_step(sequence.chords[0]):
            # 和弦进行中按 Esc / Tab / 方向键：放弃前缀，但键照常交给控件
            self.reset()

        if self._blocked(widget, sequence):
            return False

        consumed = self._handle(sequence)
        if consumed:
            self._held = key_code
        return consumed

    # ------------------------------------------------------------------ #
    # 判定
    # ------------------------------------------------------------------ #
    @staticmethod
    def _receiver_widget(watched: QObject) -> QWidget | None:
        """按键真正的接收控件：优先事件目标，其次当前焦点。"""
        if isinstance(watched, QWidget):
            return watched
        return QApplication.focusWidget()

    def _in_scope(self, widget: QWidget) -> bool:
        """按键是否发生在 ``owner`` 顶层窗口内（``owner=None`` 时全部放行）。"""
        if self._owner_dead:
            return False
        owner = self._owner
        if owner is None:
            return True
        try:
            return widget.window() is owner
        except RuntimeError:  # owner / widget 已被销毁
            return False

    def _sequence_from_event(self, event: QKeyEvent) -> _KeySequence | None:
        """``QKeyEvent`` → 单步 :class:`KeySequence`（失败 / 纯修饰键返回 ``None``）。"""
        try:
            text = QKeySequence(event.keyCombination()).toString(
                QKeySequence.SequenceFormat.PortableText)
        except (TypeError, ValueError):  # pragma: no cover - 防御 Qt 版本差异
            return None
        if not text:
            return None
        try:
            return _KeySequence.parse(text)
        except InvalidKeySequence:
            # 不认识的键名（少数多媒体键 / 输入法）：放行，别把按键吞掉
            logger.debug("shortcuts: cannot parse key event %r; ignored", text)
            return None

    def _blocked(self, widget: QWidget, sequence: _KeySequence) -> bool:
        """这一步按键是否应该交给控件自己处理。"""
        step = sequence.chords[0]
        if is_navigation_step(step):
            return True  # 焦点链 / Esc 层级不归本层
        text_input = _is_text_input(widget)
        if text_input and not step_identity(step)[0]:
            return True  # 文本输入里的无修饰键 = 打字
        declared = text_input or _is_passthrough(widget)
        return declared and is_widget_step(step)

    def _live(self, commands: tuple[str, ...]) -> bool:
        """至少有一个**可用**的槽（命令已登记）。"""
        return any(self._registry.has_slot(command) for command in commands)

    def _match(
            self,
            identity: tuple[tuple[frozenset[str], KeyAlias | str], ...],
    ) -> tuple[list[_KeySequence], list[_KeySequence]]:
        """在绑定表里找「精确命中」与「还有更长前缀」。

        只统计**至少有一个已登记槽**的绑定：否则绑定表里的占位键会把按键
        白白吞掉。
        """
        exact: list[_KeySequence] = []
        longer: list[_KeySequence] = []
        for sequence, commands in self._registry.bindings().items():
            if not self._live(commands):
                continue
            bound = sequence_identity(sequence)
            if len(bound) < len(identity):
                continue
            if bound[:len(identity)] != identity:
                continue
            (exact if len(bound) == len(identity) else longer).append(sequence)
        return exact, longer

    # ------------------------------------------------------------------ #
    # 触发
    # ------------------------------------------------------------------ #
    def _handle(self, step: _KeySequence) -> bool:
        """处理一步按键；返回是否消费掉这次 ``KeyPress``。"""
        candidate = (*self._pending, step)
        # 前缀的每一步都是单步序列：拼成一条连续的「步身份」元组
        identity = tuple(
            part for item in candidate for part in sequence_identity(item)
        )
        exact, longer = self._match(identity)

        if exact and longer:
            # 既是完整键位又是更长和弦的开头：先等超时，退化成短键位
            self._pending = candidate
            self._pending_exact = tuple(exact)
            self._timer.start()
            return True
        if exact:
            self._fire(exact)
            self.reset()
            return True
        if longer:
            self._pending = candidate
            self._pending_exact = ()
            self._timer.start()
            return True

        # 没有命中：前缀作废，把这一步重新当成新的开头
        if self._pending:
            self.reset()
            return self._handle(step)
        return False

    def _fire(self, sequences: tuple[_KeySequence, ...] | list[_KeySequence]) -> None:
        """按绑定表触发（一个键位可能绑多个命令）。"""
        for sequence in sequences:
            try:
                self._registry.trigger(sequence)
            except Exception:  # noqa: BLE001 - 槽里的异常不该炸事件循环
                logger.exception("keymap: shortcut %s failed", sequence)

    def _on_chord_timeout(self) -> None:
        """和弦超时：只有「前缀本身也是完整键位」时才补触发。"""
        exact = self._pending_exact
        self.reset()
        if exact:
            self._fire(exact)

    def __repr__(self) -> str:  # pragma: no cover - 调试可读性
        pending = ", ".join(self.pending) or "-"
        return (
            f"<ShortcutBinder installed={self._installed} "
            f"owner={type(self._owner).__name__} pending={pending}>"
        )


# ======================================================================
# 模块级入口：进程内单例（同一个 QApplication 只装一个）
# ======================================================================

def install_shortcut_binder(
        app: QApplication,
        registry: ShortcutRegistry,
        *,
        owner: QWidget | None = None,
        chord_timeout_ms: int = DEFAULT_CHORD_TIMEOUT_MS,
        allow_synthetic: bool = False,
) -> ShortcutBinder:
    """给 ``app`` 装上 binder（幂等）；返回那个 binder。

    已经装过就地更新注册表 / owner，不会叠出第二个过滤器——否则一次按键会
    触发两遍（测试里反复 boot 界面尤其明显）。
    """
    binder = _INSTALLED.get(app)
    if binder is None:
        binder = ShortcutBinder(
            registry,
            owner=owner,
            chord_timeout_ms=chord_timeout_ms,
            allow_synthetic=allow_synthetic,
            parent=app,
        )
        return binder.install()

    binder.registry = registry
    binder.owner = owner
    binder._chord_timeout_ms = max(1, int(chord_timeout_ms))
    binder._timer.setInterval(binder._chord_timeout_ms)
    binder._allow_synthetic = bool(allow_synthetic)
    return binder.install()


def uninstall_shortcut_binder(app: QApplication | None = None) -> bool:
    """摘掉 ``app`` 上的 binder；没有装过返回 ``False``。"""
    target = app if app is not None else QApplication.instance()
    if target is None:
        return False
    binder = _INSTALLED.get(target)
    if binder is None:
        return False
    binder.uninstall()
    return True
