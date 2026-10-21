#
"""回收站仓储（软删资产列表 / 恢复 / 物理清除 / 单例水位）。

时间契约
--------
本仓储按 **时间字符串的字典序** 做截止判断（``deleted_at <= ?``）。
所有时间列统一为 ``utils.time`` 的格式：

    YYYY-MM-DDTHH:MM:SS.sss     （UTC，3 位毫秒，无时区后缀）

该格式定长且字典序 == 时间序；调用方必须用 ``utils.time.now_str()`` /
``utils.time.cutoff_str()`` 生成参数，禁止直接 ``datetime.isoformat()``
（微秒 + 时区后缀会让比较静默失准）。
"""

from typing import List, Optional

from albuswall.dto.trash import (
    PurgeCandidateDTO,
    TrashedAssetDTO,
    TrashStateDTO,
)
from albuswall.repositories.base import BaseRepository
from albuswall.utils.time import now_str

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
        return self._fetch_all_dtos(
            TrashedAssetDTO,
            f"""
            SELECT {self._LIST_COLUMNS}
              FROM assets
             WHERE is_deleted = 1
             {self._ORDER_SQL}
             LIMIT ? OFFSET ?
            """,
            (limit, offset),
        )

    def get_deleted(self, asset_id: int) -> Optional[TrashedAssetDTO]:
        return self._fetch_one_dto(
            TrashedAssetDTO,
            f"""
            SELECT {self._LIST_COLUMNS}
              FROM assets
             WHERE id = ? AND is_deleted = 1
            """,
            (asset_id,),
        )

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

    def find_expired(
            self, cutoff_iso: str, limit: int
    ) -> List[PurgeCandidateDTO]:
        """取出 ``deleted_at <= cutoff_iso`` 的软删资产（到期清理候选）。

        Args:
            cutoff_iso: 截止时间，**必须**是 ``utils.time.format_iso`` 的
                格式（``YYYY-MM-DDTHH:MM:SS.sss``，UTC、3 位毫秒、无时区
                后缀）。该格式下字典序 == 时间序，可直接做 ``<=`` 比较。
                请用 ``utils.time.cutoff_str(timedelta(...))`` 生成，不要用
                ``datetime.isoformat()``（精度 / 时区不同会静默比较错）。
            limit: 最多返回行数；``<= 0`` 返回空列表。
        """
        if limit <= 0:
            return []
        return self._fetch_all_dtos(
            PurgeCandidateDTO,
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

    def find_any_deleted(self, limit: int) -> List[PurgeCandidateDTO]:
        """不判断到期，直接按删除时间升序取任意软删资产（清空回收站用）。"""
        if limit <= 0:
            return []
        return self._fetch_all_dtos(
            PurgeCandidateDTO,
            f"""
            SELECT {self._PURGE_COLUMNS}
              FROM assets
             WHERE is_deleted = 1
             ORDER BY deleted_at ASC, id ASC
             LIMIT ?
            """,
            (limit,),
        )

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
            # 时间格式与其它落库时间一致，统一由 utils.time 生成后绑参。
            self._execute(
                """
                INSERT OR IGNORE INTO trash_state (id, last_observed_at)
                VALUES (?, ?)
                """,
                (self._STATE_ID, now_str()),
            )
            row = self._fetchone(
                "SELECT * FROM trash_state WHERE id = ?",
                (self._STATE_ID,),
            )
        if row is None:
            raise RuntimeError(
                "trash_state singleton row missing after INSERT OR IGNORE"
            )
        return TrashStateDTO.from_row(row)
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
