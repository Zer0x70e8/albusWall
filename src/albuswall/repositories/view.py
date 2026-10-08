#
""""""

from pathlib import Path
from typing import Any, List, Optional
from uuid import UUID

from albuswall.dto.album import Album, AssetDTO

from .base import BaseRepository


# noinspection SpellCheckingInspection
class ViewRepository(BaseRepository):
    """相簿（专辑）视图仓储（只读）。

    接口契约（P1 收敛）：
        · 所有面向资产的公开方法只接受 uuid、只返回 uuid；
          ``assets.id`` 仅作为内部索引 seek / tie-break 使用。
        · 所有「资产列表」ORDER BY 从 ``_ASSET_ORDER_SQL`` 单点派生，
          保证 scope 与 album 内的「上/下一张」顺序一致。
        · 写操作（收藏 / 软删 / 恢复 / 硬删 / 相簿成员）见 ``AssetRepository``。
    """

    # ---------------- 排序契约（单一来源） ----------------
    # {p} 会被替换成表别名前缀（例如 "a." 或 ""）。
    _ASSET_ORDER_SQL: dict[str, str] = {
        "active": "{p}taken_at IS NULL, {p}taken_at DESC, {p}id DESC",
        "deleted": "{p}deleted_at IS NULL, {p}deleted_at DESC, {p}id DESC",
    }

    _ACTIVE_WHERE = (
        "is_deleted = 0 "
        "AND EXISTS (SELECT 1 FROM ingest_source AS s "
        "            WHERE s.id = assets.source_id AND s.is_deleted = 0)"
    )
    _ACTIVE_WHERE_A = (
        "a.is_deleted = 0 "
        "AND EXISTS (SELECT 1 FROM ingest_source AS s "
        "            WHERE s.id = a.source_id AND s.is_deleted = 0)"
    )

    _SCOPE_WHERE = {
        "active": _ACTIVE_WHERE,  # 原来就是 "is_deleted = 0"
        "deleted": "is_deleted = 1",
    }

    _PHYSICAL_ALBUM_FROM = (
        "assets a "
        "JOIN album_assets aa ON aa.asset_uuid = a.id "
        "JOIN albums AS al ON al.id = aa.album_id"
    )
    _PHYSICAL_ALBUM_WHERE = (
        "al.uuid = ? AND al.is_deleted = 0 AND a.is_deleted = 0"
    )
    _SCOPE_KEY_COL = {"active": "taken_at", "deleted": "deleted_at"}

    _ALLOWED_PREFIXES = frozenset(("", "a."))

    @classmethod
    def _order_by(cls, scope: str, prefix: str = "") -> str:
        if prefix not in cls._ALLOWED_PREFIXES:
            raise ValueError(f"unsupported prefix: {prefix!r}")
        return cls._ASSET_ORDER_SQL[scope].format(p=prefix)

    # ==================================================================
    # 虚拟相册（scope）封面 / 列表
    # ==================================================================

    def get_cover_asset_by_scope(self, scope: str) -> Optional[AssetDTO]:
        where = self._SCOPE_WHERE[scope]
        order = self._order_by(scope)
        row = self._fetchone(
            f"SELECT * FROM assets WHERE {where} ORDER BY {order} LIMIT 1"
        )
        return AssetDTO.from_row(row) if row else None

    def list_assets_by_scope(self, scope: str) -> List[AssetDTO]:
        where = self._SCOPE_WHERE[scope]
        order = self._order_by(scope)
        rows = self._fetchall(
            f"SELECT * FROM assets WHERE {where} ORDER BY {order}"
        )
        return [AssetDTO.from_row(r) for r in (rows or [])]

    def list_asset_uuids_by_scope(self, scope: str) -> List[str]:
        where = self._SCOPE_WHERE[scope]
        order = self._order_by(scope)
        rows = self._fetchall(
            f"SELECT uuid FROM assets WHERE {where} ORDER BY {order}"
        )
        return [r["uuid"] for r in (rows or [])]

    # ==================================================================
    # 相册
    # ==================================================================

    def list_album_uuids(self) -> List[str]:
        """所有未被软删除的相簿 uuid（按 sort_order, id 排序）。"""
        sql = """
                SELECT uuid
                  FROM albums
                 WHERE is_deleted = 0
                 ORDER BY sort_order ASC, id ASC
            """
        self.logger.trace("before _fetchall, sql=%s", sql)
        rows = self._fetchall(sql)
        self.logger.trace(
            "after _fetchall, type=%s, len=%s, repr=%r",
            type(rows).__name__,
            len(rows) if rows is not None else None,
            rows,
        )
        if not rows:
            return []
        return [row["uuid"] for row in rows]

    def get_album(self, album_uuid: str) -> Optional[Album]:
        """按 uuid 获取未被软删除的相簿 DTO。

        封面语义：``cover.is_deleted = 0`` 作为 LEFT JOIN 的 ON 条件，
        软删封面只会让 cover_uuid 变 NULL，不会过滤整行相簿。
        """
        row = self._fetchone(
            """
            SELECT a.id,
                   a.uuid,
                   a.title,
                   a.description,
                   cover.uuid AS cover_uuid,
                   a.album_type AS type,
                   a.created_at,
                   a.modified_at
              FROM albums AS a
              LEFT JOIN assets AS cover
                     ON cover.id = a.cover_asset_id
                    AND cover.is_deleted = 0
             WHERE a.uuid = ?
               AND a.is_deleted = 0
            """,
            (album_uuid,),
        )
        if row is None:
            return None
        return Album(
            id=row["id"],
            uuid=UUID(row["uuid"]),
            title=row["title"],
            description=row["description"] or "",
            cover=UUID(row["cover_uuid"]) if row["cover_uuid"] else None,
            type=row["type"],
            created_at=row["created_at"],
            modified_at=row["modified_at"],
        )

    def get_album_cover(self, album_uuid: str) -> Optional[AssetDTO]:
        """按相簿 uuid 获取其封面资产（相簿与资产都必须未软删）。"""
        row = self._fetchone(
            """
            SELECT asset.*
              FROM albums AS a
              JOIN assets AS asset
                     ON asset.id = a.cover_asset_id
             WHERE a.uuid = ?
               AND a.is_deleted = 0
               AND asset.is_deleted = 0
            """,
            (album_uuid,),
        )
        return AssetDTO.from_row(row) if row else None

    def get_album_uuids_of_asset(self, asset_uuid: str) -> List[str]:
        """包含该资产的所有可见相簿 uuid（排序同 list_album_uuids）。"""
        rows = self._fetchall(
            """
            SELECT al.uuid
              FROM album_assets AS aa
              JOIN albums AS al ON al.id = aa.album_id
              JOIN assets AS a  ON a.id = aa.asset_uuid
             WHERE a.uuid = ?
               AND al.is_deleted = 0
               AND a.is_deleted = 0
             ORDER BY al.sort_order ASC, al.id ASC
            """,
            (asset_uuid,),
        )
        return [r["uuid"] for r in (rows or [])]

    def count_assets(self, album_uuid: str) -> int:
        row = self._fetchone(
            """
            SELECT COUNT(*) AS cnt
              FROM album_assets AS aa
              JOIN albums AS al ON al.id = aa.album_id
              JOIN assets AS a  ON a.id = aa.asset_uuid
             WHERE al.uuid = ?
               AND al.is_deleted = 0
               AND a.is_deleted = 0
            """,
            (album_uuid,),
        )
        return int(row["cnt"]) if row else 0

    # ==================================================================
    # 相册内资产
    # ==================================================================

    def list_assets(
            self,
            album_uuid: str,
            offset: int = 0,
            limit: int = 100,
    ) -> List[AssetDTO]:
        """按唯一确定顺序分页获取相簿内资产 DTO。

        排序读 ``assets.taken_at``（与 scope 排序同源）。

        TODO(P2): 大 OFFSET 后续可改为 cursor 分页。
        """
        order = self._order_by("active", prefix="a.")
        rows = self._fetchall(
            f"""
            SELECT a.*
              FROM album_assets AS aa
              JOIN albums AS al ON al.id = aa.album_id
              JOIN assets AS a  ON a.id = aa.asset_uuid
             WHERE al.uuid = ?
               AND al.is_deleted = 0
               AND a.is_deleted = 0
             ORDER BY {order}
             LIMIT ? OFFSET ?
            """,
            (album_uuid, limit, offset),
        )
        return [AssetDTO.from_row(row) for row in (rows or [])]

    def list_asset_uuids_by_album(self, album_uuid: str) -> List[str]:
        """物理相册内所有可见 asset 的 uuid（同 JOIN / ORDER BY 契约）。"""
        order = self._order_by("active", prefix="a.")
        rows = self._fetchall(
            f"""
            SELECT a.uuid
              FROM album_assets AS aa
              JOIN albums AS al ON al.id = aa.album_id
              JOIN assets AS a  ON a.id = aa.asset_uuid
             WHERE al.uuid = ?
               AND al.is_deleted = 0
               AND a.is_deleted = 0
             ORDER BY {order}
            """,
            (album_uuid,),
        )
        return [r["uuid"] for r in (rows or [])]

    # ==================================================================
    # 单个资产
    # ==================================================================

    def get_asset(
            self, asset_uuid: str, include_deleted: bool = False
    ) -> Optional[AssetDTO]:
        """按 uuid 取 AssetDTO。

        Args:
            asset_uuid: assets.uuid。
            include_deleted: True 时不过滤软删（Trash / 恢复流程使用）。
        """
        where = "uuid = ?"
        if not include_deleted:
            where += " AND is_deleted = 0"
        row = self._fetchone(
            f"SELECT * FROM assets WHERE {where}", (asset_uuid,)
        )
        return AssetDTO.from_row(row) if row else None

    # ==================================================================
    # 上/下一张定位（P2）——入参出参全是 uuid
    # ==================================================================
    # 契约：
    #   - 入参 current_asset_uuid：资产 uuid。
    #   - 返回 (prev_uuid, next_uuid, index, total)：
    #       · prev/next 是 uuid 字符串；
    #       · index 为 1-based；目标行缺失返回 (None, None, 0, 0)。
    #   - 内部 tie-break 走 assets.id，保证与 list_asset_uuids_by_*
    #     完全同源。索引：idx_assets_active_taken_id / _deleted_deleted_at_id。

    def get_neighbours_by_scope(
            self, scope: str, current_asset_uuid: str
    ) -> tuple[Optional[str], Optional[str], int, int]:
        return self._neighbours(
            cur_uuid=current_asset_uuid,
            from_clause="assets",
            where_clause=self._SCOPE_WHERE[scope],
            where_params=(),
            id_col="id",
            uuid_col="uuid",
            key_col=self._SCOPE_KEY_COL[scope],
        )

    def get_neighbours_by_album(
            self, album_uuid: str, current_asset_uuid: str
    ) -> tuple[Optional[str], Optional[str], int, int]:
        return self._neighbours(
            cur_uuid=current_asset_uuid,
            from_clause=self._PHYSICAL_ALBUM_FROM,
            where_clause=self._PHYSICAL_ALBUM_WHERE,
            where_params=(album_uuid,),
            id_col="a.id",
            uuid_col="a.uuid",
            key_col="a.taken_at",
        )

    def locate_in_scope(self, scope: str, asset_uuid: str) -> Optional[int]:
        return self._locate(
            cur_uuid=asset_uuid,
            from_clause="assets",
            where_clause=self._SCOPE_WHERE[scope],
            where_params=(),
            id_col="id",
            uuid_col="uuid",
            key_col=self._SCOPE_KEY_COL[scope],
        )

    def locate_in_album(self, album_uuid: str, asset_uuid: str) -> Optional[int]:
        return self._locate(
            cur_uuid=asset_uuid,
            from_clause=self._PHYSICAL_ALBUM_FROM,
            where_clause=self._PHYSICAL_ALBUM_WHERE,
            where_params=(album_uuid,),
            id_col="a.id",
            uuid_col="a.uuid",
            key_col="a.taken_at",
        )

    # ------------------------------------------------------------------ #
    # 邻居 / 定位 —— 私有分派器
    # ------------------------------------------------------------------ #

    def _neighbours(
            self, *, cur_uuid, from_clause, where_clause, where_params,
            id_col, uuid_col, key_col,
    ) -> tuple[Optional[str], Optional[str], int, int]:
        """统一邻居聚合入口。当前项不在集合内 / 集合为空 → (None, None, 0, 0)。"""
        found, cur_id, cur_key = self._fetch_key(
            from_clause=from_clause, where_clause=where_clause,
            where_params=where_params, id_col=id_col, uuid_col=uuid_col,
            key_col=key_col, cur_uuid=cur_uuid,
        )
        if not found:
            return None, None, 0, 0

        prev_uuid = self._find_prev(
            from_clause=from_clause, where_clause=where_clause,
            where_params=where_params, id_col=id_col, uuid_col=uuid_col,
            key_col=key_col, cur_id=cur_id, cur_key=cur_key,
        )
        next_uuid = self._find_next(
            from_clause=from_clause, where_clause=where_clause,
            where_params=where_params, id_col=id_col, uuid_col=uuid_col,
            key_col=key_col, cur_id=cur_id, cur_key=cur_key,
        )
        index = self._count_before(
            from_clause=from_clause, where_clause=where_clause,
            where_params=where_params, id_col=id_col, key_col=key_col,
            cur_id=cur_id, cur_key=cur_key,
        ) + 1
        total = self._count_total(
            from_clause=from_clause, where_clause=where_clause,
            where_params=where_params,
        )
        return prev_uuid, next_uuid, index, total

    def _locate(
            self, *, cur_uuid, from_clause, where_clause, where_params,
            id_col, uuid_col, key_col,
    ) -> Optional[int]:
        found, cur_id, cur_key = self._fetch_key(
            from_clause=from_clause, where_clause=where_clause,
            where_params=where_params, id_col=id_col, uuid_col=uuid_col,
            key_col=key_col, cur_uuid=cur_uuid,
        )
        if not found:
            return None
        return self._count_before(
            from_clause=from_clause, where_clause=where_clause,
            where_params=where_params, id_col=id_col, key_col=key_col,
            cur_id=cur_id, cur_key=cur_key,
        ) + 1

    # ------------------------------------------------------------------ #
    # 邻居 / 定位 —— 私有工具（索引 seek，不物化全表）
    # ------------------------------------------------------------------ #
    def _fetch_key(self, *, from_clause, where_clause, where_params,
                   id_col, uuid_col, key_col, cur_uuid):
        """按 uuid 取当前项的 (id, 排序键)。

        返回的 cur_id 仅供内部 seek 使用，不向上层暴露。
        当前项不在集合内时返回 (False, None, None)。
        """
        row = self._fetchone(
            f"SELECT {id_col} AS rid, {key_col} AS k FROM {from_clause} "
            f"WHERE {where_clause} AND {uuid_col} = ?",
            (*where_params, cur_uuid),
        )
        if row is None:
            return False, None, None
        return True, row["rid"], row["k"]

    def _find_prev(self, *, from_clause, where_clause, where_params,
                   id_col, uuid_col, key_col, cur_id, cur_key):
        """严格排在 cur 前一项的 uuid；没有返回 None。

        集合排序：`(key IS NULL) ASC, key DESC, id DESC`。
        """
        if cur_key is not None:
            row = self._fetchone(
                f"""SELECT {uuid_col} AS nuuid FROM {from_clause}
                     WHERE {where_clause}
                       AND {key_col} IS NOT NULL
                       AND ({key_col} > ? OR ({key_col} = ? AND {id_col} > ?))
                     ORDER BY {key_col} ASC, {id_col} ASC
                     LIMIT 1""",
                (*where_params, cur_key, cur_key, cur_id),
            )
            return row["nuuid"] if row else None

        row = self._fetchone(
            f"""SELECT {uuid_col} AS nuuid FROM {from_clause}
                 WHERE {where_clause}
                   AND {key_col} IS NULL
                   AND {id_col} > ?
                 ORDER BY {id_col} ASC LIMIT 1""",
            (*where_params, cur_id),
        )
        if row:
            return row["nuuid"]
        row = self._fetchone(
            f"""SELECT {uuid_col} AS nuuid FROM {from_clause}
                 WHERE {where_clause}
                   AND {key_col} IS NOT NULL
                 ORDER BY {key_col} ASC, {id_col} ASC LIMIT 1""",
            where_params,
        )
        return row["nuuid"] if row else None

    def _find_next(self, *, from_clause, where_clause, where_params,
                   id_col, uuid_col, key_col, cur_id, cur_key):
        """严格排在 cur 后一项的 uuid；没有返回 None。"""
        if cur_key is not None:
            row = self._fetchone(
                f"""SELECT {uuid_col} AS nuuid FROM {from_clause}
                     WHERE {where_clause}
                       AND {key_col} IS NOT NULL
                       AND ({key_col} < ? OR ({key_col} = ? AND {id_col} < ?))
                     ORDER BY {key_col} DESC, {id_col} DESC
                     LIMIT 1""",
                (*where_params, cur_key, cur_key, cur_id),
            )
            if row:
                return row["nuuid"]
            row = self._fetchone(
                f"""SELECT {uuid_col} AS nuuid FROM {from_clause}
                     WHERE {where_clause}
                       AND {key_col} IS NULL
                     ORDER BY {id_col} DESC LIMIT 1""",
                where_params,
            )
            return row["nuuid"] if row else None

        row = self._fetchone(
            f"""SELECT {uuid_col} AS nuuid FROM {from_clause}
                 WHERE {where_clause}
                   AND {key_col} IS NULL
                   AND {id_col} < ?
                 ORDER BY {id_col} DESC LIMIT 1""",
            (*where_params, cur_id),
        )
        return row["nuuid"] if row else None

    def _count_before(self, *, from_clause, where_clause, where_params,
                      id_col, key_col, cur_id, cur_key) -> int:
        """严格排在 cur 之前的项数。"""
        if cur_key is not None:
            row = self._fetchone(
                f"""SELECT COUNT(*) AS cnt FROM {from_clause}
                     WHERE {where_clause}
                       AND {key_col} IS NOT NULL
                       AND ({key_col} > ? OR ({key_col} = ? AND {id_col} > ?))""",
                (*where_params, cur_key, cur_key, cur_id),
            )
            return int(row["cnt"]) if row else 0

        row = self._fetchone(
            f"""SELECT COUNT(*) AS cnt FROM {from_clause}
                 WHERE {where_clause} AND {key_col} IS NOT NULL""",
            where_params,
        )
        non_null = int(row["cnt"]) if row else 0
        row = self._fetchone(
            f"""SELECT COUNT(*) AS cnt FROM {from_clause}
                 WHERE {where_clause}
                   AND {key_col} IS NULL AND {id_col} > ?""",
            (*where_params, cur_id),
        )
        null_before = int(row["cnt"]) if row else 0
        return non_null + null_before

    def _count_total(self, *, from_clause, where_clause, where_params) -> int:
        row = self._fetchone(
            f"SELECT COUNT(*) AS cnt FROM {from_clause} WHERE {where_clause}",
            where_params,
        )
        return int(row["cnt"]) if row else 0

    # ==================================================================
    # 资产磁盘路径
    # ==================================================================

    def get_asset_path(
            self, asset_uuid: str, include_deleted: bool = False
    ) -> Optional[str]:
        """按 uuid 取磁盘完整路径。

        路径规则：
          · 有 source_path → ``Path(source_path) / file_path``；
          · 否则 file_path 视为绝对路径直接返回；
          · 资产不存在（或软删且未 include_deleted）→ None。
        """
        where = "a.uuid = ?"
        if not include_deleted:
            where += " AND a.is_deleted = 0"
        row = self._fetchone(
            f"""
            SELECT a.file_path   AS file_path,
                   s.source_path AS source_path
              FROM assets AS a
              LEFT JOIN ingest_source AS s
                     ON s.id = a.source_id
             WHERE {where}
            """,
            (asset_uuid,),
        )
        return self._resolve_full_path(row)

    @staticmethod
    def _resolve_full_path(row: Any) -> Optional[str]:
        if row is None or not row["file_path"]:
            return None
        file_path = Path(row["file_path"])
        source_path = row["source_path"]
        if file_path.is_absolute() or not source_path:
            return str(file_path)
        return str(Path(source_path) / file_path)

    # utils
    @staticmethod
    def _active_asset_where(alias: str = "") -> str:
        """活跃资产的可见性条件。

        活跃 = 资产自身未软删 AND 所属 source 未软删。

        用 EXISTS 子查询而非 JOIN：
          - 不改动 FROM / ORDER BY 结构，现有 partial index 继续命中；
          - ingest_source 通常只有几行，EXISTS 代价可忽略；
          - 对已写死 `a.is_deleted = 0` 的查询也能无缝替换。

        alias 传 "a" 或 ""；空串时引用表名 assets。
        """
        p = f"{alias}." if alias else "assets."
        return (
            f"{p}is_deleted = 0 "
            f"AND EXISTS (SELECT 1 FROM ingest_source AS s "
            f"WHERE s.id = {p}source_id AND s.is_deleted = 0)"
        )
