#
"""Handler 参数门面：构造 ``Param`` + 收口外部入参。

handler 模块**只**从本模块取参数能力，不要直接碰
``albuswall.command.model.Param``。被固化的三个坑：

1. **位置参数的可选值必须显式给 ``default``**。``Dispatcher._bind`` 缺参时
   填的是 ``Param.default``，不读 handler 签名里的默认值
   （见 ``tests/test_handlers_commands.py`` 的回归用例）；漏写会得到
   ``None``，随后在 handler 第一行 ``AttributeError`` → 对外表现为
   ``CommandFailed``（"命令执行失败"），而不是可预期的"参数缺失"。
   ``pos()`` 因此在构造期就拒绝这种写法。
2. **``flag`` 的 ``type`` 只能是 ``bool``**（``Param.__post_init__`` 会拦）。
   ``flag()`` 直接把它固定住。
3. **``Param.type`` 不参与运行时转换**。CLI 侧是 ``ast.literal_eval`` 后的
   字面量，IPC / GUI 侧是不可信的 JSON —— 强类型必须在 handler 入口显式
   收口，用本模块的 ``as_*`` 系列（失败统一抛 ``BadRequest``，``code`` 是
   ``bad_request``、退出码是 ``USAGE_ERROR``，handler 只需 ``except
   DispatchError`` 一棵树）。

边界
----
* 只依赖 ``albuswall.command``（模型 + 异常）与 ``albuswall.utils.validate``；
  **不** import service / 容器 / repo（``tests/test_handlers_contract.py``
  对 handler 模块有 AST 断言）；
* 本模块没有 ``setup()``，因此**不能**进 ``handlers/__init__.py`` 的
  ``MODULES``（那里会 ``assert isinstance(module, HandlerModule)``）。
"""

from __future__ import annotations

from typing import Any, Callable, Sequence
from uuid import UUID

from albuswall.command.exceptions import BadRequest
from albuswall.command.model import Param
from albuswall.utils.validate import (
    require_non_negative,
    require_positive,
    require_uuid,
)

__all__ = [
    # Param 工厂
    "flag",
    "limit_params",
    "opt",
    "paging_params",
    "pos",
    # 入参收口
    "as_bool",
    "as_choice",
    "as_int",
    "as_uuid",
    "require_non_negative",
    "require_positive",
    "require_uuid",
]


# ==================== Param 工厂 ====================

def pos(
        name: str,
        *,
        type: Callable[[str], Any] = str,
        required: bool = True,
        default: Any = None,
        choices: Sequence[Any] | None = None,
        help: str = "",
) -> Param:
    """位置参数。

    ``required=False`` 时**必须**给非 ``None`` 的 ``default`` —— 否则
    ``_bind`` 会填 ``None``，把"缺参"变成 handler 里的 ``AttributeError``。

    :raises ValueError: 声明自相矛盾（required 带 default / 可选却没 default）。
    """
    if required and default is not None:
        raise ValueError(
            f"{name!r}: required 位置参数不能带 default"
        )
    if not required and default is None:
        raise ValueError(
            f"{name!r}: 可选位置参数必须显式给 default（Dispatcher._bind "
            f"只认 Param.default，不读 handler 签名的默认值）"
        )
    return Param(
        name=name, kind="pos", type=type, default=default,
        required=required, choices=_choices(choices), help=help,
    )


def opt(
        name: str,
        *,
        type: Callable[[str], Any] = str,
        default: Any = None,
        required: bool = False,
        short: str | None = None,
        choices: Sequence[Any] | None = None,
        help: str = "",
) -> Param:
    """``--name VALUE`` 形式的选项。

    注意 ``default=None`` 与"不给 default"在 ``_bind`` 里是同一件事
    （都是 ``None``）；需要别的默认值就显式写出来，别依赖 handler 签名。
    """
    if required and default is not None:
        raise ValueError(f"{name!r}: required 选项不能带 default")
    return Param(
        name=name, kind="opt", type=type, default=default,
        required=required, short=short, choices=_choices(choices), help=help,
    )


def flag(
        name: str,
        *,
        default: bool = False,
        short: str | None = None,
        help: str = "",
) -> Param:
    """开关（``--name``）。``type`` 固定为 ``bool``。"""
    return Param(
        name=name, kind="flag", type=bool, default=default,
        short=short, help=help,
    )


def _choices(choices: Sequence[Any] | None) -> tuple[Any, ...] | None:
    """``choices`` 归一为 tuple（``Param.choices`` 的类型契约）。"""
    return None if choices is None else tuple(choices)


# ==================== 入参收口 ====================
#
# 全部失败路径统一抛 ``BadRequest``：消息里带 ``field``，方便 CLI 直接展示
# 字段名，GUI 也能据此定位控件。


def as_uuid(value: Any, *, field: str = "uuid") -> UUID:
    """``field`` 必须是合法 UUID（``UUID`` 实例或大小写不敏感的字符串）。"""
    return require_uuid(value, field=field)


def as_bool(value: Any, *, field: str) -> bool:
    """把松散布尔收口成 ``bool``。

    接受 ``bool`` / ``int`` / ``float`` / 常见字符串字面量（``"true"`` /
    ``"off"`` / ``""`` …）。``None`` 视为 ``False``（"没给" = "关"）。
    ``flag`` 参数由 ``Dispatcher._bind`` 已经转过，本函数是给 **option**
    （``--force true``）或直接调 service 的场景用的。
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off", ""):
            return False
    raise BadRequest(f"{field}: expected a boolean, got {value!r}")


def as_int(
        value: Any,
        *,
        field: str,
        minimum: int | None = None,
        maximum: int | None = None,
) -> int:
    """``field`` 必须是整数（``bool`` 不算），并可限定闭区间。

    严格拒绝字符串：能自动转的入口（CLI）已经转过，剩下的都该显式表态。
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise BadRequest(
            f"{field}: expected an int, got {type(value).__name__}"
        )
    if minimum is not None and value < minimum:
        raise BadRequest(f"{field}: must be >= {minimum}, got {value!r}")
    if maximum is not None and value > maximum:
        raise BadRequest(f"{field}: must be <= {maximum}, got {value!r}")
    return value


def as_choice(value: Any, choices: Sequence[Any], *, field: str) -> Any:
    """``field`` 必须属于 ``choices``（``Param.choices`` 的运行时兜底）。

    与 ``Param.choices`` 的分工：``Param.choices`` 只挡 CLI/IPC 走
    ``Dispatcher._bind`` 的那条路；直接调 service 的调用方（测试、GUI 内部）
    走本函数。
    """
    if value not in choices:
        raise BadRequest(f"{field}: {value!r} not in {tuple(choices)!r}")
    return value


# ==================== 复用参数组 ====================

def limit_params(
        *,
        default: int = 20,
        maximum: int = 500,
        help: str = "返回数量",
) -> tuple[Param, ...]:
    """单个 ``limit`` 选项的常见组合。"""
    return (
        opt("limit", type=int, default=default,
            help=f"{help}（最大 {maximum}）"),
    )


def paging_params(
        *,
        default: int = 20,
        help: str = "返回数量",
) -> tuple[Param, ...]:
    """``limit`` + ``offset`` 分页组合。"""
    return (
        opt("limit", type=int, default=default, help=help),
        opt("offset", type=int, default=0, help="跳过条数"),
    )
