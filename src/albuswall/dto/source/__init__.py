#
"""Ingest source 域 DTO 包（Command / Query / 事件与结果 分包）。

按 ``dto/CONVENTIONS.md`` · DTO 归属与命名 分包：

* ``dto.source.query``    —— 读模型 ``IngestSource``（+ 命令侧快照别名
  ``IngestSourceSnapshot``）与域常量 ``MANUAL_SOURCE_ID``；
* ``dto.source.command``  —— 写模型 ``IngestSourceCreate`` / ``IngestSourceUpdate``
  与表单快照 ``IngestSourceForm``，以及 ``form_from_source`` 单向桥；
* ``dto.source.events``   —— 命令侧产出的事件 / 报告
  （``SourceScanFinished`` / ``SourcePurgeReport``）。

本 ``__init__`` 只做**显式**再导出（无通配、无新增语义）。新代码建议直接从
所属子模块 import，以便在 import 行上就能看出读写侧。
"""

from .command import IngestSourceCreate, IngestSourceForm, IngestSourceUpdate, form_from_source
from .events import SourcePurgeReport, SourceScanFinished
from .query import MANUAL_SOURCE_ID, IngestSource, IngestSourceSnapshot

__all__ = [
    "IngestSource",
    "IngestSourceCreate",
    "IngestSourceForm",
    "IngestSourceSnapshot",
    "IngestSourceUpdate",
    "MANUAL_SOURCE_ID",
    "SourcePurgeReport",
    "SourceScanFinished",
    "form_from_source",
]
