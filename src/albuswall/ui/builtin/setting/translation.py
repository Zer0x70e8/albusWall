#
"""设置页的文案入口。

设置页的文字有两个来源：

1. **界面自带**的固定文案（"Settings" / "Reset to default" / …）——
   在各自的控件里用 ``self.tr(...)``；
2. **配置结构带下来**的文案 —— 分组标题 / 分组说明 / 叶子标签 / 提示，
   来自 ``setting.schema`` 的 dump（``services/command.py`` 登记，
   ``builtin_patches.py`` 打补丁）。它们属于「数据」，不是某个控件写的句子，
   所以不能靠 ``self.tr`` 翻译 —— ``QObject.tr`` 的上下文是控件类名，
   同一句话会散到几十个不同的上下文里。

本模块给第 2 类文案一个**固定上下文**（``SettingSchema``）：翻译器只需要
按这个上下文准备一份词表，设置页、``albusctl setting.schema`` 导出的文本、
插件注册的分组补丁就都能共用。

约定：只在**展示时**调用 :func:`schema_tr`，不要用它改 schema dict 本身
（写回配置的仍必须是原始文本，否则配置里会留下译文）。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QCoreApplication

__all__ = ["SETTING_SCHEMA_CONTEXT", "schema_tr"]

#: 配置结构文案的翻译上下文
SETTING_SCHEMA_CONTEXT = "SettingSchema"


def schema_tr(text: Optional[object]) -> str:
    """翻译一条来自配置结构的展示文案；空值原样返回空串。"""
    if text is None:
        return ""
    value = str(text)
    if not value:
        return ""
    return QCoreApplication.translate(SETTING_SCHEMA_CONTEXT, value)
