#
"""typed value → 各后端的「可写入形态」。

两个方向：

- ``to_ini``：typed → ``str``（委托给 ``utils.ini_parser._deconvert``，语义与
  读取侧 ``_convert`` 对称）
- ``to_json_native``：typed → JSON 原生（``None/bool/int/float/str/list/dict``）

两者**共享同一套「什么该写成什么」的约定**，但终端形态不同：

===========  ==========================  ==============================
输入          ``to_ini`` 输出             ``to_json_native`` 输出
===========  ==========================  ==============================
``None``     ``""``                      ``None``
``bool``     ``"true"`` / ``"false"``    ``True`` / ``False``
``Enum``     ``str(value.value)``        ``value.value``
``Path``     ``str(value)``              ``str(value)``
序列          ``", ".join(...)``（必要时 JSON 数组）  原生 JSON 数组
dict         JSON 对象字符串            原生 JSON 对象
===========  ==========================  ==============================

TODO（不在本阶段）：把 ``_deconvert`` 里「泛型递归 + 自定义 writer + to_ini()」
的骨架抽成两边共用的 visitor，避免两处各自维护。当前 ``to_json_native`` 只做
**值驱动**递归（INI 那边是 converter 驱动），代价是「空容器元素类型」无从得知，
收益是逻辑极简：
``converter`` 参数保留在签名里只为将来对齐两者，现在不参与判定。
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

from .errors import ConfigError
from .utils.ini_parser import _deconvert

__all__ = ["to_ini", "to_json_native"]

#: JSON 里可以原样落盘的标量
_JSON_SCALARS = (bool, int, float, str)


def to_ini(
        value: Any,
        converter: Any = None,
        custom_converters: Optional[Mapping[Any, Callable[[str], Any]]] = None,
        custom_serializers: Optional[Mapping[Any, Callable[[Any], str]]] = None,
) -> str:
    """typed value → INI 文本（``_deconvert`` 的瘦封装，保留同一套语义）。"""
    return _deconvert(
        value,
        converter,
        dict(custom_converters or {}),
        dict(custom_serializers or {}),
    )


def to_json_native(
        value: Any,
        converter: Any = None,  # noqa: ARG001  预留：将来与 to_ini 共享递归
        custom_converters: Optional[Mapping[Any, Callable[[str], Any]]] = None,  # noqa: ARG001
        custom_serializers: Optional[Mapping[Any, Callable[[Any], str]]] = None,
) -> Any:
    """typed value → JSON 原生结构。

    规则（与 ``to_ini`` 的差异见模块 docstring）：

    - 标量原样；``None`` 就是 ``None``（JSON 的 ``null``，不是空串）
    - ``Path`` → ``str``
    - ``Enum`` → ``value.value``（保证与读取侧 ``EnumType(value)`` 对称）
    - 序列 → 原生数组；``set`` / ``frozenset`` 按 ``str`` 排序后输出，保证稳定
    - ``Mapping`` → 原生对象（键强制 ``str``）
    - 注册过 serializer → 用它的返回值
    - 带 ``to_ini()`` 的对象 → 用它的返回值（在 JSON 里存字符串）
    - 其它 → ``ConfigError``：写出去读不回来的值宁可失败
    """
    serializers = custom_serializers or {}

    if value is None:
        return None
    if isinstance(value, _JSON_SCALARS):
        return value
    if isinstance(value, Path):
        return str(value)

    # 自定义 writer 优先于内置规则（与 _deconvert 的顺序一致）
    writer = serializers.get(type(value))
    if writer is not None:
        try:
            return writer(value)
        except Exception as exc:  # noqa: BLE001  统一成 ConfigError
            raise ConfigError(
                f"custom serializer for {type(value)!r} failed on {value!r}: {exc}"
            ) from exc

    if isinstance(value, Enum):
        return to_json_native(value.value)

    to_ini_method = getattr(value, "to_ini", None)
    if callable(to_ini_method):
        text = to_ini_method()
        if not isinstance(text, str):
            raise ConfigError(
                f"{type(value).__name__}.to_ini() must return str, "
                f"got {type(text).__name__}"
            )
        return text

    if isinstance(value, (list, tuple)):
        return [to_json_native(item) for item in value]
    if isinstance(value, (set, frozenset)):
        ordered = sorted(value, key=str)
        return [to_json_native(item) for item in ordered]
    if isinstance(value, Mapping):
        return {str(k): to_json_native(v) for k, v in value.items()}

    raise ConfigError(
        f"cannot serialize {type(value).__name__} ({value!r}) to JSON; "
        f"register a custom serializer or implement to_ini()"
    )
