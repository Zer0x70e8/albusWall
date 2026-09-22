#
""""""

from datetime import datetime, timezone, timedelta
from typing import Sequence, Optional

from albuswall.dto.import_ import AssetCandidateCacheDTO, AssetCreateDTO

from .base import BaseRepository

_CANDIDATE_COLUMNS = (
    "id, uuid, path, source_id, mime_type, created_at, "
    "status, claimed_by, claimed_at"
)

_INSERT_ASSET_SQL = """
    INSERT INTO assets (
        uuid, file_path, source_id,
        thumb_path, thumb_small_path, thumb_medium_path,
        original_name, mime_type, file_hash,
        file_size, width, height,
        taken_at, city, exif_json,
        is_favorite, is_deleted, deleted_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


class ImportRepository(BaseRepository):
    """"""

    # ---------- 内部工具 ----------

    @staticmethod
    def _asset_params(dto: AssetCreateDTO) -> tuple:
        return (
            dto.uuid, dto.file_path, dto.source_id,
            dto.thumb_path, dto.thumb_small_path, dto.thumb_medium_path,
            dto.original_name, dto.mime_type, dto.file_hash,
            dto.file_size, dto.width, dto.height,
            dto.taken_at, dto.city, dto.exif_json,
            dto.is_favorite, dto.is_deleted, dto.deleted_at,
        )

    # ---------- 读 ----------

    def find_missing_paths(self, source_id: int, *paths: str) -> Sequence[str]:
        """...（保持原实现）..."""
        if not paths or len(paths) > 998:
            return paths

        unique_paths = list(dict.fromkeys(paths))
        placeholders = ','.join('?' for _ in unique_paths)

        rows_assets = self._fetchall(
            "SELECT file_path FROM assets "
            "WHERE source_id = ? AND file_path IN ({})".format(placeholders),
            [source_id, *unique_paths],
        )
        rows_cache = self._fetchall(
            "SELECT path FROM asset_candidate_cache "
            "WHERE source_id = ? AND path IN ({})".format(placeholders),
            [source_id, *unique_paths],
        )

        existing = set()
        if rows_assets:
            existing.update(row[0] for row in rows_assets)
        if rows_cache:
            existing.update(row[0] for row in rows_cache)

        return [p for p in unique_paths if p not in existing]

    def get_pending_candidates(
            self, limit: Optional[int] = None
    ) -> Sequence[AssetCandidateCacheDTO]:
        """获取所有状态为 pending 的候选记录，按 id 升序。"""
        sql = (
            "SELECT {cols} FROM asset_candidate_cache "
            "WHERE status = 'pending' ORDER BY id"
        ).format(cols=_CANDIDATE_COLUMNS)

        if limit is not None:
            rows = self._fetchall(sql + " LIMIT ?", (limit,))
        else:
            rows = self._fetchall(sql)

        if not rows:
            return []
        return [AssetCandidateCacheDTO.from_row(row) for row in rows]

    # ---------- 写 ----------

    def create_candidate_cache_batch(
            self, candidates: Sequence[AssetCandidateCacheDTO]
    ) -> None:
        """批量插入资产候选缓存记录。"""
        if not candidates:
            return

        insert_sql = """
            INSERT OR IGNORE INTO asset_candidate_cache
                (uuid, path, source_id, mime_type)
            VALUES (?, ?, ?, ?)
        """
        params = [
            (c.uuid, c.path, c.source_id, c.mime_type) for c in candidates
        ]

        # ✅ 使用基类事务：自动加写锁 + commit/rollback
        with self._transaction() as conn:
            conn.executemany(insert_sql, params)

        self.logger.debug(
            "Inserted %d candidate cache records (ignored duplicates if any).",
            len(candidates),
        )

    def create_asset(self, dto: AssetCreateDTO) -> Optional[int]:
        """插入新资产记录，返回自增 ID；失败返回 None。"""
        try:
            # ✅ 在事务内取 lastrowid，确保连接仍存活
            with self._transaction() as conn:
                cursor = conn.execute(_INSERT_ASSET_SQL, self._asset_params(dto))
                return cursor.lastrowid
        except Exception as e:
            self.logger.error(
                "Failed to create asset (uuid=%s): %s", dto.uuid, e
            )
            return None

    def claim_next_candidate(
            self, worker_id: str
    ) -> Optional[AssetCandidateCacheDTO]:
        """
        原子性地领取一条 pending 状态的候选记录，标记为 processing。
        无 pending 记录时返回 None。
        """
        now_str = datetime.now(timezone.utc).isoformat()
        update_sql = """
            UPDATE asset_candidate_cache
            SET status = 'processing', claimed_by = ?, claimed_at = ?
            WHERE id = ? AND status = 'pending'
        """
        select_id_sql = (
            "SELECT id FROM asset_candidate_cache "
            "WHERE status = 'pending' ORDER BY id LIMIT 1"
        )
        fetch_sql = (
            "SELECT {cols} FROM asset_candidate_cache WHERE id = ?"
        ).format(cols=_CANDIDATE_COLUMNS)

        # ✅ SELECT + UPDATE + SELECT 全在一个事务内，保证原子性
        while True:
            with self._transaction() as conn:
                row = conn.execute(select_id_sql).fetchone()
                if not row:
                    return None
                candidate_id = row[0]
                cursor = conn.execute(
                    update_sql,
                    (worker_id, now_str, candidate_id)
                )
                if cursor.rowcount == 0:
                    continue  # 被其他 worker 抢走，尝试下一条
                full_row = conn.execute(
                    fetch_sql,
                    (candidate_id,)
                ).fetchone()
                return AssetCandidateCacheDTO.from_row(full_row)

    def finalize_candidate(
            self,
            candidate_id: int,
            dto: AssetCreateDTO
    ) -> Optional[int]:
        """在一个事务里完成 create_asset + 候选状态更新。

        - asset 插入成功：候选标记 done
        - asset 插入失败：候选标记 failed
        - 返回 asset_id；异常时事务回滚，返回 None（候选仍为 pending，可重试）
        """
        try:
            with self._transaction() as conn:
                cursor = conn.execute(_INSERT_ASSET_SQL, self._asset_params(dto))
                asset_id = cursor.lastrowid
                if asset_id:
                    conn.execute(
                        "UPDATE asset_candidate_cache SET status='done' WHERE id=?",
                        (candidate_id,),
                    )
                else:
                    conn.execute(
                        "UPDATE asset_candidate_cache SET status='failed' WHERE id=?",
                        (candidate_id,),
                    )
                return asset_id
        except Exception as e:
            self.logger.exception(
                "finalize_candidate failed (candidate_id=%s, uuid=%s): %s",
                candidate_id, dto.uuid, e,
            )
            return None

    def recover_stale_candidates(self, timeout_seconds: int = 3600) -> int:
        """回收长时间停留在 processing 状态的候选记录，重置为 pending。

        适用于进程崩溃 / worker 被 kill 后，被其领走但从未 finalize 的
        候选记录。这些记录若不回收会永久堵塞后续处理。

        判定规则：
            status = 'processing'
            且 (claimed_at IS NULL 或 claimed_at < now - timeout)

        Args:
            timeout_seconds: 超过该时长视为 stale，默认 1 小时。传 0 表示
                回收全部 processing 记录。

        Returns:
            被重置为 pending 的记录条数。
        """
        cutoff = (
                datetime.now(timezone.utc) - timedelta(seconds=timeout_seconds)
        ).isoformat()

        # claimed_at 由 claim_next_candidate 以 UTC isoformat 写入，
        # 字符串按字典序比较即等价于时间先后比较。
        with self._transaction() as conn:
            cursor = conn.execute(
                "UPDATE asset_candidate_cache "
                "SET status = 'pending', claimed_by = NULL, claimed_at = NULL "
                "WHERE status = 'processing' "
                "  AND (claimed_at IS NULL OR claimed_at < ?)",
                (cutoff,),
            )
            recovered = cursor.rowcount

        if recovered:
            self.logger.info(
                "Recovered %d stale candidate(s) (timeout=%ds).",
                recovered, timeout_seconds,
            )
        else:
            self.logger.debug(
                "No stale candidates to recover (timeout=%ds).", timeout_seconds,
            )
        return recovered

    def update_candidate_status(self, candidate_id: int, status: str) -> None:
        """更新候选缓存的状态。"""
        # ✅ 走基类 _execute：自动加写锁 + commit
        self._execute(
            "UPDATE asset_candidate_cache SET status = ? WHERE id = ?",
            (status, candidate_id),
        )

    def mark_candidate_done(self, candidate_id: int) -> None:
        self.update_candidate_status(candidate_id, 'done')

    def mark_candidate_failed(self, candidate_id: int) -> None:
        self.update_candidate_status(candidate_id, 'failed')
