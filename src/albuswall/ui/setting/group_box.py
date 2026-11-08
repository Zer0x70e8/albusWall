#
"""设置页的分组框：在 :class:`CollapsibleGroupBox` 上补两样东西。

* **标题带计数**（``item_count`` 用于空组判断与折叠阈值；范围 / 高级开关
  变化时由 :meth:`SettingGroupBox.refresh_counts` 就地更新）；
* **稳定的锚点 key**（``qualname``；视图的 ``anchor_map`` 与左侧导航共用）。

折叠只影响布局，不碰 binder——收起分组不会丢用户改动。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from ..widgets import CollapsibleGroupBox
from . import strings

__all__ = ["SettingGroupBox"]


class SettingGroupBox(CollapsibleGroupBox):
    """一个分组节点（可嵌套）。"""

    def __init__(
            self,
            group: Mapping[str, Any],
            *,
            visible_count: int,
            depth: int = 0,
            expanded: bool = True,
            parent: QWidget | None = None,
    ) -> None:
        self.group: dict[str, Any] = dict(group)
        self.group_key: str = str(group["key"])
        self.depth: int = int(depth)
        self._title = str(group.get("title") or self.group_key)
        self._total = int(group.get("item_count") or 0)
        self._advanced = bool(group.get("advanced"))
        super().__init__(self._title_text(visible_count), parent)
        self.setProperty("settingKey", self.group_key)
        self.setProperty("nestingLevel", self.depth)

        content = self.contentWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(4, 2, 4, 6)
        layout.setSpacing(2)
        self._content_layout = layout

        description = str(group.get("description") or "")
        self._description = QLabel(description, content)
        self._description.setObjectName("SettingGroupDescription")
        self._description.setWordWrap(True)
        self._description.setVisible(bool(description))
        layout.addWidget(self._description)

        if not expanded:
            self.setChecked(False)

    # ------------------------------------------------------------------
    # 标题（计数随范围 / 高级开关变化）
    # ------------------------------------------------------------------
    def _title_text(self, visible_count: int) -> str:
        suffix = ""
        if visible_count or self._total:
            suffix = (
                strings.GROUP_COUNT_OF.format(
                    visible=visible_count, total=self._total,
                )
                if visible_count != self._total
                else strings.GROUP_COUNT.format(visible=visible_count)
            )
            suffix = f" · {suffix}"
        if self._advanced:
            suffix += f" · {strings.GROUP_ADVANCED}"
        return f"{self._title}{suffix}"

    def refresh_counts(self, visible_count: int) -> None:
        """只改标题里的计数（分组内容不变时用，不重建控件）。"""
        self._base_title = self._title_text(visible_count)
        self._update_title()

    # ------------------------------------------------------------------
    # 内容装配
    # ------------------------------------------------------------------

    @property
    def content_layout(self) -> QVBoxLayout:
        """往里加叶子行 / 子分组用的布局。"""
        return self._content_layout

    def add_row_widget(self, widget: QWidget) -> None:
        self._content_layout.addWidget(widget)

    def add_group_widget(self, widget: SettingGroupBox) -> None:
        self._content_layout.addWidget(widget)

