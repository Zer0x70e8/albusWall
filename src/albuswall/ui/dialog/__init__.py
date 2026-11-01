#
"""``albuswall.ui.dialog`` —— 通用输入对话框。

对话框只返回**用户输入**，不碰数据层：谁来真正建标签、怎么落库，由调用方
在 ``accepted`` 之后自己做。
"""

from __future__ import annotations

from .tag_edit_dialog import TagEditDialog, TagEditMode

__all__ = ["TagEditDialog", "TagEditMode"]
