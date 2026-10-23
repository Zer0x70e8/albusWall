#
"""注解解析工具：把字符串注解还原成类型对象。

只解决一件事：``from __future__ import annotations`` 或延迟解析场景下，
类属性注解是字符串，需要在 ``__set_name__`` 时还原成真正的类型。

解析策略（按优先级合并 eval globals）：

1. ``builtins``
2. ``typing`` 的公开名（``Optional`` / ``List`` / ``Any`` ……）
3. ``pathlib.Path``
4. ``owner`` 所在模块的 ``__dict__``（最高优先级）——用户模块里
   import 的自定义类型 / Enum 靠这一层解析。

失败语义：解析失败返回 ``None``（**不是** ``str``）——运行时校验应该
fail-open（不校验），而不是把干净的值误判成字符串。
"""

import builtins
import sys
import typing
import warnings
from pathlib import Path
from typing import Any, Optional

__all__ = ["resolve_annotation"]


def _eval_globals(owner: Optional[type]) -> dict[str, Any]:
    """构造 eval 用的 globals：builtins → typing → Path → owner 模块。"""
    namespace: dict[str, Any] = {}
    namespace.update(vars(builtins))
    namespace.update(vars(typing))
    namespace.setdefault("Path", Path)

    if owner is not None:
        module = sys.modules.get(getattr(owner, "__module__", ""))
        if module is not None:
            # 用户模块最高优先级：同名的自定义类型要能盖过 typing 里的名字
            namespace.update(vars(module))
    return namespace


def resolve_annotation(
        ann: Any,
        *,
        owner: Optional[type] = None,
        field: Optional[str] = None,
        warn: bool = True,
) -> Any:
    """把字符串注解解析为类型对象。

    Args:
        ann: 注解。非字符串原样返回。
        owner: 声明该属性的类；用于取 ``owner.__module__`` 的模块 globals。
        field: 字段名，仅用于 warning 文案。
        warn: 解析失败是否发 ``UserWarning``。

    Returns:
        解析出的类型对象；无法解析时返回 ``None``（表示「不做校验」）。
    """
    if not isinstance(ann, str):
        return ann

    try:
        return eval(ann, _eval_globals(owner))  # noqa: S307  受控的注解求值
    except Exception as exc:  # noqa: BLE001  任何失败都 fail-open
        if warn:
            location = ""
            if owner is not None:
                owner_name = getattr(owner, "__qualname__", None) or getattr(
                    owner, "__name__", repr(owner))
                location = f" for {owner_name}"
                if field:
                    location += f".{field}"
            warnings.warn(
                f"Failed to resolve annotation string '{ann}'{location}: "
                f"{exc}; treating the field as untyped (no validation)",
                stacklevel=2,
            )
        return None
