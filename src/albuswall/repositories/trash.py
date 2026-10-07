#
""""""

from typing import List, Optional

from albuswall.repositories.base import BaseRepository
from albuswall.dto.trash import (
    PurgeCandidateDTO,
    TrashedAssetDTO,
    TrashStateDTO,
    purge_candidate_from_row,
    trashed_asset_from_row,
    trash_state_from_row,
)

from .utils.sql_helpers import placeholders


class TrashRepository(BaseRepository):
    _ORDER_SQL = "ORDER BY deleted_at DESC, id DESC"

    _LIST_COLUMNS = (
        "id, uuid, file_path, source_id, original_name, mime_type, "
        "file_size, width, height, taken_at, "
        "thumb_path, thumb_small_path, thumb_medium_path, thumb_large_path, "
        "is_favorite, deleted_at, created_at"
    )

    _PURGE_COLUMNS = (
        "id, uuid, source_id, file_path, "
        "thumb_path, thumb_small_path, thumb_medium_path, thumb_large_path"
    )

    # ---------- 查询 ----------

    def list_deleted(self, limit: int, offset: int) -> List[TrashedAssetDTO]:
        if limit <= 0 or offset < 0:
            return []
        rows = self._fetchall(
            f"""
            SELECT {self._LIST_COLUMNS}
              FROM assets
             WHERE is_deleted = 1
             {self._ORDER_SQL}
             LIMIT ? OFFSET ?
            """,
            (limit, offset),
        )
        return [trashed_asset_from_row(r) for r in rows]

    def get_deleted(self, asset_id: int) -> Optional[TrashedAssetDTO]:
        row = self._fetchone(
            f"""
            SELECT {self._LIST_COLUMNS}
              FROM assets
             WHERE id = ? AND is_deleted = 1
            """,
            (asset_id,),
        )
        return trashed_asset_from_row(row) if row is not None else None

    # ---------- 恢复 ----------

    def restore_batch(self, asset_ids: List[int]) -> List[int]:
        """批量恢复，返回**实际被恢复**的 id 列表。

        行为
        ----
        * 事务内 SELECT-then-UPDATE，保证"确认仍在软删"与"置回 active"
          原子。
        * 若 UPDATE 因唯一索引 ``idx_assets_source_file_path_active``
          冲突抛 ``IntegrityError``，整个事务回滚，**所有** id 都未恢复。
          逐条重试由 Service 层负责。
        * ``trg_assets_modified_at`` 会自动刷新 ``modified_at``。
        """
        if not asset_ids:
            return []
        ph = placeholders(len(asset_ids))
        with self._transaction() as conn:
            rows = conn.execute(
                f"""
                SELECT id FROM assets
                 WHERE is_deleted = 1
                   AND id IN ({ph})
                """,
                tuple(asset_ids),
            ).fetchall()
            confirmed = [r["id"] for r in rows]
            if not confirmed:
                return []
            conn.execute(
                f"""
                UPDATE assets
                   SET is_deleted = 0,
                       deleted_at = NULL
                 WHERE id IN ({placeholders(len(confirmed))})
                """,
                tuple(confirmed),
            )
        return confirmed

    # ---------- 物理清除 ----------

    def find_expired(self, cutoff_iso: str, limit: int) -> List[PurgeCandidateDTO]:
        if limit <= 0:
            return []
        rows = self._fetchall(
            f"""
            SELECT {self._PURGE_COLUMNS}
              FROM assets
             WHERE is_deleted = 1
               AND deleted_at IS NOT NULL
               AND deleted_at <= ?
             ORDER BY deleted_at ASC, id ASC
             LIMIT ?
            """,
            (cutoff_iso, limit),
        )
        return [purge_candidate_from_row(r) for r in rows]

    def find_any_deleted(self, limit: int) -> List[PurgeCandidateDTO]:
        if limit <= 0:
            return []
        rows = self._fetchall(
            f"""
            SELECT {self._PURGE_COLUMNS}
              FROM assets
             WHERE is_deleted = 1
             ORDER BY deleted_at ASC, id ASC
             LIMIT ?
            """,
            (limit,),
        )
        return [purge_candidate_from_row(r) for r in rows]

    def purge_batch(self, asset_ids: List[int]) -> List[int]:
        """物理删除；返回实际删除的 id 列表。未列出的 id 表示未在软删状态。"""
        if not asset_ids:
            return []
        ph = placeholders(len(asset_ids))
        with self._transaction() as conn:
            rows = conn.execute(
                f"""
                SELECT id FROM assets
                 WHERE is_deleted = 1
                   AND id IN ({ph})
                """,
                tuple(asset_ids),
            ).fetchall()
            confirmed = [r["id"] for r in rows]
            if not confirmed:
                return []
            conn.execute(
                f"""
                DELETE FROM assets
                 WHERE id IN ({placeholders(len(confirmed))})
                """,
                tuple(confirmed),
            )
        return confirmed

    def count_deleted(self) -> int:
        """垃圾箱内资产总数。命中 partial index ``idx_assets_deleted``。"""
        row = self._fetchone(
            "SELECT COUNT(*) AS c FROM assets WHERE is_deleted = 1"
        )
        return int(row["c"]) if row is not None else 0


class TrashStateRepository(BaseRepository):
    _STATE_ID = 1

    def get(self) -> TrashStateDTO:
        row = self._fetchone(
            "SELECT * FROM trash_state WHERE id = ?",
            (self._STATE_ID,),
        )
        if row is None:
            self._execute(
                """
                INSERT OR IGNORE INTO trash_state (id, last_observed_at)
                VALUES (?, strftime('%Y-%m-%dT%H:%M:%f','now'))
                """,
                (self._STATE_ID,),
            )
            row = self._fetchone(
                "SELECT * FROM trash_state WHERE id = ?",
                (self._STATE_ID,),
            )
        if row is None:
            raise RuntimeError(
                "trash_state singleton row missing after INSERT OR IGNORE"
            )
        return trash_state_from_row(row)

    def update(
            self,
            *,
            last_observed_at: str,
            last_cleanup_at: str,
            cleaned: int,
            clock_jumps: int = 0,
    ) -> None:
        self._execute(
            """
            UPDATE trash_state
               SET last_observed_at       = ?,
                   last_cleanup_at        = ?,
                   last_cleanup_deleted   = ?,
                   suspicious_clock_jumps = suspicious_clock_jumps + ?
             WHERE id = ?
            """,
            (
                last_observed_at,
                last_cleanup_at,
                cleaned,
                clock_jumps,
                self._STATE_ID,
            ),
        )
