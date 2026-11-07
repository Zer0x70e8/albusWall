"""值类别推断：类型名 + 选项 → :class:`~albuswall.common.enums.SettingWidget`。

**唯一实现**。展示策略（``service.command.handlers._present``）与设置页契约
（``ui.setting.contract``）都从这里取，避免同一套规则各写一份。

输入来自 ``dump_schema_dict`` 的 ``"type"`` / ``"choices"`` 字段（
:func:`albuswall.configue.schema.type_name` 的输出格式：``"int"`` /
``"int | None"`` / ``"list[str]"`` / ``"Path"``），不读任何配置状态。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from albuswall.common.enums import SettingWidget

__all__ = ["SCALAR_WIDGETS", "WIDGETS", "infer_widget", "strip_optional"]

#: ``configue.schema.type_name`` 的标量输出 → 值类别。键是**完整**的类型名。
SCALAR_WIDGETS: dict[str, str] = {
    "bool": SettingWidget.BOOL.value,
    "int": SettingWidget.INT.value,
    "float": SettingWidget.FLOAT.value,
    "str": SettingWidget.TEXT.value,
    "Path": SettingWidget.PATH.value,
}

#: :func:`infer_widget` 的全部返回值（顺序即文档顺序）。
WIDGETS: tuple[str, ...] = tuple(member.value for member in SettingWidget)


def strip_optional(type_text: str) -> str:
    """剥掉 ``" | None"``；多分支联合（``"int | str"``）原样返回。"""
    if "|" not in type_text:
        return type_text
    parts = [
        part.strip() for part in type_text.split("|")
        if part.strip() and part.strip() != "None"
    ]
    return parts[0] if len(parts) == 1 else type_text


def infer_widget(type_name: str, choices: Sequence[Any] = ()) -> str:
    """类型字符串 + 选项 → 值类别提示（见 :data:`WIDGETS`）。

    ``choices`` 非空优先于类型（``Literal`` / ``Enum`` 的选项就是事实，
    不必再看内层类型）。
    """
    if choices:
        return SettingWidget.CHOICE.value

    base = strip_optional(str(type_name or "").strip())
    scalar = SCALAR_WIDGETS.get(base)
    if scalar is not None:
        return scalar
    if base in ("list", "tuple", "set") or base.startswith(
            ("list[", "tuple[", "set["),
    ):
        return SettingWidget.LIST.value
    if base == "dict" or base.startswith("dict["):
        return SettingWidget.DICT.value
    return SettingWidget.AUTO.value
