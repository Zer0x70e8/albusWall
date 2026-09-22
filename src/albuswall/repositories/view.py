#
""""""

from pathlib import Path
from typing import List, Optional
from uuid import UUID

from albuswall.dto.album import Album, AssetDTO

from .base import BaseRepository


class ViewRepository(BaseRepository):
    """相簿（专辑）视图仓储。"""

    _SCOPE_WHERE = {"active": "is_deleted = 0", "deleted": "is_deleted = 1"}
    _SCOPE_ORDER = {
        "active": "taken_at IS NULL, taken_at DESC, id DESC",
        "deleted": "deleted_at IS NULL, deleted_at DESC, id DESC",
    }

    def get_cover_asset_by_scope(self, scope: str) -> Optional[AssetDTO]:
        where = self._SCOPE_WHERE[scope]
        order = self._SCOPE_ORDER[scope]
        row = self._fetchone(
            f"SELECT * FROM assets WHERE {where} ORDER BY {order} LIMIT 1"
        )
        return AssetDTO.from_row(row) if row else None

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
        self.logger.trace("after _fetchall, type=%s, len=%s, repr=%r",
                          type(rows).__name__, len(rows) if rows is not None else None, rows)
        if not rows:
            return []
        return [row["uuid"] for row in rows]

    def get_album_by_uuid(self, album_uuid: str) -> Optional[Album]:
        """根据 uuid 获取未被软删除的相簿 DTO。

        Args:
            album_uuid: 相簿 UUID（字符串形式）。

        Returns:
            对应的 Album DTO；若不存在或已被软删除则返回 None。
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

        Args:
            album_uuid: 相簿 UUID（字符串形式）。

        Returns:
            对应的 AssetDTO；若相簿不存在、已被软删除、未设置封面
            或封面资产已被软删除，则返回 None。
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

    def get_asset_full_path(self, asset_uuid: str) -> Optional[Path]:
        """根据资产 uuid 获取其磁盘上的完整路径。

        路径拼接规则：
          - 若资产关联的导入源存在（source_path 非空），使用
            ``Path(source_path) / file_path`` 拼接（file_path 为相对路径）。
          - 若资产未关联导入源（source_id 为空或 source_path 为 NULL），
            则 ``file_path`` 被视为绝对路径直接返回。
          - 若资产本身不存在或已被软删除，返回 None。

        Args:
            asset_uuid: 资产 UUID（字符串形式）。

        Returns:
            资产完整路径（Path 对象）；若资产不存在或已被软删除则返回 None。
        """
        row = self._fetchone(
            """
            SELECT a.file_path     AS file_path,
                   s.source_path   AS source_path
              FROM assets AS a
              LEFT JOIN ingest_source AS s
                     ON s.id = a.source_id
             WHERE a.uuid = ?
               AND a.is_deleted = 0
            """,
            (asset_uuid,),
        )
        if row is None or not row["file_path"]:
            return None

        file_path = Path(row["file_path"])
        source_path = row["source_path"]

        # # file_path 已为绝对路径，或没有可用的源路径起点时，直接返回
        # if file_path.is_absolute() or not source_path:
        #     return str(file_path

        return Path(source_path) / file_path
