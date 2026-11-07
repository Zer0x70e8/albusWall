"""设置项的**值类别**枚举（跨层共享值域，只声明成员与值）。

「值类别」描述的是「这个值该怎么显示」，不是配置事实，也不是 Qt 控件；
展示侧（设置页 / CLI 的展示策略）拿它决定用哪种编辑器，缺省时按
:func:`albuswall.common.setting_widget.infer_widget` 兜底推导。

历史：本枚举曾在 ``configue.schema`` 定义、随后被复制到
``service.command.handlers._present`` 与 ``ui.setting.contract`` 各一份
（两份类型、值相同，``isinstance`` 互不认账）。现归口到这里，只留一份。
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["SettingWidget"]


class SettingWidget(StrEnum):
    """值类别（**不是**控件类）。"""

    AUTO = "auto"
    TEXT = "text"
    INT = "int"
    FLOAT = "float"
    BOOL = "bool"
    CHOICE = "choice"
    PATH = "path"
    LIST = "list"
    DICT = "dict"
