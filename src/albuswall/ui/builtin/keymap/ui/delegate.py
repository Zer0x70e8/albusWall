"""按键列 delegate：编辑时给一个 ``QKeySequenceEdit``，提交后透传给 model。"""
from __future__ import annotations

from typing import Any

from PySide6.QtCore import QModelIndex
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QKeySequenceEdit, QStyledItemDelegate, QWidget

__all__ = ["KeyColumnDelegate"]


class KeyColumnDelegate(QStyledItemDelegate):
    """按键列的可编辑 delegate（单段组合键）。"""

    def createEditor(self, parent: QWidget, option: Any, index: QModelIndex) -> QWidget:  # noqa: N802
        editor = QKeySequenceEdit(parent)
        editor.setMaximumSequenceLength(1)
        editor.setClearButtonEnabled(True)
        return editor

    def setEditorData(self, editor: QWidget, index: QModelIndex) -> None:  # noqa: N802
        assert isinstance(editor, QKeySequenceEdit)
        key = str(index.data() or "")
        editor.setKeySequence(QKeySequence.fromString(
            key, QKeySequence.SequenceFormat.PortableText))

    def setModelData(  # noqa: N802
            self, editor: QWidget, model: Any, index: QModelIndex,
    ) -> None:
        assert isinstance(editor, QKeySequenceEdit)
        sequence = editor.keySequence()
        if sequence.isEmpty() or sequence.count() != 1:
            return
        text = sequence.toString(QKeySequence.SequenceFormat.PortableText)
        model.setData(index, text)
