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

from typing import List, Optional, Sequence, Union
from uuid import UUID

from albuswall.dto.utils import to_uuid
from albuswall.dto.trash import (
    PurgeCandidate,
    TrashedAsset,
    TrashState,
)
from albuswall.repositories.base import BaseRepository
from albuswall.utils.time import now_str

from .utils.sql_helpers import as_uuid_str, chunked, placeholders


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

    def list_deleted(self, limit: int, offset: int) -> List[TrashedAsset]:
        if limit <= 0 or offset < 0:
            return []
        return self._fetch_all_dtos(
            TrashedAsset,
            f"""
            SELECT {self._LIST_COLUMNS}
              FROM assets
             WHERE is_deleted = 1
             {self._ORDER_SQL}
             LIMIT ? OFFSET ?
            """,
            (limit, offset),
        )

    def get_deleted(self, asset_id: int) -> Optional[TrashedAsset]:
        return self._fetch_one_dto(
            TrashedAsset,
            f"""
            SELECT {self._LIST_COLUMNS}
              FROM assets
             WHERE id = ? AND is_deleted = 1
            """,
            (asset_id,),
        )

    def get_deleted_by_uuid(
            self, asset_uuid: Union[UUID, str]
    ) -> Optional[TrashedAsset]:
        """按 uuid 取回收站里的单行（只在 ``is_deleted = 1`` 时命中）。

        做什么：``SELECT ... FROM assets WHERE uuid = ? AND is_deleted = 1``。
        返回：``TrashedAsset``（含内部 ``id``，仅供 ``TrashService`` 做
        uuid→id 桥接使用，不对外暴露）；不在回收站 / uuid 不存在 → ``None``。
        抛：不抛（非法 uuid → ``None``）。
        前置条件：无。
        原子性：单条只读查询，非事务；与后续 ``restore_batch`` 之间非原子
        （并发恢复会让 UPDATE 变成 0 行，由调用链按"实际恢复"记账）。
        关系：``get_deleted(asset_id)`` 的 uuid 版；前者保留给既有 id 面
        调用点，两者不互相替代。
        """
        try:
            uuid = as_uuid_str(to_uuid(asset_uuid))
        except (ValueError, AttributeError, TypeError):
            return None
        return self._fetch_one_dto(
            TrashedAsset,
            f"""
            SELECT {self._LIST_COLUMNS}
              FROM assets
             WHERE uuid = ? AND is_deleted = 1
            """,
            (uuid,),
        )

    def list_deleted_by_uuids(
            self, asset_uuids: Sequence[Union[UUID, str]]
    ) -> List[TrashedAsset]:
        """按 uuid 批量取回收站里的行（G-R3 的批量桥接查询）。

        做什么：``WHERE is_deleted = 1 AND uuid IN (...)``，超过
        ``IN_CLAUSE_CHUNK`` 自动分片。
        返回：命中的 ``TrashedAsset`` 列表（顺序不保证与入参一致）；
        不在回收站 / uuid 不存在的**不会**出现在结果里——调用方用"缺失"
        表达 not_found。
        抛：不抛（非法 uuid 被跳过）。
        前置条件：无。
        原子性：只读快照；多分片之间不保证同一快照。
        关系：``list_deleted(limit, offset)`` 的 uuid 定向版本（不分页、
        不排序），不替代后者。
        """
        wanted: list[str] = []
        seen: set[str] = set()
        for value in asset_uuids:
            try:
                key = as_uuid_str(to_uuid(value))
            except (ValueError, AttributeError, TypeError):
                continue
            if key in seen:
                continue
            seen.add(key)
            wanted.append(key)
        if not wanted:
            return []

        rows: List[TrashedAsset] = []
        for chunk in chunked(wanted):
            rows.extend(self._fetch_all_dtos(
                TrashedAsset,
                f"""
                SELECT {self._LIST_COLUMNS}
                  FROM assets
                 WHERE is_deleted = 1
                   AND uuid IN ({placeholders(len(chunk))})
                """,
                tuple(chunk),
            ))
        return rows

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
    ) -> List[PurgeCandidate]:
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
            PurgeCandidate,
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

    def find_any_deleted(self, limit: int) -> List[PurgeCandidate]:
        """不判断到期，直接按删除时间升序取任意软删资产（清空回收站用）。"""
        if limit <= 0:
            return []
        return self._fetch_all_dtos(
            PurgeCandidate,
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

    def get(self) -> TrashState:
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
        return TrashState.from_row(row)
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
