#
"""资产写操作仓储。

写操作专属：所有会改动 ``assets`` / ``album_assets`` 状态的 SQL 都集中在此。
**不要和 ViewRepository 混用** —— 读查询一律走 ``ViewRepository``。

接口契约（P1 收敛）：
    · 所有公开方法以 ``asset uuid`` 为键，**不暴露 assets.id**。
    · id 仅在 SQL 内部作为外键（album_assets.asset_uuid）使用。

磁盘文件契约：
    * ``purge`` / ``empty_trash`` 只删数据库行，**不碰任何磁盘文件**；
    * 缩略图文件应由调用方先行通过 ``ThumbnailService.purge_bulk``
      （或 ``ThumbnailRepository.purge_bulk``）拿到路径快照，再由调用方
      自行 unlink；
    * 原图文件同理，由调用方决定去留。
"""

from typing import Sequence, Union
from uuid import UUID

from .base import BaseRepository

# 与 ThumbnailRepository 保持同一量级，兼顾旧版 SQLite 的变量上限
_IN_CLAUSE_CHUNK = 900

_UUIDLike = Union[UUID, str]


def _s(value: _UUIDLike) -> str:
    return str(value)


# noinspection SpellCheckingInspection
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

              uuids = view_repo.list_asset_uuids_by_scope("deleted")
              snapshots = thumbnail_repo.purge_bulk(uuids)  # 清列 + 返回快照
              # 调用方遍历 snapshots 删磁盘文件
              asset_repo.purge_many(uuids)                  # 删行
    """

    # ==================================================================
    # 收藏
    # ==================================================================

    def set_favorite(self, asset_uuid: _UUIDLike, favorite: bool) -> int:
        """设置 / 取消收藏。返回受影响行数（0 表示资产不存在）。"""
        cursor = self._execute(
            "UPDATE assets SET is_favorite = ? WHERE uuid = ?",
            (1 if favorite else 0, _s(asset_uuid)),
        )
        return cursor.rowcount

    # ==================================================================
    # 软删 / 恢复
    # ==================================================================

    def soft_delete(self, asset_uuid: _UUIDLike) -> int:
        """软删除单个资产（幂等：已软删返回 0 行）。"""
        cursor = self._execute(
            """
            UPDATE assets
               SET is_deleted = 1,
                   deleted_at = strftime('%Y-%m-%dT%H:%M:%f','now')
             WHERE uuid = ?
               AND is_deleted = 0
            """,
            (_s(asset_uuid),),
        )
        return cursor.rowcount

    def soft_delete_many(self, asset_uuids: Sequence[_UUIDLike]) -> int:
        """批量软删，单事务提交。"""
        uuids = [_s(u) for u in asset_uuids]
        if not uuids:
            return 0
        affected = 0
        with self._transaction() as conn:
            for start in range(0, len(uuids), _IN_CLAUSE_CHUNK):
                chunk = uuids[start:start + _IN_CLAUSE_CHUNK]
                placeholders = ",".join("?" * len(chunk))
                cursor = conn.execute(
                    f"""
                    UPDATE assets
                       SET is_deleted = 1,
                           deleted_at = strftime('%Y-%m-%dT%H:%M:%f','now')
                     WHERE uuid IN ({placeholders})
                       AND is_deleted = 0
                    """,
                    chunk,
                )
                affected += cursor.rowcount
        return affected

    def restore(self, asset_uuid: _UUIDLike) -> int:
        """恢复单个软删资产（幂等：未删除返回 0 行）。"""
        cursor = self._execute(
            """
            UPDATE assets
               SET is_deleted = 0,
                   deleted_at = NULL
             WHERE uuid = ?
               AND is_deleted = 1
            """,
            (_s(asset_uuid),),
        )
        return cursor.rowcount

    def restore_many(self, asset_uuids: Sequence[_UUIDLike]) -> int:
        """批量恢复，单事务提交。"""
        uuids = [_s(u) for u in asset_uuids]
        if not uuids:
            return 0
        affected = 0
        with self._transaction() as conn:
            for start in range(0, len(uuids), _IN_CLAUSE_CHUNK):
                chunk = uuids[start:start + _IN_CLAUSE_CHUNK]
                placeholders = ",".join("?" * len(chunk))
                cursor = conn.execute(
                    f"""
                    UPDATE assets
                       SET is_deleted = 0,
                           deleted_at = NULL
                     WHERE uuid IN ({placeholders})
                       AND is_deleted = 1
                    """,
                    chunk,
                )
                affected += cursor.rowcount
        return affected

    # ==================================================================
    # 硬删 / 清空回收站
    # ==================================================================

    def purge(self, asset_uuid: _UUIDLike) -> int:
        """永久删除单个资产行。

        - ``album_assets`` 走 ON DELETE CASCADE；
        - ``albums.cover_asset_id`` 走 ON DELETE SET NULL；
        - **不触碰磁盘文件**（缩略图 / 原图由调用方编排）。
        """
        cursor = self._execute(
            "DELETE FROM assets WHERE uuid = ?", (_s(asset_uuid),)
        )
        return cursor.rowcount

    def purge_many(self, asset_uuids: Sequence[_UUIDLike]) -> int:
        """批量硬删，单事务提交。磁盘契约同 ``purge``。"""
        uuids = [_s(u) for u in asset_uuids]
        if not uuids:
            return 0
        affected = 0
        with self._transaction() as conn:
            for start in range(0, len(uuids), _IN_CLAUSE_CHUNK):
                chunk = uuids[start:start + _IN_CLAUSE_CHUNK]
                placeholders = ",".join("?" * len(chunk))
                cursor = conn.execute(
                    f"DELETE FROM assets WHERE uuid IN ({placeholders})",
                    chunk,
                )
                affected += cursor.rowcount
        return affected

    def empty_trash(self) -> int:
        """清空回收站：硬删所有 ``is_deleted = 1`` 的资产。

        **不触碰磁盘文件**。推荐调用方先 ``ViewRepository.list_asset_uuids_by_scope("deleted")``
        取一遍 uuid 交给 ``ThumbnailService.purge_bulk`` 收集缩略图快照、
        自行删盘，再调本方法删行。
        """
        cursor = self._execute("DELETE FROM assets WHERE is_deleted = 1")
        return cursor.rowcount

    # ==================================================================
    # 相簿成员
    # ==================================================================

    def add_to_album(self, album_uuid: str, asset_uuid: _UUIDLike) -> int:
        """把资产加入指定相簿。

        - 相簿或资产不可见（is_deleted = 1）时不会写入；
        - 已存在的关系通过 INSERT OR IGNORE 跳过（天然幂等）；
        - ``asset_taken_at`` 冗余列从 ``assets.taken_at`` 同步。
        """
        cursor = self._execute(
            """
            INSERT OR IGNORE INTO album_assets (album_id, asset_uuid, asset_taken_at)
            SELECT al.id, a.id, a.taken_at
              FROM albums AS al
              JOIN assets AS a ON a.uuid = ?
             WHERE al.uuid = ?
               AND al.is_deleted = 0
               AND a.is_deleted = 0
            """,
            (_s(asset_uuid), album_uuid),
        )
        return cursor.rowcount

    def remove_from_album(self, album_uuid: str, asset_uuid: _UUIDLike) -> int:
        """把资产从指定相簿移除（相簿不可见时不做任何事）。"""
        cursor = self._execute(
            """
            DELETE FROM album_assets
             WHERE album_id = (
                       SELECT id FROM albums
                        WHERE uuid = ?
                          AND is_deleted = 0
                   )
               AND asset_uuid = (
                       SELECT id FROM assets WHERE uuid = ?
                   )
            """,
            (album_uuid, _s(asset_uuid)),
        )
        return cursor.rowcount
