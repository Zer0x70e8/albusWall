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

    @classmethod
    def _order_by(cls, scope: str, prefix: str = "") -> str:
        """返回给定 scope 下的 ORDER BY 片段。

        Args:
            scope: ``"active"`` 或 ``"deleted"``。
            prefix: 列名表别名前缀（如 ``"a."``），默认无别名。
        """
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
    # 资产磁盘路径
    # ==================================================================

    def get_asset_full_path(
            self, asset_uuid: str, include_deleted: bool = False
    ) -> Optional[Path]:
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
            资产完整路径（Path 对象）；无法定位时返回 None。
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
    ) -> Optional[Path]:
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
    def _resolve_full_path(row: Any) -> Optional[Path]:
        """根据查询行拼接磁盘完整路径。两条 get_asset_full_path* 共用同一套规则。"""
        if row is None or not row["file_path"]:
            return None

        file_path = Path(row["file_path"])
        source_path = row["source_path"]

        # file_path 已为绝对路径，或没有可用的源路径起点时，直接返回
        if file_path.is_absolute() or not source_path:
            return file_path

        return Path(source_path) / file_path
