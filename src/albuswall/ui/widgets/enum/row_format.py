# -*- coding: utf-8 -*-
"""表单行格式枚举。

本枚举只决定「控件形态」，不承担值语义：值语义（JSON / PATH / BOOL ...）
全部通过 add_row 的 transform / validator 钩子表达。
"""

from enum import Enum, auto


# noinspection SpellCheckingInspection
class RowFormat(Enum):
    """表单行的输入控件类型（形态，全局）。"""

    KV = auto()  # 单行文本
    NUMBER = auto()  # 数值 spinbox（int / float）
    MULTILINE = auto()  # 多行文本（QPlainTextEdit）
    ENUM = auto()  # 下拉（显示 label，提交 value）
    CHECKBOX = auto()  # 复选框（value=bool，text=旁文案）
    RADIO = auto()  # 互斥二选一（items=[(text, value)]）
    COLOR = auto()  # 颜色：输入框 + 预览 + 拾色
    KEYSEQUENCE = auto()  # QKeySequenceEdit
    PASSWORD = auto()  # QLineEdit, EchoMode.Password
    SLIDER = auto()  # 水平滑块
    FILE = auto()  # 路径（文件）：可编辑 + 浏览
    DIRECTORY = auto()  # 路径（目录）：可编辑 + 浏览
    READONLY = auto()  # 只读文本
    SUBFORM = auto()  # 嵌套子表单（get_value 返回 dict/list）
    SEPARATOR = auto()  # 整行分割线
    LABEL = auto()  # 整行纯展示文本
