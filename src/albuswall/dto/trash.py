#
"""Trash domain DTOs.

边界契约
--------
* 所有 DTO 由 repo 层的 ``*_from_row`` 从 ``sqlite3.Row`` 构造，
  Service / UI 层只接触 DTO，不接触 Row。
* 布尔字段在 DTO 边界归一化为 ``bool``。DB 里是 INTEGER 0/1。
* 时间字段保持字符串（与 schema ``strftime('%Y-%m-%dT%H:%M:%f','now')``
  同构）。DTO 层不做 datetime 解析——需要时由消费方自行转换。
* DTO 是**只读值对象**：构造后不应修改。TypedDict 不强制这一点，
  由使用约定保证。
"""

from typing import List, Optional, TypedDict
from sqlite3 import Row


# ──────────────────────────────────────────────────────────────────────
# 实体型 DTO
# ──────────────────────────────────────────────────────────────────────
class TrashedAssetDTO(TypedDict):
    """垃圾箱列表项 / 单条详情。

    与 ``TrashRepository._LIST_COLUMNS`` 一一对应。
    """
    id: int
    uuid: str
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


class PurgeCandidateDTO(TypedDict):
    """物理清除流程的候选行。

    故意不携带 ``original_name`` / ``mime_type`` 等展示字段：
    清除流程只需要"定位行 + 定位文件"所需的最小信息。
    与 ``TrashRepository._PURGE_COLUMNS`` 一一对应。
    """
    id: int
    uuid: str
    source_id: int
    file_path: str
    thumb_path: Optional[str]
    thumb_small_path: Optional[str]
    thumb_medium_path: Optional[str]
    thumb_large_path: Optional[str]


class TrashStateDTO(TypedDict):
    """``trash_state`` 单例行。

    ``last_observed_at`` 是单调水位，只增不减。
    """
    last_observed_at: str
    last_cleanup_at: Optional[str]
    last_cleanup_deleted: int
    suspicious_clock_jumps: int


# ──────────────────────────────────────────────────────────────────────
# 结果型 DTO
# ──────────────────────────────────────────────────────────────────────
class RestoreReportDTO(TypedDict):
    """恢复操作结果。

    * ``restored``：实际恢复的 id 列表。
    * ``not_found``：请求恢复但不在垃圾箱中的 id（可能已恢复或不存在）。
    * ``conflicts``：因唯一索引 ``idx_assets_source_file_path_active``
      冲突而失败的 ``(id, file_path)`` 列表。仅在 Service 层逐条
      重试时可能非空；整批失败时抛 ``IntegrityError`` 而非返回本结构。
    """
    restored: List[int]
    not_found: List[int]
    conflicts: List["RestoreConflictDTO"]


class RestoreConflictDTO(TypedDict):
    """单条恢复冲突记录。"""
    asset_id: int
    file_path: str
    reason: str  # 当前统一为 "path_conflict"，未来可扩展


class PurgeReportDTO(TypedDict):
    """物理清除结果。

    * ``purged``：实际删除的 id 列表（Service 需要它定位缩略图目录）。
    * ``thumb_failures``：缩略图清理失败的 ``(asset_id, error)`` 列表。
      DB 层已删，文件残留不会回滚——这条信息用于上层记录告警。
    """
    purged: List[int]
    thumb_failures: List["ThumbFailureDTO"]


class ThumbFailureDTO(TypedDict):
    asset_id: int
    error: str


class CleanupReportDTO(TypedDict):
    """一轮清理周期的完整结果。

    * ``purged``：本轮物理删除的 id 列表。
    * ``clock_jump``：本轮对系统时钟的诊断结论，见 ``ClockJumpDTO``。
    * ``state``：更新后的水位状态，方便上层直接展示。
    """
    purged: List[int]
    clock_jump: "ClockJumpDTO"
    state: TrashStateDTO


class ClockJumpDTO(TypedDict):
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


# ──────────────────────────────────────────────────────────────────────
# Row → DTO 转换器
# ──────────────────────────────────────────────────────────────────────
def trashed_asset_from_row(row: Row) -> TrashedAssetDTO:
    return TrashedAssetDTO(
        id=row["id"],
        uuid=row["uuid"],
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


def purge_candidate_from_row(row: Row) -> PurgeCandidateDTO:
    return PurgeCandidateDTO(
        id=row["id"],
        uuid=row["uuid"],
        source_id=row["source_id"],
        file_path=row["file_path"],
        thumb_path=row["thumb_path"],
        thumb_small_path=row["thumb_small_path"],
        thumb_medium_path=row["thumb_medium_path"],
        thumb_large_path=row["thumb_large_path"],
    )


def trash_state_from_row(row: Row) -> TrashStateDTO:
    return TrashStateDTO(
        last_observed_at=row["last_observed_at"],
        last_cleanup_at=row["last_cleanup_at"],
        last_cleanup_deleted=row["last_cleanup_deleted"],
        suspicious_clock_jumps=row["suspicious_clock_jumps"],
    )
