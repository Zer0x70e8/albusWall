#
"""Trash domain DTOs。

归属（``dto/CONVENTIONS.md`` · DTO 归属与命名）：本文件**未分包**（Command / Query 门槛未达），
两侧用分区注释标出：

* Query         —— ``TrashState``（单例行状态）/ ``TrashedAsset``（列表项 / 详情）；
* 命令侧写前读   —— ``PurgeCandidate``（``find_expired``/``find_any_deleted`` → ``purge_batch``）；
* 事件与结果     —— ``RestoreReport`` / ``CleanupReport`` 及其子结构
  （``RestoreConflict`` / ``ThumbFailure`` / ``ClockJump``）；
* ``PurgeReport`` —— **不在本模块定义**：唯一版本在 ``dto/asset/events.py``
  （uuid 面，七字段）。本模块只做**惰性再导出**，保证旧 import 路径
  ``from albuswall.dto.trash import PurgeReport`` 继续可用（D3）。

边界契约
--------
* 所有 DTO 由 repo 层的 ``*_from_row`` 从 ``sqlite3.Row`` 构造，
  Service / UI 层只接触 DTO，不接触 Row。
* 布尔字段在 DTO 边界归一化为 ``bool``。DB 里是 INTEGER 0/1。
* 时间字段保持字符串（与 schema ``strftime('%Y-%m-%dT%H:%M:%f','now')``
  同构）。DTO 层不做 datetime 解析——需要时由消费方自行转换。
* DTO 是**只读值对象**：``frozen=True`` 强制构造后不可修改。
* ``uuid`` 字段对外统一为 ``UUID``；``from_row`` 负责 ``str→UUID``，
  写路径（repo 的 INSERT 绑定）负责 ``UUID→str``。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import List, Optional
from uuid import UUID

from .utils import to_uuid


# ──────────────────────────────────────────────────────────────────────
# Query —— 实体 / 状态读模型
# ──────────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class TrashState:
    """``trash_state`` 单例行（Query）。

    ``last_observed_at`` 是单调水位，只增不减。
    """
    last_observed_at: str
    last_cleanup_at: Optional[str]
    last_cleanup_deleted: int
    suspicious_clock_jumps: int

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "TrashState":
        """契约：``from_row`` 固定接受 ``sqlite3.Row``，不兼容 ``Mapping``。"""
        return cls(
            last_observed_at=row["last_observed_at"],
            last_cleanup_at=row["last_cleanup_at"],
            last_cleanup_deleted=row["last_cleanup_deleted"],
            suspicious_clock_jumps=row["suspicious_clock_jumps"],
        )


@dataclass(frozen=True, slots=True)
class TrashedAsset:
    """垃圾箱列表项 / 单条详情（Query）。

    与 ``TrashRepository._LIST_COLUMNS`` 一一对应。
    """
    id: int
    uuid: UUID
    file_path: str
    source_id: int
    original_name: str
    mime_type: str
    file_size: int
    width: int
    height: int
    taken_at: Optional[str]
    thumb_path: Optional[str]
    thumb_small_path: Optional[str]
    thumb_medium_path: Optional[str]
    thumb_large_path: Optional[str]
    is_favorite: bool
    deleted_at: Optional[str]
    created_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "TrashedAsset":
        """契约：``from_row`` 固定接受 ``sqlite3.Row``，不兼容 ``Mapping``。"""
        return cls(
            id=row["id"],
            uuid=to_uuid(row["uuid"]),
            file_path=row["file_path"],
            source_id=row["source_id"],
            original_name=row["original_name"],
            mime_type=row["mime_type"],
            file_size=row["file_size"],
            width=row["width"],
            height=row["height"],
            taken_at=row["taken_at"],
            thumb_path=row["thumb_path"],
            thumb_small_path=row["thumb_small_path"],
            thumb_medium_path=row["thumb_medium_path"],
            thumb_large_path=row["thumb_large_path"],
            is_favorite=bool(row["is_favorite"]),
            deleted_at=row["deleted_at"],
            created_at=row["created_at"],
        )


# ──────────────────────────────────────────────────────────────────────
# 命令侧写前读
# ──────────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class PurgeCandidate:
    """物理清除流程的候选行（命令侧写前读）。

    故意不携带 ``original_name`` / ``mime_type`` 等展示字段：
    清除流程只需要"定位行 + 定位文件"所需的最小信息。
    与 ``TrashRepository._PURGE_COLUMNS`` 一一对应。
    """
    id: int
    uuid: UUID
    source_id: int
    file_path: str
    thumb_path: Optional[str]
    thumb_small_path: Optional[str]
    thumb_medium_path: Optional[str]
    thumb_large_path: Optional[str]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "PurgeCandidate":
        """契约：``from_row`` 固定接受 ``sqlite3.Row``，不兼容 ``Mapping``。"""
        return cls(
            id=row["id"],
            uuid=to_uuid(row["uuid"]),
            source_id=row["source_id"],
            file_path=row["file_path"],
            thumb_path=row["thumb_path"],
            thumb_small_path=row["thumb_small_path"],
            thumb_medium_path=row["thumb_medium_path"],
            thumb_large_path=row["thumb_large_path"],
        )


# ──────────────────────────────────────────────────────────────────────
# 事件与结果 —— 命令侧产出，供 service / UI 消费
# ──────────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class RestoreConflict:
    """单条恢复冲突记录。"""
    asset_id: int
    file_path: str
    reason: str  # 当前统一为 "path_conflict"，未来可扩展


@dataclass(frozen=True, slots=True)
class ThumbFailure:
    asset_id: int
    error: str


@dataclass(frozen=True, slots=True)
class ClockJump:
    """时钟诊断结论。

    ``kind`` 取值：
      * ``"normal"``   —— 正常推进，本轮允许清理
      * ``"backward"`` —— 时钟回拨，本轮**不**清理，水位冻结
      * ``"forward"``  —— 时钟前跳，本轮**不**清理，水位受限推进

    ``advised_cutoff``：本轮若允许清理，使用的截止时间；
    为 None 时表示本轮跳过清理（``kind`` 非 normal）。
    """
    kind: str
    advised_cutoff: Optional[str]


@dataclass(frozen=True, slots=True)
class RestoreReport:
    """恢复操作结果。

    * ``restored``：实际恢复的 id 列表。
    * ``not_found``：请求恢复但不在垃圾箱中的 id（可能已恢复或不存在）。
    * ``conflicts``：因唯一索引 ``idx_assets_source_file_path_active``
      冲突而失败的 ``(id, file_path)`` 列表。仅在 Service 层逐条
      重试时可能非空；整批失败时抛 ``IntegrityError`` 而非返回本结构。
    """
    restored: List[int]
    not_found: List[int]
    conflicts: List[RestoreConflict]


@dataclass(frozen=True, slots=True)
class CleanupReport:
    """一轮清理周期的完整结果。

    * ``purged``：本轮物理删除的资产 uuid 列表（与 ``PurgeReport.purged``
      同源——物理清除报告已收敛为 uuid 面，见 ``dto/asset/events.py``）。
    * ``clock_jump``：本轮对系统时钟的诊断结论，见 ``ClockJump``。
    * ``state``：更新后的水位状态，方便上层直接展示。
    * ``thumb_failures``：删缩略图目录失败的条目（``asset_id`` + 错误摘要）。
      定时清理与 ``empty_trash`` 都要能把"删不掉几张"报上来，否则 UI 只能说
      "清理完了"，用户看不到残留的缓存目录。
    """
    purged: List[UUID]
    clock_jump: ClockJump
    state: TrashState
    thumb_failures: List["ThumbFailure"] = field(default_factory=list)


# ──────────────────────────────────────────────────────────────────────
# 兼容再导出（D3）：PurgeReport 的唯一版本在 dto/asset/events.py
# ──────────────────────────────────────────────────────────────────────
# 为什么用模块级 ``__getattr__`` 而不是顶部 import：
#   ``dto/asset/events.py`` 需要 ``from albuswall.dto.trash import ThumbFailure``，
#   顶部 import 会形成 ``dto.trash ↔ dto.asset.events`` 的循环，且哪个模块先被
#   import 决定谁 ImportError。惰性再导出把依赖推到"真正有人取 PurgeReport 时"，
#   两个方向的 import 顺序都成立。
def __getattr__(name: str):
    """惰性再导出 ``PurgeReport``（PEP 562）。"""
    if name == "PurgeReport":
        from albuswall.dto.asset.events import PurgeReport

        globals()["PurgeReport"] = PurgeReport
        return PurgeReport
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
