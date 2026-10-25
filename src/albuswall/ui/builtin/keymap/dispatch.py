"""分发策略（Qt-free）：这次按键该不该触发动作的规则只在这里。

Qt 层只负责把 ``focus_owned`` / ``in_text_input`` / ``modal_open`` /
``editing_sequence`` 四个布尔量塞进来。
"""
from __future__ import annotations

from dataclasses import dataclass

from .model import Action, Context, Scope

__all__ = ["DispatchPolicy"]


@dataclass(frozen=True, slots=True)
class DispatchPolicy:
    """纯函数对象：``allows(action, ctx) -> bool``。"""

    focus_owned: bool = True
    in_text_input: bool = False
    modal_open: bool = False
    editing_sequence: bool = False

    def allows(self, action: Action, ctx: Context) -> bool:
        # 录键 / 模态对话框：键盘归对方，事件整体放行 = 不触发我们的动作
        if self.editing_sequence or self.modal_open:
            return False
        if action.when is not None:
            try:
                if not action.when(ctx):
                    return False
            except Exception:  # noqa: BLE001 - 谓词失败只跳过这一条
                return False
        if action.scope is Scope.COMPONENT and not self.focus_owned:
            return False
        return not (self.in_text_input
                    and not action.is_global
                    and not action.allow_in_text_input)
