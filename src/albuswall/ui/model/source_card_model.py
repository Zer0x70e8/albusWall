#
"""导入源卡片模型。

导入源页左侧的卡片列表（标题 / 描述 / 路径 / 标签）。旧版把 ``CardItem``
当成纯展示结构，卡片之外的事情（新建草稿、删除、编辑）全靠 presenter 手动
增删；这里补上了业务级入口（``upsert`` / ``remove_by_uuid``），因为「按标识
对齐」是列表模型的通用需求，不该每个调用方各写一遍。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from PySide6.QtCore import QModelIndex

from .base import EntryListModel, attr_of
from .roles import SourceCardRole

__all__ = ["SourceCard", "SourceCardModel"]


@dataclass(slots=True)
class SourceCard:
    """一张导入源卡片。"""

    uuid: str = ""
    title: str = ""
    description: str = ""
    path: str = ""
    tags: list[str] = field(default_factory=list)

    @classmethod
    def from_object(cls, source: Any) -> "SourceCard":
        """从任意「带同名属性的导入源对象」构造（DTO / Row / dict 均可）。"""
        raw_tags = attr_of(source, "tags") or ()
        return cls(
            uuid=_text(attr_of(source, "uuid")),
            title=_text(attr_of(source, "title") or attr_of(source, "name")),
            description=_text(attr_of(source, "description")),
            path=_text(
                attr_of(source, "path") or attr_of(source, "source_path")),
            tags=[_text(tag) for tag in raw_tags if _text(tag)],
        )


def _text(value: Any) -> str:
    return "" if value is None else str(value)


class SourceCardModel(EntryListModel[SourceCard]):
    """导入源卡片列表模型。

    Roles::

        DisplayRole / ToolTipRole     → 标题
        SourceCardRole.Uuid           → 标识
        SourceCardRole.Title          → 标题
        SourceCardRole.Description    → 描述
        SourceCardRole.Path           → 路径
        SourceCardRole.Tags           → 标签名序列
    """

    ROLE_FIELDS = {
        SourceCardRole.Uuid: "uuid",
        SourceCardRole.Title: "title",
        SourceCardRole.Description: "description",
        SourceCardRole.Path: "path",
        SourceCardRole.Tags: "tags",
    }
    DISPLAY_FIELD = "title"
    TOOLTIP_FIELD = "description"

    # ------------------------------------------------------------------ #
    # 装载
    # ------------------------------------------------------------------ #
    def set_cards(self, cards: Iterable[Any]) -> None:
        """用任意导入源对象序列重建卡片列表。"""
        self.set_entries(self.to_cards(cards))

    @staticmethod
    def to_cards(cards: Iterable[Any]) -> list[SourceCard]:
        entries: list[SourceCard] = []
        for card in cards:
            if isinstance(card, SourceCard):
                entries.append(card)
            else:
                entries.append(SourceCard.from_object(card))
        return entries

    def tags_at(self, row: int) -> list[str]:
        """某行的标签序列（拷贝；改它不影响模型）。"""
        card = self.entry_at(row)
        return list(card.tags) if card is not None else []

    # ------------------------------------------------------------------ #
    # 按标识对齐的增量修改
    # ------------------------------------------------------------------ #
    def row_of(self, uuid: Any) -> int:
        """返回标识对应的行号；找不到时返回 ``-1``。"""
        target = _text(uuid)
        for row, card in enumerate(self._entries):
            if card.uuid == target:
                return row
        return -1

    def index_of(self, uuid: Any) -> QModelIndex:
        row = self.row_of(uuid)
        return self.index(row, 0) if row >= 0 else QModelIndex()

    def upsert(self, card: SourceCard | Any) -> int:
        """有则更新、无则追加；返回该行行号。

        更新时只在该行内容真变了的前提下发 ``dataChanged``。
        """
        entry = (card if isinstance(card, SourceCard)
                 else SourceCard.from_object(card))
        row = self.row_of(entry.uuid) if entry.uuid else -1
        if row < 0:
            return self.append(entry)
        self.update_entry(row, entry)
        return row

    def remove_by_uuid(self, uuid: Any) -> bool:
        """按标识删除一行；找不到返回 ``False``。"""
        row = self.row_of(uuid)
        if row < 0:
            return False
        return self.remove_rows(row, 1)

    def set_tags(self, row: int, tags: Iterable[Any]) -> bool:
        """替换某行的标签并通知重绘。"""
        card = self.entry_at(row)
        if card is None:
            return False
        new_tags = [_text(tag) for tag in tags if _text(tag)]
        if card.tags == new_tags:
            return False
        card.tags = new_tags
        self.touch_row(row, (SourceCardRole.Tags,))
        return True

    def tooltip(self, row: int) -> str:
        """一行卡片的完整提示文本（标题 / 描述 / 路径拼行）。"""
        card = self.entry_at(row)
        if card is None:
            return ""
        return "\n".join(
            part for part in (card.title, card.description, card.path) if part)
