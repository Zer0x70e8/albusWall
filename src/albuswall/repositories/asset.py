#
"""资产写操作仓储。

写操作专属：所有会改动 ``assets`` / ``album_assets`` 状态的 SQL 都集中在此。
**不要和 ViewRepository 混用** —— 读查询一律走 ``ViewRepository``，
本类不提供任何查询接口，以免两边职责重叠、排序契约分叉。

磁盘文件契约：
    * ``purge`` / ``empty_trash`` 只删数据库行，**不碰任何磁盘文件**；
    * 缩略图文件应由调用方先行通过 ``ThumbnailService.purge_bulk``
      （或 ``ThumbnailRepository.purge_bulk``）拿到路径快照，再由调用方
      自行 unlink；
    * 原图文件同理，由调用方决定去留。
"""

from typing import Sequence

from .base import BaseRepository

# 与 ThumbnailRepository 保持同一量级，兼顾旧版 SQLite 的变量上限
_IN_CLAUSE_CHUNK = 900


class AssetRepository(BaseRepository):
    """资产（assets）写操作仓储。

    **写操作专属，不要和 ViewRepository 混用。**

    职责边界：
        · 收藏切换（``set_favorite``）；
        · 软删 / 恢复（``soft_delete*`` / ``restore*``）；
        · 硬删与清空回收站（``purge`` / ``empty_trash``）；
        · 相簿成员增删（``add_to_album`` / ``remove_from_album``）。
        读侧（列表、封面、邻居定位等）请使用 ``ViewRepository``。

    软删语义：
        · 软删写 ``is_deleted = 1`` + ``deleted_at = 当前 UTC``；
        · 恢复写 ``is_deleted = 0`` + ``deleted_at = NULL``；
        · 两者均幂等：对已经在目标状态的资产再次执行返回 0 行。

    磁盘文件契约：
        · ``purge`` / ``empty_trash`` 只删数据库行，**不碰磁盘**；
        · 缩略图清理路径示例（调用方编排）::

              ids = view_repo.list_asset_ids_by_scope("deleted")
              snapshots = thumbnail_repo.purge_bulk(ids)   # 清列 + 返回快照
              # 调用方遍历 snapshots 删磁盘文件
              asset_repo.purge_many(ids)                   # 删行
    """

    # ==================================================================
    # 收藏
    # ==================================================================

    def set_favorite(self, asset_id: int, favorite: bool) -> int:
        """设置 / 取消收藏。

        Args:
            asset_id: assets.id。
            favorite: True 置 1，False 置 0。

        Returns:
            受影响行数（0 表示资产不存在）。
        """
        cursor = self._execute(
            "UPDATE assets SET is_favorite = ? WHERE id = ?",
            (1 if favorite else 0, int(asset_id)),
        )
        return cursor.rowcount

    # ==================================================================
    # 软删 / 恢复
    # ==================================================================

    def soft_delete(self, asset_id: int) -> int:
        """软删除单个资产：``is_deleted = 1`` + ``deleted_at = now``。

        幂等：对已经软删的资产再次调用返回 0 行，不会刷新 ``deleted_at``。

        Returns:
            受影响行数（0 表示资产不存在或已处于删除态）。
        """
        cursor = self._execute(
            """
            UPDATE assets
               SET is_deleted = 1,
                   deleted_at = strftime('%Y-%m-%dT%H:%M:%f','now')
             WHERE id = ?
               AND is_deleted = 0
            """,
            (int(asset_id),),
        )
        return cursor.rowcount

    def soft_delete_many(self, asset_ids: Sequence[int]) -> int:
        """批量软删，单事务提交。已在删除态的资产会被 WHERE 过滤掉。"""
        ids = [int(i) for i in asset_ids]
        if not ids:
            return 0

        affected = 0
        with self._transaction() as conn:
            for start in range(0, len(ids), _IN_CLAUSE_CHUNK):
                chunk = ids[start:start + _IN_CLAUSE_CHUNK]
                placeholders = ",".join("?" * len(chunk))
                cursor = conn.execute(
                    f"""
                    UPDATE assets
                       SET is_deleted = 1,
                           deleted_at = strftime('%Y-%m-%dT%H:%M:%f','now')
                     WHERE id IN ({placeholders})
                       AND is_deleted = 0
                    """,
                    chunk,
                )
                affected += cursor.rowcount
        return affected

    def restore(self, asset_id: int) -> int:
        """恢复单个软删资产：``is_deleted = 0`` + ``deleted_at = NULL``。

        幂等：对未删除的资产再次调用返回 0 行。

        Returns:
            受影响行数（0 表示资产不存在或未处于删除态）。
        """
        cursor = self._execute(
            """
            UPDATE assets
               SET is_deleted = 0,
                   deleted_at = NULL
             WHERE id = ?
               AND is_deleted = 1
            """,
            (int(asset_id),),
        )
        return cursor.rowcount

    def restore_many(self, asset_ids: Sequence[int]) -> int:
        """批量恢复，单事务提交。未处于删除态的资产会被 WHERE 过滤掉。"""
        ids = [int(i) for i in asset_ids]
        if not ids:
            return 0

        affected = 0
        with self._transaction() as conn:
            for start in range(0, len(ids), _IN_CLAUSE_CHUNK):
                chunk = ids[start:start + _IN_CLAUSE_CHUNK]
                placeholders = ",".join("?" * len(chunk))
                cursor = conn.execute(
                    f"""
                    UPDATE assets
                       SET is_deleted = 0,
                           deleted_at = NULL
                     WHERE id IN ({placeholders})
                       AND is_deleted = 1
                    """,
                    chunk,
                )
                affected += cursor.rowcount
        return affected

    # ==================================================================
    # 硬删 / 清空回收站
    # ==================================================================

    def purge(self, asset_id: int) -> int:
        """永久删除单个资产行。

        - ``album_assets`` 依赖 ``ON DELETE CASCADE`` 自动清理；
        - ``albums.cover_asset_id`` 依赖 ``ON DELETE SET NULL`` 自动置空；
        - **本方法不触碰磁盘文件**。缩略图 / 原图请由调用方在删除前通过
          ``ThumbnailService.purge_bulk`` 拿到快照后自行清理。

        Returns:
            受影响行数（0 表示资产不存在）。
        """
        cursor = self._execute("DELETE FROM assets WHERE id = ?", (int(asset_id),))
        return cursor.rowcount

    def purge_many(self, asset_ids: Sequence[int]) -> int:
        """批量硬删，单事务提交。缩略图文件清理契约同 ``purge``。"""
        ids = [int(i) for i in asset_ids]
        if not ids:
            return 0

        affected = 0
        with self._transaction() as conn:
            for start in range(0, len(ids), _IN_CLAUSE_CHUNK):
                chunk = ids[start:start + _IN_CLAUSE_CHUNK]
                placeholders = ",".join("?" * len(chunk))
                cursor = conn.execute(
                    f"DELETE FROM assets WHERE id IN ({placeholders})",
                    chunk,
                )
                affected += cursor.rowcount
        return affected

    def empty_trash(self) -> int:
        """清空回收站：硬删所有 ``is_deleted = 1`` 的资产。

        **本方法不触碰磁盘文件**。推荐调用方在调用前先取一遍
        ``ViewRepository.list_asset_ids_by_scope("deleted")``，把得到的 id
        交给 ``ThumbnailService.purge_bulk`` 收集缩略图快照、自行删盘，再调
        本方法删行。

        Returns:
            被删除的资产行数。
        """
        cursor = self._execute("DELETE FROM assets WHERE is_deleted = 1")
        return cursor.rowcount

    # ==================================================================
    # 相簿成员
    # ==================================================================

    def add_to_album(self, album_uuid: str, asset_id: int) -> int:
        """把资产加入指定相簿。

        - 相簿或资产不可见（``is_deleted = 1``）时不会写入；
        - 已存在的关系通过 ``INSERT OR IGNORE`` 跳过，天然幂等；
        - ``asset_taken_at`` 冗余列从 ``assets.taken_at`` 同步写入。

        Args:
            album_uuid: albums.uuid。
            asset_id: assets.id。

        Returns:
            实际新增的关联行数（0 表示已存在 / 任一侧不可见）。
        """
        cursor = self._execute(
            """
            INSERT OR IGNORE INTO album_assets (album_id, asset_id, asset_taken_at)
            SELECT al.id, a.id, a.taken_at
              FROM albums AS al
              JOIN assets AS a ON a.id = ?
             WHERE al.uuid = ?
               AND al.is_deleted = 0
               AND a.is_deleted = 0
            """,
            (int(asset_id), album_uuid),
        )
        return cursor.rowcount

    def remove_from_album(self, album_uuid: str, asset_id: int) -> int:
        """把资产从指定相簿移除。

        相簿不存在或已被软删时不做任何事。

        Returns:
            受影响行数（0 表示关联不存在或相簿不可见）。
        """
        cursor = self._execute(
            """
            DELETE FROM album_assets
             WHERE album_id = (
                       SELECT id FROM albums
                        WHERE uuid = ?
                          AND is_deleted = 0
                   )
               AND asset_id = ?
            """,
            (album_uuid, int(asset_id)),
        )
        return cursor.rowcount
