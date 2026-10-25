#
"""可映射键位的「动作」登记表（不依赖 Qt）。

一条 :class:`KeyAction` 描述「一个可以绑定按键的功能」：

    from albuswall.ui.builtin.keymap import register_action, KeyAction

    register_action(KeyAction(
        id="demo.hello",
        label="Say hello",
        default_keys="Ctrl+Alt+H",
        handler=lambda ctx: print("hello", ctx.window),
    ))

设计约定
========

导入期安全
    登记只写内存表，不 import Qt、不读配置。UI 实现（``manager`` / ``page``）
    在用到时才 import 本模块，因此插件可以在任何时机声明动作。

稳定 id
    ``id`` 是落盘键：``keymap.json`` 里 ``bindings`` 的键就是它。一旦发布
    就不要改名（改名等于让用户的旧配置失效）；显示文案用 ``label``。

任意按键
    按钮 / 动作是开放的，按键也是：``default_keys`` 与配置文件里的值都是
    Qt PortableText 序列字符串（``"Ctrl+,"`` / ``"F1"`` / ``"Left"``）。
    空字符串表示**默认不绑定**，用户仍然可以在设置页里给它绑一个键。

条件执行
    ``when(ctx)`` 返回 False 时该动作不参与本次按键分发（例如「上一张」
    只在详情页打开时生效）。这是「同一个键在不同界面做不同事」的唯一
    表达方式，不允许在 handler 里各写一套判断。

重复登记
    重复 id 默认抛异常（与 ``@command`` / ``PluginManager`` 一致）：
    静默覆盖会让「谁赢了」变成 import 顺序的副产品。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterator, Optional

__all__ = [
    "GROUP_ORDER",
    "KeyAction",
    "clear_actions",
    "get_action",
    "group_rank",
    "iter_actions",
    "register_action",
    "unregister_action",
]

#: 动作分组（只影响设置页里的排序与检索，不影响功能）
GROUP_ORDER: dict[str, int] = {
    "window": 10,
    "search": 20,
    "focus": 25,
    "viewer": 30,
    "general": 90,
}

#: 未登记分组排在已知分组之后
_UNKNOWN_GROUP_RANK = 50

#: handler 的返回值：``False`` = 这次按键没被消费（继续往下传）；
#: 其余（含 None）= 已消费。
Handler = Callable[[Any], Any]
Predicate = Callable[[Any], bool]


def group_rank(group: str) -> int:
    """分组名 → 排序权重（未知分组统一排在中间偏后）。"""
    return GROUP_ORDER.get(str(group or ""), _UNKNOWN_GROUP_RANK)


@dataclass(frozen=True)
class KeyAction:
    """一条可绑定按键的动作声明。

    Args:
        id: 稳定标识（落盘键），建议 ``<域>.<动作>``，如 ``viewer.next``。
        label: 设置页展示名（用户可读）。
        default_keys: 默认按键（PortableText），空串 = 默认不绑定。
        description: 一句话说明（设置页 tooltip / 说明行）。
        group: 分组名（见 :data:`GROUP_ORDER`）。
        order: 组内排序权重（小的在前）。
        handler: 触发回调，收到 :class:`KeymapContext`；返回 ``False``
            表示「这次没处理」（事件继续传递）。
        when: 可选可用性判定；返回 False 时该动作这次不参与分发。
        allow_in_text_input: 焦点在输入框里时是否仍然响应。
            默认 False（除 Ctrl/Alt/Meta 组合外，输入框里的裸键不抢）。
    """

    id: str
    label: str
    default_keys: str = ""
    description: str = ""
    group: str = "general"
    order: int = 100
    handler: Optional[Handler] = None
    when: Optional[Predicate] = None
    allow_in_text_input: bool = False

    def __post_init__(self) -> None:
        if not str(self.id or "").strip():
            raise ValueError("key action id must be a non-empty string")
        if not str(self.label or "").strip():
            raise ValueError(f"key action {self.id!r}: label is required")
        if self.handler is not None and not callable(self.handler):
            raise TypeError(
                f"key action {self.id!r}: handler must be callable, "
                f"got {type(self.handler).__name__}"
            )
        if self.when is not None and not callable(self.when):
            raise TypeError(
                f"key action {self.id!r}: when must be callable, "
                f"got {type(self.when).__name__}"
            )

    @property
    def sort_key(self) -> tuple[int, int, str]:
        return (group_rank(self.group), int(self.order), self.id)


_ACTIONS: dict[str, KeyAction] = {}


def register_action(action: KeyAction, *, replace: bool = False) -> KeyAction:
    """登记一个动作；重复 id 默认抛 ``ValueError``。"""
    if not isinstance(action, KeyAction):
        raise TypeError(
            f"register_action expected KeyAction, got {type(action).__name__}"
        )
    if action.id in _ACTIONS and not replace:
        raise ValueError(f"duplicate key action: {action.id!r}")
    _ACTIONS[action.id] = action
    return action


def unregister_action(action_id: str) -> Optional[KeyAction]:
    """移除一个动作（测试 / 插件卸载用）。"""
    return _ACTIONS.pop(str(action_id), None)


def get_action(action_id: str) -> Optional[KeyAction]:
    return _ACTIONS.get(str(action_id))


def iter_actions() -> Iterator[KeyAction]:
    """按 ``(分组权重, order, id)`` 排好序的全部动作。"""
    return iter(sorted(_ACTIONS.values(), key=lambda action: action.sort_key))


def clear_actions() -> None:
    """测试用：清空整张表。"""
    _ACTIONS.clear()
