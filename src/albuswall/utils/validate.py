#
"""命令参数校验工具：把外部入参收口成强类型值。

命令层收到的参数来自 CLI（``ast.literal_eval`` 后）或 IPC / GUI 调用方，
形状不可信。本模块提供最小的强依赖校验：失败一律抛 ``BadRequest``
（``DispatchError`` 子类，``code="bad_request"``），消息里带 ``field`` 名，
使 handler 只需 ``except DispatchError`` 一棵树即可。

依赖方向：``utils → command``（只取异常类型），不反向 import 任何
handler / service，保持"工具纯函数、无副作用、无状态"。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from albuswall.command.exceptions import BadRequest

__all__ = [
    "require_non_negative",
    "require_positive",
    "require_uuid",
]


def require_uuid(value: str, *, field: str = "uuid") -> UUID:
    """``field`` 必须是合法 UUID 字符串（空串 / ``None`` / 非法格式 → ``BadRequest``）。

    大小写不敏感：``UUID()`` 接受大写输入，返回值的 ``str()`` 是规范小写形式，
    调用方拿 ``str(uuid_obj)`` 落库/比较即可（库内口径也是小写）。
    """
    if value is None:
        raise BadRequest(f"{field}: missing uuid (expected a non-empty string)")
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str):
        raise BadRequest(
            f"{field}: expected a uuid string, got {type(value).__name__}"
        )
    text = value.strip()
    if not text:
        raise BadRequest(f"{field}: missing uuid (expected a non-empty string)")
    try:
        return UUID(text)
    except (TypeError, ValueError) as e:
        raise BadRequest(f"{field}: invalid uuid {value!r}") from e


def require_non_negative(value: int, *, field: str) -> int:
    """``field`` 必须是非负整数。

    ``bool`` 不算整数（``True`` 是 ``int`` 的子类，必须显式拒绝）；
    负数 / 非整数 → ``BadRequest``。
    """
    _require_int(value, field=field)
    if value < 0:
        raise BadRequest(f"{field}: must be >= 0, got {value!r}")
    return value


def require_positive(value: int, *, field: str) -> int:
    """``field`` 必须是正整数。

    ``bool`` 不算整数（``True`` 是 ``int`` 的子类，必须显式拒绝）；
    0 / 负数 / 非整数 → ``BadRequest``。
    """
    _require_int(value, field=field)
    if value <= 0:
        raise BadRequest(f"{field}: must be > 0, got {value!r}")
    return value


def _require_int(value: Any, *, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BadRequest(
            f"{field}: expected an int, got {type(value).__name__}"
        )
