#
"""Asset 域 DTO 包（当前只有事件与结果侧）。

按 ``dto/CONVENTIONS.md`` · R3 分包门槛：本域**尚未**出现 Command / Query
写读模型（资产写操作尚未实现 UI 接线），因此没有 ``command.py`` /
``query.py``；只从 ``events.py`` 起步。资产读模型 ``Asset`` 仍在
``dto/album.py``（历史归属，``Album`` / ``Asset`` 同表读路径）。

本 ``__init__`` 只做**显式**再导出（无通配），新代码建议直接从
``albuswall.dto.asset.events`` import，便于在 import 行上看出读写侧。
"""

from .events import (
    ClassifiedAssets,
    FavoriteResult,
    MembershipResult,
    MoveResult,
    PurgeReport,
    RestoreConflict,
    RestoreConflictReason,
    RestoreResult,
    SkippedAsset,
    SkipReason,
    TrashResult,
)

__all__ = [
    "ClassifiedAssets",
    "FavoriteResult",
    "MembershipResult",
    "MoveResult",
    "PurgeReport",
    "RestoreConflict",
    "RestoreConflictReason",
    "RestoreResult",
    "SkipReason",
    "SkippedAsset",
    "TrashResult",
]
