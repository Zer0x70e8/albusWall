#
""""""

from pathlib import Path
from typing import Any, List, Optional
from uuid import UUID

from albuswall.dto.album import Album, AssetDTO

from .base import BaseRepository


class ViewRepository(BaseRepository):
    """相簿（专辑）视图仓储。

    职责边界（P1）：
        本仓储只做“读”查询。收藏 / 软删 / 恢复 / 硬删等写操作
        应放到独立的 ``AssetRepository``，不要塞进来污染视图职责。
    """

    # ---------------- 排序契约（单一来源） ----------------
    # 所有面向“资产列表”的 ORDER BY 必须从 _ASSET_ORDER_SQL 派生，
    # 这样 scope（active/deleted）与 album 内列表的“上/下一张”顺序才能一致。
    #
    # {p} 会被替换成表别名前缀（例如 "a." 或 ""）。
    _ASSET_ORDER_SQL: dict[str, str] = {
        "active": "{p}taken_at IS NULL, {p}taken_at DESC, {p}id DESC",
        "deleted": "{p}deleted_at IS NULL, {p}deleted_at DESC, {p}id DESC",
    }

    _SCOPE_WHERE = {"active": "is_deleted = 0", "deleted": "is_deleted = 1"}

    # 物理相册的统一 FROM/JOIN，所有查询共用
    _PHYSICAL_ALBUM_FROM = (
        "assets a "
        "JOIN album_assets aa ON aa.asset_id = a.id "
        "JOIN albums AS al ON al.id = aa.album_id"
    )
    _PHYSICAL_ALBUM_WHERE = (
        "al.uuid = ? AND al.is_deleted = 0 AND a.is_deleted = 0"
    )
    # 排序键列按 scope 分派（与 _ASSET_ORDER_SQL 的键一一对应）
    _SCOPE_KEY_COL = {"active": "taken_at", "deleted": "deleted_at"}

    _ALLOWED_PREFIXES = frozenset(("", "a."))

    @classmethod
    def _order_by(cls, scope: str, prefix: str = "") -> str:
        """返回给定 scope 下的 ORDER BY 片段。

        Args:
            scope: ``"active"`` 或 ``"deleted"``。
            prefix: 列名表别名前缀（如 ``"a."``），默认无别名。
        """
        if prefix not in cls._ALLOWED_PREFIXES:  # assist
            raise ValueError(f"unsupported prefix: {prefix!r}")
        return cls._ASSET_ORDER_SQL[scope].format(p=prefix)

    # ==================================================================
    # 虚拟相册（All / Trash）封面与 id 列表
    # ==================================================================

    def get_cover_asset_by_scope(self, scope: str) -> Optional[AssetDTO]:
        where = self._SCOPE_WHERE[scope]
        order = self._order_by(scope)
        row = self._fetchone(
            f"SELECT * FROM assets WHERE {where} ORDER BY {order} LIMIT 1"
        )
        return AssetDTO.from_row(row) if row else None

    def list_asset_ids_by_scope(self, scope: str) -> List[int]:
        """按 scope（active / deleted）列出 asset 的整数 id。

        与 get_cover_asset_by_scope 使用同一套 WHERE / ORDER BY。
        """
        where = self._SCOPE_WHERE[scope]
        order = self._order_by(scope)
        rows = self._fetchall(
            f"SELECT id FROM assets WHERE {where} ORDER BY {order}"
        )
        return [int(r["id"]) for r in (rows or [])]

    def list_asset_dtos_by_scope(self, scope: str) -> List[AssetDTO]:
        """按 scope 列出 asset 的完整 DTO（供虚拟相册内容视图使用）。"""
        where = self._SCOPE_WHERE[scope]
        order = self._order_by(scope)
        rows = self._fetchall(
            f"SELECT * FROM assets WHERE {where} ORDER BY {order}"
        )
        return [AssetDTO.from_row(r) for r in (rows or [])]

    # ==================================================================
    # 相簿（专辑）
    # ==================================================================

    def get_active_album_uuids(self) -> List[str]:
        """获取所有未被软删除的相簿 uuid 列表。

        Returns:
            相簿 uuid 列表；若不存在则返回空列表。
        """
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

    def get_album_by_uuid(self, album_uuid: str) -> Optional[Album]:
        """根据 uuid 获取未被软删除的相簿 DTO。

        封面语义与 get_album_cover_by_uuid 保持一致（P1）：
            ``cover.is_deleted = 0`` 作为 LEFT JOIN 的 ON 条件，
            软删封面只会让 cover_uuid 变成 NULL，不会把整行相簿过滤掉。
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

    def get_album_cover_by_uuid(self, album_uuid: str) -> Optional[AssetDTO]:
        """根据相簿 uuid 获取其封面资产。

        仅当相簿存在、未被软删除、且封面资产本身也未被软删除时才返回。
        """
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
        if row is None:
            return None
        return AssetDTO.from_row(row)

    def get_album_uuids_of_asset(self, asset_uuid: str) -> List[str]:
        """返回包含该资产的所有可见相簿 uuid（排序与 get_active_album_uuids 一致）。"""
        rows = self._fetchall(
            """
            SELECT al.uuid
              FROM album_assets AS aa
              JOIN albums AS al ON al.id = aa.album_id
              JOIN assets AS a  ON a.id = aa.asset_id
             WHERE a.uuid = ?
               AND al.is_deleted = 0
               AND a.is_deleted = 0
             ORDER BY al.sort_order ASC, al.id ASC
            """,
            (asset_uuid,),
        )
        return [r["uuid"] for r in (rows or [])]

    # ==================================================================
    # 相簿内的资产
    # ==================================================================

    def count_assets(self, album_uuid: str) -> int:
        """统计相簿内未被软删除的资产数量。"""
        row = self._fetchone(
            """
            SELECT COUNT(*) AS cnt
              FROM album_assets AS aa
              JOIN albums AS al ON al.id = aa.album_id
              JOIN assets AS a  ON a.id = aa.asset_id
             WHERE al.uuid = ?
               AND al.is_deleted = 0
               AND a.is_deleted = 0
            """,
            (album_uuid,),
        )
        return int(row["cnt"]) if row else 0

    def list_assets(
            self,
            album_uuid: str,
            offset: int = 0,
            limit: int = 100,
    ) -> List[AssetDTO]:
        """按唯一确定顺序分页获取相簿内资产。

        排序直接读 ``assets.taken_at``（而不是冗余的
        ``album_assets.asset_taken_at``），与 scope 排序同源，避免
        “上/下一张顺序不一致”。

        TODO(P2): 大 OFFSET 后续可改为 cursor 分页。
        """
        order = self._order_by("active", prefix="a.")
        rows = self._fetchall(
            f"""
            SELECT a.*
              FROM album_assets AS aa
              JOIN albums AS al ON al.id = aa.album_id
              JOIN assets AS a  ON a.id = aa.asset_id
             WHERE al.uuid = ?
               AND al.is_deleted = 0
               AND a.is_deleted = 0
             ORDER BY {order}
             LIMIT ? OFFSET ?
            """,
            (album_uuid, limit, offset),
        )
        return [AssetDTO.from_row(row) for row in (rows or [])]

    # 语义化别名，与 list_asset_dtos_by_scope 对称。
    list_asset_dtos_by_album = list_assets

    def list_asset_ids_by_album(self, album_uuid: str) -> List[int]:
        """列出物理相册内所有可见 asset 的整数 id。

        与 list_assets 使用同一套 JOIN + ORDER BY 契约。
        """
        order = self._order_by("active", prefix="a.")
        rows = self._fetchall(
            f"""
            SELECT a.id
              FROM album_assets AS aa
              JOIN albums AS al ON al.id = aa.album_id
              JOIN assets AS a  ON a.id = aa.asset_id
             WHERE al.uuid = ?
               AND al.is_deleted = 0
               AND a.is_deleted = 0
             ORDER BY {order}
            """,
            (album_uuid,),
        )
        return [int(r["id"]) for r in (rows or [])]

    # ==================================================================
    # 资产 DTO / 元数据
    # ==================================================================

    def get_asset_by_id(
            self, asset_id: int, include_deleted: bool = False
    ) -> Optional[AssetDTO]:
        """按整数主键取 AssetDTO。

        Args:
            asset_id: assets.id。
            include_deleted: 为 True 时不过滤软删（供 Trash / 恢复流程使用）。
        """
        where = "id = ?"
        if not include_deleted:
            where += " AND is_deleted = 0"
        row = self._fetchone(
            f"SELECT * FROM assets WHERE {where}",
            (int(asset_id),),
        )
        return AssetDTO.from_row(row) if row else None

    def get_asset_by_uuid(
            self, asset_uuid: str, include_deleted: bool = False
    ) -> Optional[AssetDTO]:
        """按 uuid 取 AssetDTO。

        Args:
            asset_uuid: assets.uuid。
            include_deleted: 为 True 时不过滤软删（供 Trash / 恢复流程使用）。
        """
        where = "uuid = ?"
        if not include_deleted:
            where += " AND is_deleted = 0"
        row = self._fetchone(
            f"SELECT * FROM assets WHERE {where}",
            (asset_uuid,),
        )
        return AssetDTO.from_row(row) if row else None

    def get_asset_metadata(
            self, asset_uuid: str, include_deleted: bool = False
    ) -> Optional[dict[str, Any]]:
        """返回资产元数据的浅层 dict（等同 AssetDTO.to_dict()）。

        对于只关心 exif / 宽高 / 拍摄时间等字段的调用方，避免暴露整个 DTO。
        """
        dto = self.get_asset_by_uuid(asset_uuid, include_deleted=include_deleted)
        return dto.to_dict() if dto else None

    # ==================================================================
    # 上/下一张定位（P2）
    # ==================================================================
    # 契约：
    #   - 排序必须与 list_asset_ids_by_album / list_asset_ids_by_scope 完全同源，
    #     否则缩略图网格翻页会出现“错位一张”。
    #   - 返回 (prev_id, next_id, index, total)：
    #       · index 为 1-based 位置；
    #       · 目标行缺失（相册为空 / 资产不属于该相册）时返回 (None, None, 0, 0)。
    #   - 实现走基于排序键的索引 seek（O(log N)），不做窗口函数全表物化。
    #     排序契约 = `(key IS NULL) ASC, key DESC, id DESC`，其中 NULL 段在末尾。
    #     SQLite 的 DESC 索引里 NULL 天然聚在末尾、段内 id DESC 有序，
    #     因此 prev/next/index/total 全部能落在
    #     idx_assets_active_taken_id / idx_assets_deleted_deleted_at_id 上。

    def get_asset_neighbours_by_scope(
            self, scope: str, current_asset_id: int
    ) -> tuple[Optional[int], Optional[int], int, int]:
        """虚拟相册（All / Trash）内的上/下一张定位。

        Args:
            scope: ``"active"`` 或 ``"deleted"``（见 _SCOPE_WHERE）。
            current_asset_id: 目标资产的整数主键。
        """
        return self._neighbours(
            cur_id=int(current_asset_id),
            from_clause="assets",
            where_clause=self._SCOPE_WHERE[scope],
            where_params=(),
            id_col="id",
            key_col=self._SCOPE_KEY_COL[scope],
        )

    def get_asset_neighbours_by_album(
            self, album_uuid: str, current_asset_id: int
    ) -> tuple[Optional[int], Optional[int], int, int]:
        """物理相册内的上/下一张定位。

        排序读 ``assets.taken_at``（与 list_assets 同源），
        不用冗余的 ``album_assets.asset_taken_at``。
        """
        return self._neighbours(
            cur_id=int(current_asset_id),
            from_clause=self._PHYSICAL_ALBUM_FROM,
            where_clause=self._PHYSICAL_ALBUM_WHERE,
            where_params=(album_uuid,),
            id_col="a.id",
            key_col="a.taken_at",
        )

    def locate_in_scope(self, scope: str, asset_id: int) -> Optional[int]:
        """只返回 1-based index；资产不在该 scope 中返回 None。"""
        return self._locate(
            cur_id=int(asset_id),
            from_clause="assets",
            where_clause=self._SCOPE_WHERE[scope],
            where_params=(),
            id_col="id",
            key_col=self._SCOPE_KEY_COL[scope],
        )

    def locate_in_album(
            self, album_uuid: str, asset_id: int
    ) -> Optional[int]:
        """只返回 1-based index；资产不在该相册中返回 None。"""
        return self._locate(
            cur_id=int(asset_id),
            from_clause=self._PHYSICAL_ALBUM_FROM,
            where_clause=self._PHYSICAL_ALBUM_WHERE,
            where_params=(album_uuid,),
            id_col="a.id",
            key_col="a.taken_at",
        )

    # ------------------------------------------------------------------ #
    # 邻居 / 定位 —— 私有分派器
    # ------------------------------------------------------------------ #
    def _neighbours(
            self, *, cur_id, from_clause, where_clause, where_params,
            id_col, key_col,
    ) -> tuple[Optional[int], Optional[int], int, int]:
        """统一的邻居聚合入口。

        - 当前项不在集合内 → (None, None, 0, 0)。
        - 集合为空 → 当前项必然不在集合内，同样返回 (None, None, 0, 0)。
        """
        found, cur_key = self._fetch_key(
            from_clause=from_clause, where_clause=where_clause,
            where_params=where_params, id_col=id_col, key_col=key_col,
            cur_id=cur_id,
        )
        if not found:
            return None, None, 0, 0

        prev_id = self._find_prev(
            from_clause=from_clause, where_clause=where_clause,
            where_params=where_params, id_col=id_col, key_col=key_col,
            cur_id=cur_id, cur_key=cur_key,
        )
        next_id = self._find_next(
            from_clause=from_clause, where_clause=where_clause,
            where_params=where_params, id_col=id_col, key_col=key_col,
            cur_id=cur_id, cur_key=cur_key,
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
        return prev_id, next_id, index, total

    def _locate(
            self, *, cur_id, from_clause, where_clause, where_params,
            id_col, key_col,
    ) -> Optional[int]:
        """统一的位置定位入口。当前项不在集合内 → None。"""
        found, cur_key = self._fetch_key(
            from_clause=from_clause, where_clause=where_clause,
            where_params=where_params, id_col=id_col, key_col=key_col,
            cur_id=cur_id,
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
                   id_col, key_col, cur_id):
        """按主键取当前项的排序键；当前项不在集合内返回哨兵。"""
        row = self._fetchone(
            f"SELECT {key_col} AS k FROM {from_clause} "
            f"WHERE {where_clause} AND {id_col} = ?",
            (*where_params, cur_id),
        )
        if row is None:
            return False, None
        return True, row["k"]

    def _find_prev(self, *, from_clause, where_clause, where_params,
                   id_col, key_col, cur_id, cur_key):
        """严格排在 cur 前一项的 id；没有返回 None。

        集合排序：`(key IS NULL) ASC, key DESC, id DESC`。
        """
        if cur_key is not None:
            # 非 NULL 段：key 更大（或 key 相等且 id 更大）的最近一项
            row = self._fetchone(
                f"""SELECT {id_col} AS nid FROM {from_clause}
                     WHERE {where_clause}
                       AND {key_col} IS NOT NULL
                       AND ({key_col} > ? OR ({key_col} = ? AND {id_col} > ?))
                     ORDER BY {key_col} ASC, {id_col} ASC
                     LIMIT 1""",
                (*where_params, cur_key, cur_key, cur_id),
            )
            return row["nid"] if row else None

        # cur 落在 NULL 段：先找 NULL 段中 id 更大的
        row = self._fetchone(
            f"""SELECT {id_col} AS nid FROM {from_clause}
                 WHERE {where_clause}
                   AND {key_col} IS NULL
                   AND {id_col} > ?
                 ORDER BY {id_col} ASC LIMIT 1""",
            (*where_params, cur_id),
        )
        if row:
            return row["nid"]
        # 否则落到非 NULL 段的最后一项
        row = self._fetchone(
            f"""SELECT {id_col} AS nid FROM {from_clause}
                 WHERE {where_clause}
                   AND {key_col} IS NOT NULL
                 ORDER BY {key_col} ASC, {id_col} ASC LIMIT 1""",
            where_params,
        )
        return row["nid"] if row else None

    def _find_next(self, *, from_clause, where_clause, where_params,
                   id_col, key_col, cur_id, cur_key):
        """严格排在 cur 后一项的 id；没有返回 None。"""
        if cur_key is not None:
            # 非 NULL 段：key 更小（或 key 相等且 id 更小）的最近一项
            row = self._fetchone(
                f"""SELECT {id_col} AS nid FROM {from_clause}
                     WHERE {where_clause}
                       AND {key_col} IS NOT NULL
                       AND ({key_col} < ? OR ({key_col} = ? AND {id_col} < ?))
                     ORDER BY {key_col} DESC, {id_col} DESC
                     LIMIT 1""",
                (*where_params, cur_key, cur_key, cur_id),
            )
            if row:
                return row["nid"]
            # 非 NULL 段之后紧邻 NULL 段的第一项
            row = self._fetchone(
                f"""SELECT {id_col} AS nid FROM {from_clause}
                     WHERE {where_clause}
                       AND {key_col} IS NULL
                     ORDER BY {id_col} DESC LIMIT 1""",
                where_params,
            )
            return row["nid"] if row else None

        # cur 落在 NULL 段：找 NULL 段中 id 更小的
        row = self._fetchone(
            f"""SELECT {id_col} AS nid FROM {from_clause}
                 WHERE {where_clause}
                   AND {key_col} IS NULL
                   AND {id_col} < ?
                 ORDER BY {id_col} DESC LIMIT 1""",
            (*where_params, cur_id),
        )
        return row["nid"] if row else None

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

        # cur 在 NULL 段：所有非 NULL 项 + NULL 段中 id 更大的
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

    def get_asset_full_path(
            self, asset_uuid: str, include_deleted: bool = False
    ) -> Optional[str]:
        """根据资产 uuid 获取其磁盘上的完整路径。

        路径拼接规则：
          - 若资产关联的导入源存在（source_path 非空），使用
            ``Path(source_path) / file_path`` 拼接（file_path 为相对路径）。
          - 若资产未关联导入源（source_id 为空或 source_path 为 NULL），
            则 ``file_path`` 被视为绝对路径直接返回。
          - 若资产本身不存在（或被软删且未开启 include_deleted），返回 None。

        Args:
            asset_uuid: 资产 UUID（字符串形式）。
            include_deleted: 为 True 时连同软删资产一起返回，
                供 Trash（回收站）中展示原图使用。

        Returns:
            资产完整路径字符串；无法定位时返回 None。
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

    def get_asset_full_path_by_id(
            self, asset_id: int, include_deleted: bool = False
    ) -> Optional[str]:
        """按 asset 整数主键取磁盘完整路径。拼路径规则与 get_asset_full_path 一致。"""
        where = "a.id = ?"
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
            (int(asset_id),),
        )
        return self._resolve_full_path(row)

    @staticmethod
    def _resolve_full_path(row: Any) -> Optional[str]:
        """根据查询行拼接磁盘完整路径。两条 get_asset_full_path* 共用同一套规则。"""
        if row is None or not row["file_path"]:
            return None

        file_path = Path(row["file_path"])
        source_path = row["source_path"]

        # file_path 已为绝对路径，或没有可用的源路径起点时，直接返回
        if file_path.is_absolute() or not source_path:
            return str(file_path)

        return str(Path(source_path) / file_path)
