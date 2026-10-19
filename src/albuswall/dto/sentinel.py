#
"""DTO 层 PATCH 语义的缺失标记。

约定：
- ``UNSET``   —— 调用方**没有提供**这个字段，Repository 应跳过，不写 DB。
- ``None``    —— 调用方**显式提供了 NULL**，Repository 应写入 NULL。
- 其他值      —— 调用方提供了具体值，Repository 应写入该值。

这与 ``attrs.NOTHING``、``Pydantic`` 的 ``model_fields_set``、
``SQLAlchemy`` 的 ``no_value`` 是同一思路。

命名
----
* :data:`PatchField`  —— PATCH 语义字段（强调"未设置 / 显式 None / 具体值"）。
* :data:`MaybeUnset`  —— :data:`PatchField` 的中性别名，语义完全等价，
  用于非 PATCH 场景（如 ``BackpressureEvent`` 按 ``reason`` 动态附加的
  可选字段）。二者等价关系由
  ``tests/dto/test_contracts.py::test_maybe_unset_alias_matches_patch_field``
  守护。
"""

from __future__ import annotations

from typing import Final, TypeAlias, TypeVar, Union

T = TypeVar("T")


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

# PATCH 字段：未设置 = UNSET；显式 None = 置 NULL；否则为 T
PatchField: TypeAlias = Union[T, None, UnsetType]

# 中性别名：与 PatchField 语义完全等价，命名适用于非 PATCH 场景
# （例如 BackpressureEvent 的 seq/priority/kind 等按 reason 动态附加字段）。
# 保持为 PatchField 的别名而非独立 Union，保证二者类型上不可区分，
# 避免出现"看着等价实际上不一样"的隐性分裂。
MaybeUnset: TypeAlias = PatchField

# TODO(py3.12): 最低支持版本升到 3.12 后，替换为 PEP 695 的
#     type PatchField[T] = T | None | UnsetType
#     type MaybeUnset[T] = T | None | UnsetType
# 现在用 Union 写法是因为 3.11 下 `T | None` 不返回 _UnionGenericAlias，
# 泛型替换不生效；3.12 起 `|` 可直接用于 TypeVar 组合。
