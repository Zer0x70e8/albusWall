#
""""""

from __future__ import annotations

from PySide6.QtCore import Qt

# 自定义 role 从 UserRole 起分配，避免与 Qt 内置 role 冲突。
#   KEY_ROLE        —— 字段显示名（本地化后），如 "拍摄时间"
#   VALUE_ROLE      —— 字段显示值（已格式化），如 "2024-01-01 12:00"
#   RAW_VALUE_ROLE  —— 字段原始值，供排序 / 复制 / 二次处理
KEY_ROLE = Qt.ItemDataRole.UserRole + 1
VALUE_ROLE = Qt.ItemDataRole.UserRole + 2
RAW_VALUE_ROLE = Qt.ItemDataRole.UserRole + 3
