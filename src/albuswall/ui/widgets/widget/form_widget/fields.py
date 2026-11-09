# -*- coding: utf-8 -*-
"""FormView 专用的复合字段控件。

这三个类只服务于表单行（分割线、颜色、路径），因此随表单包一起走，
不进入 ``widgets/widget`` 的公开面。
"""

from __future__ import annotations

import os

from PySide6.QtCore import QSize
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QColorDialog, QFileDialog, QFrame, QHBoxLayout, QLineEdit, QPushButton,
    QSizePolicy, QWidget,
)

from .row_format import BrowseKind


# ----------------------------------------------------------------------
class SeparatorLine(QFrame):
    """固定高度、水平拉伸的分割线。"""

    def __init__(self, parent=None, thickness: int = 1, color: str = "#d0d0d0"):
        super().__init__(parent)
        self._thickness = max(1, int(thickness))
        self.setFixedHeight(self._thickness)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setStyleSheet(
            f"QFrame {{ background-color: {color}; border: none; }}"
        )

    def sizeHint(self) -> QSize:
        return QSize(0, self._thickness)

    def minimumSizeHint(self) -> QSize:
        return QSize(0, self._thickness)


# ----------------------------------------------------------------------
class ColorField(QWidget):
    """颜色字段：输入框 + 预览色块 + 拾色按钮。

    窄列修复：给 composite 一个最小宽度，内部 edit 也有最小宽度，
    按钮保持 28px，不会被压成 0。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(96)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self.edit = QLineEdit(self)
        self.edit.setMinimumWidth(40)
        self.preview = QFrame(self)
        self.preview.setFixedSize(24, 24)
        self.button = QPushButton("…", self)
        self.button.setFixedWidth(28)

        layout.addWidget(self.edit, 1)
        layout.addWidget(self.preview, 0)
        layout.addWidget(self.button, 0)

        self.edit.textChanged.connect(self._refresh_preview)
        self.button.clicked.connect(self._pick_color)
        self._refresh_preview("")

    def _refresh_preview(self, text: str) -> None:
        text = text.strip()
        color = QColor.fromString(text) if text else QColor()
        if color.isValid():
            hex_argb = color.name(QColor.NameFormat.HexArgb)
            self.preview.setStyleSheet(
                f"QFrame {{ background-color: {hex_argb}; "
                f"border: 1px solid #888; }}"
            )
        else:
            self.preview.setStyleSheet(
                "QFrame { background-color: transparent; "
                "border: 1px dashed #d00; }"
            )

    def _pick_color(self) -> None:
        text = self.edit.text().strip()
        current = QColor.fromString(text) if text else QColor()
        if not current.isValid():
            current = QColor("#ffffff")
        picked = QColorDialog.getColor(
            current, self, "选择颜色",
            QColorDialog.ColorDialogOption.ShowAlphaChannel,
        )
        if picked.isValid():
            if picked.alpha() < 255:
                self.edit.setText(
                    f"rgba({picked.red()}, {picked.green()}, {picked.blue()}, "
                    f"{picked.alpha() / 255:.3f})"
                )
            else:
                self.edit.setText(picked.name().upper())

    # ---- 统一的字段协议（read / write 走这两个方法）----
    def text(self) -> str:
        return self.edit.text()

    def setText(self, text: str) -> None:
        self.edit.setText(text)

    def setReadOnlyMode(self, read_only: bool) -> None:
        self.edit.setReadOnly(read_only)
        self.button.setEnabled(not read_only)


# ----------------------------------------------------------------------
class PathField(QWidget):
    """路径字段：可编辑输入框 + 浏览按钮。

    browse_kind 决定对话框类型：
      FILE      → 打开文件
      DIRECTORY → 选择目录
      SAVE      → 保存文件
      ANY       → 不校验存在性/类型；浏览时按当前文本运行时判定
    """

    def __init__(self, parent=None, browse_kind: BrowseKind = BrowseKind.FILE):
        super().__init__(parent)
        self._browse_kind = browse_kind
        self.setMinimumWidth(72)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self.edit = QLineEdit(self)
        self.edit.setMinimumWidth(40)
        self.button = QPushButton("…", self)
        self.button.setFixedWidth(28)
        layout.addWidget(self.edit, 1)
        layout.addWidget(self.button, 0)
        self.button.clicked.connect(self._browse)

    def _browse(self) -> None:
        start = self.edit.text().strip()
        kind = self._browse_kind
        if kind is BrowseKind.DIRECTORY:
            path = QFileDialog.getExistingDirectory(self, "选择目录", start)
        elif kind is BrowseKind.SAVE:
            path, _ = QFileDialog.getSaveFileName(self, "保存文件", start)
        elif kind is BrowseKind.ANY and start and os.path.isdir(start):
            path = QFileDialog.getExistingDirectory(self, "选择目录", start)
        else:
            path, _ = QFileDialog.getOpenFileName(self, "选择文件", start)
        if path:
            self.edit.setText(path)

    # ---- 统一的字段协议 ----
    def text(self) -> str:
        return self.edit.text()

    def setText(self, text: str) -> None:
        self.edit.setText(text)

    def setReadOnlyMode(self, read_only: bool) -> None:
        self.edit.setReadOnly(read_only)
        self.button.setEnabled(not read_only)
