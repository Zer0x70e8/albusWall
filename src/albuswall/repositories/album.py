#
"""相簿仓储：``albums`` 的写入口 + ``album_assets`` 的排序维护。

与 ``ViewRepository`` 的分工（本域唯一的读 / 写分界）
-----------------------------------------------------
* ``ViewRepository``（读）—— 相簿 DTO、封面、成员列表、邻接位置，口径是
  "给 UI 渲染什么"；
* ``AlbumRepository``（写，本模块）—— 建 / 改 / 软删 / 恢复相簿，设封面，
  维护 ``sort_order``。

``album_assets`` 的**成员增删**不在本模块：那是资产写用例
（``AssetRepository.add_to_album`` / ``remove_from_album`` /
``move_between_albums``），因为它们以"一批资产"为主语；而本模块的主语是
"一个相簿"（改名、封面、排序、删除）。两者都写 ``albums`` 侧或
``album_assets`` 侧，但事务的边界不同，合在一起会让"谁负责锁哪张表"变得含糊。

不变量
------
* 相簿名**不做**全库唯一：``albums`` 表没有 title 唯一索引，同名相簿是合法的
  （用户可能有"2024 精选"两个）。因此本模块不捕获重名，也不提供
  ``find_by_title``——要查同名请用读路径。
* ``cover_asset_id`` 指向的资产**必须可见**才会写入；写入时用
  ``SELECT ... WHERE assets 可见`` 的子查询，资产不可见/不存在就不更新封面
  列（把 NULL 写进去是合法的，但要由调用方显式表达，见 ``AlbumUpdate``）。
* 软删相簿不动 ``album_assets`` 行：恢复时成员关系原样回来。
"""

from __future__ import annotations

import uuid
from typing import Optional, Sequence
from uuid import UUID

from albuswall.dto.album.command import AlbumCreate, AlbumUpdate
from albuswall.dto.sentinel import UNSET
from albuswall.dto.utils import to_uuid
from albuswall.log import Logger, getLogger

from .base import BaseRepository
from .utils.sql_helpers import as_uuid_str
from .utils.visibility import active_asset_where

__all__ = ["AlbumRepository"]

_UUIDLike = UUID | str

_logger: Logger = getLogger(__name__)

#: 「可见资产」条件（别名 ``asset``）；设封面时用它过滤。
_ACTIVE_ASSET_WHERE = active_asset_where("asset")


class AlbumRepository(BaseRepository):
    """相簿写仓储。"""

    # ==================================================================
    # 建 / 改
    # ==================================================================
    def create(self, album: AlbumCreate) -> UUID:
        """建相簿，返回新 uuid（标题已由 DTO 校验非空）。"""
        new_uuid = str(uuid.uuid4())
        cover_uuid = (
            None if album.cover_uuid is None else as_uuid_str(album.cover_uuid)
        )
        if cover_uuid is None:
            self._execute(
                """
                INSERT INTO albums (uuid, title, album_type, description,
                                    sort_order, cover_asset_id)
                VALUES (?, ?, ?, ?, ?, NULL)
                """,
                (new_uuid, album.title, album.album_type,
                 album.description, album.sort_order),
            )
        else:
            self._execute(
                f"""
                INSERT INTO albums (uuid, title, album_type, description,
                                    sort_order, cover_asset_id)
                SELECT ?, ?, ?, ?, ?,
                       (SELECT asset.id FROM assets AS asset
                         WHERE asset.uuid = ? AND {_ACTIVE_ASSET_WHERE})
                """,
                (new_uuid, album.title, album.album_type,
                 album.description, album.sort_order, cover_uuid),
            )
        return to_uuid(new_uuid)

    def update(self, album_uuid: _UUIDLike, patch: AlbumUpdate) -> bool:
        """PATCH 相簿；空 patch 或目标不存在（含已软删）返回 False。

        ``cover_uuid`` 三态：``UNSET`` 不动 / ``None`` 清空 / uuid 设为该资产
        （资产不可见时子查询给 NULL，等价于清空 —— 这是刻意的：把封面指向
        一张已经看不到的资产没有意义）。
        """
        if patch.is_empty():
            return False

        set_parts: list[str] = []
        params: list = []

        title = patch.derived_title
        if title is not None:
            set_parts.append("title = ?")
            params.append(title)
        if patch.description is not UNSET:
            set_parts.append("description = ?")
            params.append(str(patch.description or ""))
        if patch.album_type is not UNSET:
            set_parts.append("album_type = ?")
            params.append(int(patch.album_type))
        if patch.sort_order is not UNSET:
            set_parts.append("sort_order = ?")
            params.append(int(patch.sort_order))
        if patch.cover_uuid is not UNSET:
            if patch.cover_uuid is None:
                set_parts.append("cover_asset_id = NULL")
            else:
                set_parts.append(
                    "cover_asset_id = (SELECT asset.id FROM assets AS asset "
                    f"WHERE asset.uuid = ? AND {_ACTIVE_ASSET_WHERE})"
                )
                params.append(as_uuid_str(patch.cover_uuid))

        params.append(as_uuid_str(album_uuid))
        cursor = self._execute(
            f"UPDATE albums SET {', '.join(set_parts)} "
            f"WHERE uuid = ? AND is_deleted = 0",
            tuple(params),
        )
        return cursor.rowcount > 0

    def set_cover(
            self, album_uuid: _UUIDLike, cover_uuid: Optional[_UUIDLike],
    ) -> bool:
        """只改封面（``None`` = 清空）。返回目标相簿是否存在且可见。"""
        return self.update(
            album_uuid, AlbumUpdate(cover_uuid=cover_uuid),
        )

    def reorder(self, album_uuids: Sequence[_UUIDLike]) -> int:
        """按给定顺序重写 ``sort_order``（第 i 个 → i），返回更新的行数。

        整个重排在一个事务里：中途失败时不会留下"一半新序一半旧序"。
        不存在的 uuid 会被跳过（不视为错误：UI 拿着过期列表重排很常见）。
        """
        if not album_uuids:
            return 0
        updated = 0
        with self._transaction() as conn:
            for index, album_uuid in enumerate(album_uuids):
                cursor = conn.execute(
                    "UPDATE albums SET sort_order = ? "
                    " WHERE uuid = ? AND is_deleted = 0",
                    (index, as_uuid_str(album_uuid)),
                )
                updated += cursor.rowcount
        return updated

    # ==================================================================
    # 软删 / 恢复
    # ==================================================================
    def soft_delete(self, album_uuid: _UUIDLike, *, cascade: bool = False) -> bool:
        """软删相簿。

        ``cascade=False``（默认）只标相簿自身；``album_assets`` 行**保留**，
        恢复时成员关系原样回来。``cascade=True`` 额外清掉成员关系（用于
        "删除相簿并彻底断开资产归属"的场景，比如导入目录结构重建）。

        返回是否真的改动（已软删 / 不存在 → False，幂等）。
        """
        target = as_uuid_str(album_uuid)
        with self._transaction() as conn:
            cursor = conn.execute(
                "UPDATE albums SET is_deleted = 1 "
                " WHERE uuid = ? AND is_deleted = 0",
                (target,),
            )
            if cursor.rowcount == 0:
                return False
            if cascade:
                conn.execute(
                    """
                    DELETE FROM album_assets
                     WHERE album_id = (SELECT id FROM albums WHERE uuid = ?)
                    """,
                    (target,),
                )
        return True

    def restore(self, album_uuid: _UUIDLike) -> bool:
        """撤销软删；未软删 / 不存在 → False。"""
        cursor = self._execute(
            "UPDATE albums SET is_deleted = 0 "
            " WHERE uuid = ? AND is_deleted = 1",
            (as_uuid_str(album_uuid),),
        )
        return cursor.rowcount > 0

    # ==================================================================
    # 读（写路径需要的少量判定，避免绕去 ViewRepository）
    # ==================================================================
    def exists(
            self, album_uuid: _UUIDLike, *, include_deleted: bool = False,
    ) -> bool:
        """相簿行是否存在（``include_deleted`` 控制是否连同软删一起算）。"""
        where = "uuid = ?" if include_deleted else "uuid = ? AND is_deleted = 0"
        row = self._fetchone(
            f"SELECT 1 FROM albums WHERE {where} LIMIT 1",
            (as_uuid_str(album_uuid),),
        )
        return row is not None

    def count_members(self, album_uuid: _UUIDLike) -> int:
        """相簿成员数（**不过滤资产可见性**，用于"这个相簿空了吗"的判定）。"""
        row = self._fetchone(
            """
            SELECT COUNT(*) AS cnt
              FROM album_assets AS aa
              JOIN albums AS a ON a.id = aa.album_id
             WHERE a.uuid = ?
            """,
            (as_uuid_str(album_uuid),),
        )
        return int(row["cnt"]) if row else 0
