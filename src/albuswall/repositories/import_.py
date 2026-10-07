#
"""ImportRepository：导入流程相关的 SQLite 仓储。

约定：
    - 列清单 / INSERT 占位符全部从 DTO 派生（DTO.COLUMNS / to_insert_params），
      仓储层不再手写第二份，字段增删只动 DTO。
    - 状态字面量统一走 CandidateStatus 枚举，禁止裸字符串。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Sequence, Optional

from albuswall.common.enums import CandidateStatus
from albuswall.dto.import_ import AssetCandidateCacheDTO, AssetCreateDTO

from .base import BaseRepository
from .utils.sql_helpers import placeholders

# ---- 从 DTO 派生的 SQL 片段（单一事实源：DTO.COLUMNS） ----

# 候选表 SELECT 列清单
_CANDIDATE_COLUMNS = ", ".join(AssetCandidateCacheDTO.COLUMNS)

# 资产 INSERT：列名与占位符均从 AssetCreateDTO.COLUMNS 生成，
# 与 AssetCreateDTO.to_insert_params() 严格同序。
_INSERT_ASSET_SQL = "INSERT INTO assets ({cols}) VALUES ({phs})".format(
    cols=", ".join(AssetCreateDTO.COLUMNS),
    phs=placeholders(len(AssetCreateDTO.COLUMNS)),
)

_UPDATE_CANDIDATE_STATUS_SQL = (
    "UPDATE asset_candidate_cache SET status = ? WHERE id = ?"
)

# SQLite 默认 SQLITE_MAX_VARIABLE_NUMBER = 999；
# 每次查询固定占用 1 个变量（source_id），故单批最多 998 个 path 占位符。
_MAX_PATHS_PER_QUERY = 998

# 单次拉取 pending 的默认上限，避免无 limit 调用时全表扫描。
_DEFAULT_PENDING_LIMIT = 500


class ImportRepository(BaseRepository):
    """导入流程仓储。"""

    # ---------- 读 ----------

    def find_missing_paths(self, source_id: int, *paths: str) -> Sequence[str]:
        """返回既不在 assets 也不在 asset_candidate_cache 中的路径。

        路径语义：传入的 paths 必须与表内存储形式一致（相对路径）。

        Raises:
            ValueError: 去重后的路径数超过单次查询的 SQLite 变量上限
                (_MAX_PATHS_PER_QUERY)。调用方需要自行分块后再调用，
                而不是期望本方法静默返回全量路径。
        """
        if not paths:
            return paths

        unique_paths = list(dict.fromkeys(paths))
        if len(unique_paths) > _MAX_PATHS_PER_QUERY:
            raise ValueError(
                "find_missing_paths() received {} unique paths, exceeding "
                "SQLite per-query variable limit ({}). "
                "Split into batches before calling.".format(
                    len(unique_paths), _MAX_PATHS_PER_QUERY
                )
            )

        ph = placeholders(len(unique_paths))

        rows_assets = self._fetchall(
            "SELECT file_path FROM assets "
            "WHERE source_id = ? AND file_path IN ({})".format(ph),
            [source_id, *unique_paths],
        )
        rows_cache = self._fetchall(
            "SELECT path FROM asset_candidate_cache "
            "WHERE source_id = ? AND path IN ({})".format(ph),
            [source_id, *unique_paths],
        )

        existing: set[str] = set()
        if rows_assets:
            existing.update(row[0] for row in rows_assets)
        if rows_cache:
            existing.update(row[0] for row in rows_cache)

        return [p for p in unique_paths if p not in existing]

    def get_pending_candidates(
            self, limit: Optional[int] = None
    ) -> Sequence[AssetCandidateCacheDTO]:
        """获取状态为 pending 的候选记录，按 id 升序。

        limit=None 时使用 _DEFAULT_PENDING_LIMIT，避免无界全量拉取。
        """
        effective_limit = _DEFAULT_PENDING_LIMIT if limit is None else limit
        sql = (
            "SELECT {cols} FROM asset_candidate_cache "
            "WHERE status = ? ORDER BY id LIMIT ?"
        ).format(cols=_CANDIDATE_COLUMNS)

        rows = self._fetchall(
            sql, (CandidateStatus.PENDING.value, effective_limit)
        )
        if not rows:
            return []
        return [AssetCandidateCacheDTO.from_row(row) for row in rows]

    def get_candidate_status(
            self, candidate_id: int
    ) -> Optional[CandidateStatus]:
        """返回候选记录的当前状态；不存在时返回 None。

        供 ImportService 在 finalize_candidate 返回 None 时，
        判断终态是 SKIPPED（重复导入）还是 FAILED（真失败）。
        """
        row = self._fetchone(
            "SELECT status FROM asset_candidate_cache WHERE id = ?",
            (candidate_id,),
        )
        return CandidateStatus(row[0]) if row else None

    def get_source_paths(self) -> dict[int, str]:
        """返回 {source_id: source_path}。

        供 ImportService 在调度前把 source_id 解析为绝对 source_path，
        再由子进程 process_candidate 拼出真实绝对路径。
        """
        rows = self._fetchall("SELECT id, source_path FROM ingest_source")
        return {row[0]: row[1] for row in rows}

    # ---------- 写 ----------

    def create_candidate_cache_batch(
            self, candidates: Sequence[AssetCandidateCacheDTO]
    ) -> None:
        """批量插入资产候选缓存记录。

        仅写入 (uuid, path, source_id, mime_type) 四个显式列；
        其余列（id / created_at / status / claimed_*）交给 schema DEFAULT。
        """
        if not candidates:
            return

        insert_sql = (
            "INSERT OR IGNORE INTO asset_candidate_cache "
            "(uuid, path, source_id, mime_type) "
            "VALUES (?, ?, ?, ?)"
        )
        params = [
            (c.uuid, c.path, c.source_id, c.mime_type) for c in candidates
        ]

        with self._transaction() as conn:
            conn.executemany(insert_sql, params)

        self.logger.debug(
            "Inserted %d candidate cache records (ignored duplicates if any).",
            len(candidates),
        )

    def create_asset(self, dto: AssetCreateDTO) -> Optional[int]:
        """插入新资产记录，返回自增 ID；失败返回 None。"""
        try:
            with self._transaction() as conn:
                cursor = conn.execute(
                    _INSERT_ASSET_SQL, dto.to_insert_params()
                )
                return cursor.lastrowid
        except Exception as e:
            self.logger.error(
                "Failed to create asset (uuid=%s): %s", dto.uuid, e
            )
            return None

    def claim_next_candidate(
            self, worker_id: str
    ) -> Optional[AssetCandidateCacheDTO]:
        """原子性地领取一条 pending 状态的候选记录，标记为 processing。"""
        now_str = datetime.now(timezone.utc).isoformat()

        pending = CandidateStatus.PENDING.value
        processing = CandidateStatus.PROCESSING.value

        update_sql = """
            UPDATE asset_candidate_cache
            SET status = ?, claimed_by = ?, claimed_at = ?
            WHERE id = ? AND status = ?
        """
        select_id_sql = (
            "SELECT id FROM asset_candidate_cache "
            "WHERE status = ? ORDER BY id LIMIT 1"
        )
        fetch_sql = (
            "SELECT {cols} FROM asset_candidate_cache WHERE id = ?"
        ).format(cols=_CANDIDATE_COLUMNS)

        while True:
            with self._transaction() as conn:
                row = conn.execute(select_id_sql, (pending,)).fetchone()
                if not row:
                    return None
                candidate_id = row[0]
                cursor = conn.execute(
                    update_sql,
                    (processing, worker_id, now_str, candidate_id, pending),
                )
                if cursor.rowcount == 0:
                    # 被其它 worker 抢走，重试
                    continue
                full_row = conn.execute(
                    fetch_sql, (candidate_id,)
                ).fetchone()
                return AssetCandidateCacheDTO.from_row(full_row)

    def finalize_candidate(
            self,
            candidate_id: int,
            dto: AssetCreateDTO,
    ) -> Optional[int]:
        """在一个事务里完成 create_asset + 候选状态更新。

        返回值 / 候选终态：
        - 插入成功                → 返回 asset_uuid，候选置 DONE
        - 唯一约束冲突（重复导入）→ 返回 None，候选置 SKIPPED
        - 其他异常                → 返回 None，候选置 FAILED

        注意：异常/IntegrityError 时外层 with 已经 rollback 完成，
        再通过 _safe_set_candidate_status 用独立事务落状态，
        避免状态更新被回滚吃掉（原实现的 P0）。
        """
        try:
            with self._transaction() as conn:
                cursor = conn.execute(
                    _INSERT_ASSET_SQL, dto.to_insert_params()
                )
                asset_id = cursor.lastrowid
                conn.execute(
                    _UPDATE_CANDIDATE_STATUS_SQL,
                    (CandidateStatus.DONE.value, candidate_id),
                )
                return asset_id

        except sqlite3.IntegrityError as e:
            msg = str(e).lower()
            if "unique" in msg:
                # 命中 assets.uuid / idx_assets_active_hash /
                # idx_assets_source_file_path_active → 重复导入
                self.logger.info(
                    "Duplicate asset; marking candidate as skipped "
                    "(candidate_id=%s, uuid=%s, file_path=%s, source_id=%s): %s",
                    candidate_id, dto.uuid, dto.file_path, dto.source_id, e,
                )
                self._safe_set_candidate_status(
                    candidate_id, CandidateStatus.SKIPPED
                )
                return None

            # 其它 IntegrityError（NOT NULL / CHECK 等）→ 真失败
            self.logger.exception(
                "IntegrityError not caused by uniqueness "
                "(candidate_id=%s, uuid=%s): %s",
                candidate_id, dto.uuid, e,
            )
            self._safe_set_candidate_status(
                candidate_id, CandidateStatus.FAILED
            )
            return None

        except Exception as e:
            self.logger.exception(
                "finalize_candidate failed (candidate_id=%s, uuid=%s): %s",
                candidate_id, dto.uuid, e,
            )
            self._safe_set_candidate_status(
                candidate_id, CandidateStatus.FAILED
            )
            return None

    # noinspection broad-exception
    def _safe_set_candidate_status(
            self, candidate_id: int, status: CandidateStatus
    ) -> None:
        """在独立事务里更新候选状态；失败只记录日志，不再向上抛。

        必须在外层 _transaction 之外调用：_execute 会取 _WRITE_LOCK 并 commit，
        在活动事务内调用会被 BaseRepository 主动拒绝。
        """
        try:
            self._execute(
                _UPDATE_CANDIDATE_STATUS_SQL,
                (status.value, candidate_id),
            )
        except Exception:
            self.logger.exception(
                "Failed to set candidate %s status=%s; "
                "recover_stale_candidates will eventually recycle it.",
                candidate_id, status.value,
            )

    def recover_stale_candidates(self, timeout_seconds: int = 3600) -> int:
        """回收长时间停留在 processing 状态的候选记录，重置为 pending。"""
        cutoff = (
                datetime.now(timezone.utc) - timedelta(seconds=timeout_seconds)
        ).isoformat()

        with self._transaction() as conn:
            cursor = conn.execute(
                "UPDATE asset_candidate_cache "
                "SET status = ?, claimed_by = NULL, claimed_at = NULL "
                "WHERE status = ? "
                "  AND (claimed_at IS NULL OR claimed_at < ?)",
                (
                    CandidateStatus.PENDING.value,
                    CandidateStatus.PROCESSING.value,
                    cutoff,
                ),
            )
            recovered = cursor.rowcount

        if recovered:
            self.logger.info(
                "Recovered %d stale candidate(s) (timeout=%ds).",
                recovered, timeout_seconds,
            )
        else:
            self.logger.debug(
                "No stale candidates to recover (timeout=%ds).",
                timeout_seconds,
            )
        return recovered

    def update_candidate_status(
            self, candidate_id: int, status: CandidateStatus
    ) -> None:
        """更新候选缓存的状态。入参使用 CandidateStatus，禁止裸字符串。"""
        current = self.get_candidate_status(candidate_id)
        if current is not None and not current.can_transition_to(status):
            raise ValueError(
                f"illegal transition: {current.value} -> {status.value} "
                f"(candidate_id={candidate_id})"
            )

        self._execute(
            _UPDATE_CANDIDATE_STATUS_SQL,
            (status.value, candidate_id)
        )

    def mark_candidate_done(self, candidate_id: int) -> None:
        self.update_candidate_status(candidate_id, CandidateStatus.DONE)

    def mark_candidate_failed(self, candidate_id: int) -> None:
        self.update_candidate_status(candidate_id, CandidateStatus.FAILED)
