"""内置动作包：导入即登记（``@shortcut``）。

``factory.load()`` 在 Qt 阶段 import 本包；``shortcuts.py`` 只保留窗口 /
焦点 / 搜索 / 详情页的既有动作，新增的库级动作按主题拆在下面几个模块里。
"""
from __future__ import annotations

from . import detail, library, preview, selection, view

__all__ = [
    "DETAIL_ACTION_IDS", "LIBRARY_ACTION_IDS", "PREVIEW_ACTION_IDS",
    "SELECTION_ACTION_IDS", "VIEW_ACTION_IDS", "action_ids",
]

DETAIL_ACTION_IDS = detail.DETAIL_ACTION_IDS
LIBRARY_ACTION_IDS = library.LIBRARY_ACTION_IDS
PREVIEW_ACTION_IDS = preview.PREVIEW_ACTION_IDS
SELECTION_ACTION_IDS = selection.SELECTION_ACTION_IDS
VIEW_ACTION_IDS = view.VIEW_ACTION_IDS


def action_ids() -> tuple[str, ...]:
    """本包登记的全部动作 id（供测试 / 帮助面板核对）。"""
    return (
        *LIBRARY_ACTION_IDS, *PREVIEW_ACTION_IDS, *DETAIL_ACTION_IDS,
        *VIEW_ACTION_IDS, *SELECTION_ACTION_IDS,
    )
