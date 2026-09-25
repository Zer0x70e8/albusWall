# albuswall/dto/sentinel.py
"""DTO 层 PATCH 语义的缺失标记。

约定：
- ``UNSET``   —— 调用方**没有提供**这个字段，Repository 应跳过，不写 DB。
- ``None``    —— 调用方**显式提供了 NULL**，Repository 应写入 NULL。
- 其他值      —— 调用方提供了具体值，Repository 应写入该值。

这与 ``attrs.NOTHING``、``Pydantic`` 的 ``model_fields_set``、
``SQLAlchemy`` 的 ``no_value`` 是同一思路。
"""

from __future__ import annotations

from typing import Final


class _UnsetType:
    """``UNSET`` 的类型。单例，模块级只应存在一个实例。"""

    _instance: "_UnsetType | None" = None

    def __new__(cls) -> "_UnsetType":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        # noinspection bad-return
        return cls._instance

    def __repr__(self) -> str:
        return "UNSET"

    def __bool__(self) -> bool:
        # 明确 falsy：`if not value` 也能识别，但语义上请用 `is UNSET`
        return False

    def __copy__(self) -> "_UnsetType":
        return self

    def __deepcopy__(self, _memo) -> "_UnsetType":
        return self

    def __reduce__(self):
        # pickle 往返后仍是同一个单例
        return _get_unset, ()


def _get_unset() -> "_UnsetType":
    return UNSET


UNSET: Final = _UnsetType()
UnsetType = _UnsetType
