#
"""``albuswall.ui.window.source`` —— 导入源页（卡片列表 + 表单详情）。

纯视图层：卡片 model / delegate 与表单的回填全部由外部完成。
"""

from __future__ import annotations

from .core import IngestSource
from .detail_widget import IngestSourceDetailWidget

__all__ = ["IngestSource", "IngestSourceDetailWidget"]
