#
"""共享内核枚举：按域分文件，此处汇总导出。

DTO / repo / service / UI 一律从这里取枚举，不在各层声明平行枚举。
"""

from .import_ import FileTypeCheckMode
from .source import CandidateStatus
from .task import BackpressureReason, ExecutorType, PermitKind
from .thumbnail import ThumbSpec
from .trigger import UpdateMode

__all__ = [
    "BackpressureReason",
    "CandidateStatus",
    "ExecutorType",
    "FileTypeCheckMode",
    "PermitKind",
    "ThumbSpec",
    "UpdateMode",
]
