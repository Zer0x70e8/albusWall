# -*- coding: utf-8 -*-
""""""

from __future__ import annotations

from typing import TYPE_CHECKING

from .._lazy import install

_LAZY: dict[str, str] = {
    "RowFormat": "row_format",
}

__all__ = [
    "RowFormat",
]

if TYPE_CHECKING:  # pragma: no cover - 仅供 IDE / 类型检查器
    from .row_format import RowFormat

install(__name__, globals(), _LAZY)
