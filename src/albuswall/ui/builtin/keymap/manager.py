#
"""按键分发：一个应用级事件过滤器 + 一张「按键 → 动作」表。

为什么不用 ``QShortcut``
========================

``QShortcut`` 绑得死、抢得狠：一个 ``Left`` 会连同列表 / 输入框的默认行为
一起吞掉，也没地方表达「只在详情页生效」。本项目的键位要求是**任意按键
集中管理**，所以改成：

1. 在 ``QApplication`` 上装一个事件过滤器（能收到所有子控件的按键）；
2. 用它自己的「焦点属于本窗口吗 / 焦点在输入框吗」策略决定是否参与；
3. 命中 ``按键 → [动作...]`` 里的第一条可用动作后消费事件，否则原样放行。

事件顺序保证
============

* 输入框（``QLineEdit`` / ``QTextEdit`` / …）里默认只放行带
  Ctrl / Alt / Meta 的组合键，避免 ``F`` 这种裸键触发「收藏」；
* 有模态对话框时焦点窗口不是主窗口，过滤器直接不参与；
* 同一个键被多个动作占用时按登记顺序取第一条可用的，设置页会把冲突
  明确标出来（``store.conflicts``）。

生命周期
========

``KeymapManager`` 是一个 presenter（实现 ``setup`` / ``teardown``），由
``PresenterManager`` 构造并登记；``setup`` 时装过滤器 + 订阅
``dynamic.keymap`` 的变更，``teardown`` 时全部撤掉。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import QApplication

from albuswall.log import getLogger

from . import store
from .actions import ensure_builtin_actions
from .keys import (
    is_key_sequence_editor,
    is_modifier_key,
    is_text_input,
    sequence_from_event,
    text_input_allows_modifiers,
)
from .registry import KeyAction, iter_actions

__all__ = ["KeymapContext", "KeymapManager", "active_manager"]

_logger = getLogger(__name__)

#: 当前进程里生效的 manager（设置页 / 调试用；没有就返回 None）
_active: Optional["KeymapManager"] = None


def active_manager() -> Optional["KeymapManager"]:
    """取当前生效的 :class:`KeymapManager`（未装配时返回 None）。"""
    return _active


@dataclass(slots=True)
class KeymapContext:
    """动作回调能拿到的东西（故意只暴露少量入口，避免 handler 乱爬窗口）。

    ``focus`` 是**本次按键发生时的焦点控件**（每次分发前刷新）；``when``
    谓词据此避免抢走列表 / 输入框自己的方向键。
    """

    window: Any
    container: Any = None
    presenters: Any = None
    focus: Any = None


class KeymapManager(QObject):
    """把按键事件分发到 :mod:`registry` 里登记的动作。"""

    def __init__(
            self,
            window: Any,
            container: Any = None,
            presenters: Any = None,
            parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._window = window
        self._container = container
        self._presenters = presenters
        self._ctx = KeymapContext(
            window=window, container=container, presenters=presenters,
        )

        #: 按键 → 按登记顺序排好的动作列表
        self._table: dict[str, list[KeyAction]] = {}
        self._unsubscribe = None
        self._installed = False

    # ---------------- 生命周期 ----------------
    def setup(self, container: Any = None) -> None:
        """登记内置动作、装事件过滤器、订阅键位配置。"""
        if container is not None:
            self._container = container
            self._ctx.container = container

        ensure_builtin_actions()
        self.refresh()

        state = store.keymap_state()
        try:
            self._unsubscribe = state.observe(self._on_state_changed)
        except Exception:  # noqa: BLE001  没有观察接口也不影响分发
            _logger.debug("keymap state observer unavailable", exc_info=True)

        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
            self._installed = True

        global _active
        _active = self
        _logger.info(
            "keymap ready: %d distinct keys, %d bound actions",
            len(self._table),
            sum(len(actions) for actions in self._table.values()),
        )

    def teardown(self) -> None:
        """撤掉事件过滤器与观察者（幂等）。"""
        if self._installed:
            app = QApplication.instance()
            if app is not None:
                app.removeEventFilter(self)
            self._installed = False

        if self._unsubscribe is not None:
            try:
                self._unsubscribe()
            except Exception:  # noqa: BLE001
                _logger.debug("keymap state unsubscribe failed", exc_info=True)
            self._unsubscribe = None

        global _active
        if _active is self:
            _active = None

    # ---------------- 表 ----------------
    def refresh(self) -> None:
        """按最新动作 + 配置重建「按键 → 动作」表。"""
        ensure_builtin_actions()
        table: dict[str, list[KeyAction]] = {}
        for action in iter_actions():
            keys = store.binding_for(action)
            if not keys or action.handler is None:
                continue
            table.setdefault(keys, []).append(action)
        self._table = table

    @property
    def table(self) -> dict[str, list[KeyAction]]:
        """当前生效的 ``{按键: [动作...]}``（只读副本）。"""
        return {keys: list(actions) for keys, actions in self._table.items()}

    def actions_for(self, sequence: str) -> list[KeyAction]:
        """某个按键上挂着的动作（按登记顺序）。"""
        return list(self._table.get(sequence, ()))

    @staticmethod
    def conflicts() -> dict[str, list[str]]:
        """当前所有「一键多动作」的冲突：``{按键: [动作 id...]}``。"""
        return store.conflicts()

    def trigger(self, action_id: str) -> bool:
        """按动作 id 直接执行（IPC / 菜单 / 测试共用同一个 handler）。

        和按键路径唯一的区别是**不看** ``when`` 条件：调用方明确指定了要执行
        哪个动作，是否可用由调用方自己判断。
        """
        from .registry import get_action

        action = get_action(action_id)
        if action is None:
            return False
        return self._invoke(action)

    # ---------------- 事件 ----------------
    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: N802
        if event.type() == QEvent.Type.KeyPress:
            try:
                if self.handle_key_event(event):
                    return True
            except Exception:  # noqa: BLE001  分发失败不能影响正常输入
                _logger.exception("keymap dispatch failed")
        return super().eventFilter(obj, event)

    def handle_key_event(self, event: Any, *, focus: Any = None) -> bool:
        """处理一个 ``QKeyEvent``；返回 True 表示事件已被消费。

        ``focus`` 仅供测试注入；正常路径取 ``QApplication.focusWidget()``。
        """
        if not store.is_enabled():
            return False

        app = QApplication.instance()
        if app is None:
            return False
        if focus is None:
            focus = app.focusWidget()
        if not self._owns_focus(focus):
            return False

        # 正在「采键」（QKeySequenceEdit）时完全放行，否则想绑 Ctrl+F 会
        # 先被键位系统吃掉
        if is_key_sequence_editor(focus):
            return False

        if is_modifier_key(event.key()):
            return False

        sequence = sequence_from_event(event)
        if not sequence:
            return False
        candidates = self._table.get(sequence)
        if not candidates:
            return False

        # 谓词 / handler 需要知道「这次按键发生在哪个控件上」
        self._ctx.focus = focus

        modifiers = event.modifiers()
        in_text = is_text_input(focus)
        text_only = in_text and not text_input_allows_modifiers(modifiers)

        for action in candidates:
            if text_only and not action.allow_in_text_input:
                continue
            if not self._can_run(action):
                continue
            if self._invoke(action):
                event.accept()
                return True
        return False

    # ---------------- 内部 ----------------
    def _owns_focus(self, focus: Any) -> bool:
        """焦点控件 / 活动窗口是否属于本窗口。"""
        if focus is not None:
            try:
                return focus.window() is self._window
            except Exception:  # noqa: BLE001  已销毁的控件
                return False
        active = QApplication.activeWindow()
        return active is None or active is self._window

    def _can_run(self, action: KeyAction) -> bool:
        predicate = action.when
        if predicate is None:
            return True
        try:
            return bool(predicate(self._ctx))
        except Exception:  # noqa: BLE001  条件失败只跳过这条动作
            _logger.exception("keymap action %r: when() failed", action.id)
            return False

    def _invoke(self, action: KeyAction) -> bool:
        """执行动作；``False`` 表示「没消费」，继续尝试下一条。"""
        handler = action.handler
        if handler is None:
            return False
        try:
            result = handler(self._ctx)
        except Exception:  # noqa: BLE001  单个动作失败不能打断输入
            _logger.exception("keymap action %r failed", action.id)
            return False
        return result is not False

    def _on_state_changed(self, _node: Any, key: str) -> None:
        """``dynamic.keymap`` 变了 → 重建表（设置页保存后立刻生效）。"""
        if key in ("bindings", "enabled", "*"):
            self.refresh()
            _logger.debug("keymap refreshed after change: %s", key)
