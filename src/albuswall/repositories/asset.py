#
"""资产写操作仓储。

写操作专属：所有会改动 ``assets`` / ``album_assets`` / ``asset_tags``
状态的 SQL 都集中在此。
**不要和 ViewRepository 混用** —— 读查询一律走 ``ViewRepository``。

接口契约（P1 收敛）：
    · 所有公开方法以 ``asset uuid`` 为键，**不暴露 assets.id**。
    · uuid 入参收 ``UUID | str``（DTO 侧是 ``UUID``），绑定前统一过
      ``as_uuid_str``；``assets.id`` 仅在 SQL 内部作为外键
      （``album_assets.asset_id`` / ``asset_tags.asset_id``）使用。
    · 批量语句统一走 ``chunked`` + ``placeholders``，单事务提交。

磁盘文件契约：
    * ``purge`` / ``empty_trash`` 只删数据库行，**不碰任何磁盘文件**；
    * 缩略图文件应由调用方先行通过 ``ThumbnailService.purge_bulk``
      （或 ``ThumbnailRepository.clear_and_snapshot_bulk_by_uuid``）拿到
      路径快照，再由调用方自行 unlink；
    * 原图文件同理，由调用方决定去留。
"""

from __future__ import annotations

from typing import Sequence
from uuid import UUID

from albuswall.utils.time import now_str

from .base import BaseRepository
from .utils.sql_helpers import as_uuid_str, chunked, placeholders
from .utils.visibility import active_asset_where

_UUIDLike = UUID | str

# 「可见资产」条件（含 source 可见性）；成员增删的写入前置共用。
_ACTIVE_ASSET_WHERE_A = active_asset_where("a")


def _dedupe_uuids(values: Sequence[_UUIDLike]) -> list[str]:
    """去重（保持顺序）并转成 SQL 可绑定的 ``str``。"""
    return [as_uuid_str(v) for v in dict.fromkeys(values)]


# noinspection SpellCheckingInspection
class AssetRepository(BaseRepository):
    """资产（assets）写操作仓储。

    **写操作专属，不要和 ViewRepository 混用。**

    职责边界：
        · 收藏切换（``set_favorite``）；
        · 软删 / 恢复（``soft_delete*`` / ``restore*``）；
        · 硬删与清空回收站（``purge*`` / ``empty_trash``）；
        · 相簿成员增删（``add_to_album`` / ``remove_from_album``）；
        · 标签成员增删（``add_tag*`` / ``remove_tag*``）。
        读侧（列表、封面、邻居定位等）请使用 ``ViewRepository``。

    软删语义：
        · 软删写 ``is_deleted = 1`` + ``deleted_at = 当前 UTC``；
        · 恢复写 ``is_deleted = 0`` + ``deleted_at = NULL``；
        · 两者均幂等：对已经在目标状态的资产再次执行返回 0 行。

    磁盘文件契约：
        · ``purge`` / ``empty_trash`` 只删数据库行，**不碰磁盘**；
        · 缩略图清理路径示例（调用方编排）::

              uuids = view_repo.list_asset_uuids_by_scope("deleted")
              snapshots = thumbnail_repo.clear_and_snapshot_bulk_by_uuid(uuids)
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
            (1 if favorite else 0, as_uuid_str(asset_uuid)),
        )
        return cursor.rowcount

    # ==================================================================
    # 软删 / 恢复
    # ==================================================================

    def soft_delete(self, asset_uuid: _UUIDLike) -> int:
        """软删除单个资产（幂等：已软删返回 0 行）。

        ``deleted_at`` 由 Python 侧按 ``utils.time`` 的统一格式生成后绑参，
        不在 SQL 内写 ``strftime``：时间的唯一格式来源留在 Python。
        """
        cursor = self._execute(
            """
            UPDATE assets
               SET is_deleted = 1,
                   deleted_at = ?
             WHERE uuid = ?
               AND is_deleted = 0
            """,
            (now_str(), as_uuid_str(asset_uuid)),
        )
        return cursor.rowcount

    def soft_delete_many(self, asset_uuids: Sequence[_UUIDLike]) -> int:
        """批量软删，单事务提交。返回实际变更行数。"""
        return self._update_many(
            """
            UPDATE assets
               SET is_deleted = 1,
                   deleted_at = ?
             WHERE uuid IN ({ph})
               AND is_deleted = 0
            """,
            _dedupe_uuids(asset_uuids),
            prefix=(now_str(),),
        )

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
            (as_uuid_str(asset_uuid),),
        )
        return cursor.rowcount

    def restore_many(self, asset_uuids: Sequence[_UUIDLike]) -> int:
        """批量恢复，单事务提交。返回实际变更行数。"""
        return self._update_many(
            """
            UPDATE assets
               SET is_deleted = 0,
                   deleted_at = NULL
             WHERE uuid IN ({ph})
               AND is_deleted = 1
            """,
            _dedupe_uuids(asset_uuids),
        )

    # TODO 预留，可能没用
    # # ==================================================================
    # # 硬删 / 清空回收站
    # # ==================================================================
    #
    # def purge(self, asset_uuid: _UUIDLike) -> int:
    #     """永久删除单个资产行。
    #
    #     - ``album_assets`` / ``asset_tags`` 走 ON DELETE CASCADE；
    #     - ``albums.cover_asset_id`` 走 ON DELETE SET NULL；
    #     - **不触碰磁盘文件**（缩略图 / 原图由调用方编排）。
    #     """
    #     cursor = self._execute(
    #         "DELETE FROM assets WHERE uuid = ?", (as_uuid_str(asset_uuid),)
    #     )
    #     return cursor.rowcount
    #
    # def purge_many(self, asset_uuids: Sequence[_UUIDLike]) -> int:
    #     """批量硬删，单事务提交。磁盘契约同 ``purge``。"""
    #     return self._update_many(
    #         "DELETE FROM assets WHERE uuid IN ({ph})",
    #         _dedupe_uuids(asset_uuids),
    #     )
    #
    # def empty_trash(self) -> int:
    #     """清空回收站：硬删所有 ``is_deleted = 1`` 的资产。
    #
    #     **不触碰磁盘文件**。推荐调用方先
    #     ``ViewRepository.list_asset_uuids_by_scope("deleted")`` 取一遍 uuid
    #     交给缩略图服务收集快照、自行删盘，再调本方法删行。
    #     """
    #     cursor = self._execute("DELETE FROM assets WHERE is_deleted = 1")
    #     return cursor.rowcount

    # ==================================================================
    # 相簿成员
    # ==================================================================

    def add_to_album(
            self, album_uuid: _UUIDLike, asset_uuid: _UUIDLike
    ) -> int:
        """把资产加入指定相簿。

        - 相簿或资产不可见（is_deleted = 1）时不会写入；
        - 已存在的关系通过 INSERT OR IGNORE 跳过（天然幂等）；
        - ``asset_taken_at`` 冗余列从 ``assets.taken_at`` 同步。
        """
        cursor = self._execute(
            f"""
            INSERT OR IGNORE INTO album_assets (album_id, asset_id, asset_taken_at)
            SELECT al.id, a.id, a.taken_at
              FROM albums AS al
              JOIN assets AS a ON a.uuid = ?
             WHERE al.uuid = ?
               AND al.is_deleted = 0
               AND {_ACTIVE_ASSET_WHERE_A}
            """,
            (as_uuid_str(asset_uuid), as_uuid_str(album_uuid)),
        )
        return cursor.rowcount

    def remove_from_album(
            self, album_uuid: _UUIDLike, asset_uuid: _UUIDLike
    ) -> int:
        """把资产从指定相簿移除（相簿不可见时不做任何事）。"""
        cursor = self._execute(
            """
            DELETE FROM album_assets
             WHERE album_id = (
                       SELECT id FROM albums
                        WHERE uuid = ?
                          AND is_deleted = 0
                   )
               AND asset_id = (
                       SELECT id FROM assets WHERE uuid = ?
                   )
            """,
            (as_uuid_str(album_uuid), as_uuid_str(asset_uuid)),
        )
        return cursor.rowcount

    # ==================================================================
    # 标签成员
    # ==================================================================

    def add_tag_to_asset(
            self, tag_uuid: _UUIDLike, asset_uuid: _UUIDLike
    ) -> int:
        """给资产打标签。

        - 标签或资产不可见（is_deleted = 1）时不会写入；
        - 已存在的绑定通过 INSERT OR IGNORE 跳过（天然幂等）；
        - ``tag_uuid`` 必须指向 ``tags.uuid``（规范标签），**不得写别名**。
        """
        cursor = self._execute(
            f"""
            INSERT OR IGNORE INTO asset_tags (asset_id, tag_uuid)
            SELECT a.id, t.uuid
              FROM assets AS a
              JOIN tags   AS t ON t.uuid = ?
             WHERE a.uuid = ?
               AND {_ACTIVE_ASSET_WHERE_A}
               AND t.is_deleted = 0
            """,
            (as_uuid_str(tag_uuid), as_uuid_str(asset_uuid)),
        )
        return cursor.rowcount

    def remove_from_tag(
            self, tag_uuid: _UUIDLike, asset_uuid: _UUIDLike
    ) -> int:
        """把资产从指定标签移除（标签不可见时不做任何事）。"""
        cursor = self._execute(
            """
            DELETE FROM asset_tags
             WHERE asset_id = (
                       SELECT id FROM assets WHERE uuid = ?
                   )
               AND tag_uuid = (
                       SELECT uuid FROM tags
                        WHERE uuid = ?
                          AND is_deleted = 0
                   )
            """,
            (as_uuid_str(asset_uuid), as_uuid_str(tag_uuid)),
        )
        return cursor.rowcount

    # ==================================================================
    # 标签成员（批量）
    # ==================================================================

    def add_tags_to_asset(
            self, asset_uuid: _UUIDLike, tag_uuids: Sequence[_UUIDLike]
    ) -> int:
        """批量给资产打标签。

        - 资产或标签不可见（``is_deleted = 1``）时不会写入对应绑定；
        - 已存在的绑定通过 ``INSERT OR IGNORE`` 跳过（天然幂等）；
        - 入参先按出现顺序去重，避免同批重复 uuid 造成无谓扫描；
        - 单事务提交，返回**实际新增**的绑定行数。
        """
        uuids = _dedupe_uuids(tag_uuids)
        if not uuids:
            return 0

        asset_value = as_uuid_str(asset_uuid)
        affected = 0
        with self._transaction() as conn:
            for chunk in chunked(uuids):
                cursor = conn.execute(
                    f"""
                    INSERT OR IGNORE INTO asset_tags (asset_id, tag_uuid)
                    SELECT a.id, t.uuid
                      FROM assets AS a
                      JOIN tags   AS t
                        ON t.uuid IN ({placeholders(len(chunk))})
                       AND t.is_deleted = 0
                     WHERE a.uuid = ?
                       AND {_ACTIVE_ASSET_WHERE_A}
                    """,
                    (*chunk, asset_value),
                )
                affected += cursor.rowcount
        return affected

    def remove_tags_from_asset(
            self, asset_uuid: _UUIDLike, tag_uuids: Sequence[_UUIDLike]
    ) -> int:
        """批量移除资产上的标签绑定。

        - 与 ``remove_from_tag`` 保持一致：仅移除 ``tags.is_deleted = 0``
          的标签绑定；软删标签的绑定保留，标签恢复后即可重现；
        - 资产不存在时自然 0 行；
        - 入参去重后单事务提交，返回**实际删除**的绑定行数。
        """
        uuids = _dedupe_uuids(tag_uuids)
        if not uuids:
            return 0

        asset_value = as_uuid_str(asset_uuid)
        affected = 0
        with self._transaction() as conn:
            for chunk in chunked(uuids):
                cursor = conn.execute(
                    f"""
                    DELETE FROM asset_tags
                     WHERE asset_id = (
                               SELECT id FROM assets WHERE uuid = ?
                           )
                       AND tag_uuid IN (
                               SELECT uuid FROM tags
                                WHERE uuid IN ({placeholders(len(chunk))})
                                  AND is_deleted = 0
                           )
                    """,
                    (asset_value, *chunk),
                )
                affected += cursor.rowcount
        return affected

    # ==================================================================
    # 内部工具
    # ==================================================================

    def _update_many(
            self,
            sql: str,
            uuids: Sequence[str],
            *,
            prefix: Sequence = (),
    ) -> int:
        """按 ``IN (...)`` 分批执行同一条语句，单事务提交。

        ``sql`` 中使用 ``{ph}`` 表示当前批次的占位符列表；用
        ``str.replace`` 而不是 ``str.format`` 展开，避免 SQL 里未来出现
        ``{`` / ``}``（例如 JSON 字面量）时被误当成格式占位。

        ``prefix`` 是每批固定绑定的前置参数（如 ``deleted_at``），
        ``uuids`` 已由调用方去重并转成 ``str``。
        """
        if not uuids:
            return 0
        affected = 0
        with self._transaction() as conn:
            for chunk in chunked(uuids):
                statement = sql.replace("{ph}", placeholders(len(chunk)))
                cursor = conn.execute(statement, (*prefix, *chunk))
                affected += cursor.rowcount
        return affected
