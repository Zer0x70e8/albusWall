#
"""``AlbumService`` —— 相簿本身的生命周期（``albums`` 的唯一写入口）。

职责范围
--------
* 建 / 改名 / 改描述 / 设封面 / 排序 / 软删 / 恢复；
* **不管** `album_assets` 的成员增删 —— 那是资产写用例的主语
  （``AssetOpsService.add_to_album`` / ``remove_from_album`` /
  ``move_between_albums``）。

读走 ``ViewService``（同一套可见性口径），写走这里。因此"列表里看得到"和
"能改名"永远一致：两者都基于 ``albums.is_deleted = 0``。

与 ``AssetOpsService`` 共享的守卫
---------------------------------
"不能改系统相册 / 回收站相册"这条，本服务**复用** ``AssetOpsService`` 的实现
语义，但**不**注入它（那会造出一条"相簿写依赖资产写"的怪边）。这里只依赖
``ViewRepository`` 的两个判定函数，与 ``AssetOpsService.ensure_mutable_album``
逐字同源 —— 名字相同、顺序相同（先回收站、再系统、最后存在性），因为
虚拟相册不落表，顺序反了会把 Trash 误判成"不存在"。

线程语义
--------
纯 DB 写，无后台行为、无信号、不切线程。UI 写路径直接调用即可；命令层需要
跨线程安全时由 ``MainLoopService`` 在调用侧决定（相簿写不碰任何属主线程状态）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional
from uuid import UUID

from albuswall.dto.album.command import AlbumCreate, AlbumReorder, AlbumUpdate
from albuswall.dto.album.query import Album
from albuswall.dto.utils import to_uuid
from albuswall.log import Logger, getLogger
from albuswall.service.errors import AlbumNotFound, NotAMutableAlbum

if TYPE_CHECKING:  # 只为类型标注；运行时鸭子类型，避免装配顺序依赖
    from albuswall.repositories.album import AlbumRepository
    from albuswall.repositories.view import ViewRepository

__all__ = ["AlbumService"]

_UUIDLike = UUID | str

_logger: Logger = getLogger(__name__)


class AlbumService:
    """相簿写用例（被动服务）。"""

    def __init__(
            self,
            album_repo: "AlbumRepository",
            view_repo: "ViewRepository",
    ) -> None:
        self._albums = album_repo
        self._view = view_repo

    def describe(self, *, verbose: bool = False) -> str:
        base = "AlbumService()"
        if verbose:
            base = base[:-1] + (
                f"album={type(self._albums).__name__}, "
                f"view={type(self._view).__name__})"
            )
        return base

    # ------------------------------------------------------------------ #
    # 守卫
    # ------------------------------------------------------------------ #
    def ensure_mutable_album(self, album_uuid: _UUIDLike) -> None:
        """确保相簿存在且不是系统 / 回收站相册。

        顺序刻意与 ``AssetOpsService.ensure_mutable_album`` 一致：先回收站、
        再系统相册、最后存在性（虚拟相册不落 ``albums`` 表）。

        Raises:
            NotAMutableAlbum: 回收站相册（``reason="trash"``）/ 系统相册
                （``reason="system"``）。
            AlbumNotFound: 不存在或已软删。
        """
        uuid = to_uuid(album_uuid)
        if self._view.is_trash_album(uuid):
            raise NotAMutableAlbum(uuid, reason="trash")
        if self._view.is_system_album(uuid):
            raise NotAMutableAlbum(uuid, reason="system")
        if not self._view.album_exists(uuid):
            raise AlbumNotFound(uuid)

    # ------------------------------------------------------------------ #
    # 写
    # ------------------------------------------------------------------ #
    def create(self, album: AlbumCreate) -> Album:
        """建相簿，返回读模型（调用方通常要立刻显示它）。

        ``cover_uuid`` 指向的资产不可见时，封面留空而不是报错 —— 建相簿不该
        因为"顺手指定的封面已经不在"而失败。
        """
        new_uuid = self._albums.create(album)
        created = self._view.get_album(new_uuid)
        if created is None:  # pragma: no cover - 刚建的行必然可见
            raise AlbumNotFound(new_uuid)
        return created

    def update(self, album_uuid: _UUIDLike, patch: AlbumUpdate) -> bool:
        """PATCH 相簿；返回是否真的写了库（空 patch / 无变化 → False）。

        Raises:
            AlbumNotFound / NotAMutableAlbum: 守卫失败。
        """
        self.ensure_mutable_album(album_uuid)
        return self._albums.update(album_uuid, patch)

    def rename(self, album_uuid: _UUIDLike, new_title: str) -> bool:
        """改标题（空标题由 ``AlbumUpdate.derived_title`` 抛 ``ValueError``）。"""
        return self.update(album_uuid, AlbumUpdate(title=new_title))

    def set_cover(
            self, album_uuid: _UUIDLike, cover_uuid: Optional[_UUIDLike],
    ) -> bool:
        """设封面；``None`` = 清空。"""
        return self.update(
            album_uuid, AlbumUpdate(cover_uuid=cover_uuid),
        )

    def reorder(self, order: AlbumReorder) -> int:
        """按给定顺序重排相簿（一个事务内写完），返回更新的行数。

        不存在的 uuid 被跳过（UI 拿过期列表重排很常见）。
        """
        return self._albums.reorder(order.album_uuids)

    def delete(self, album_uuid: _UUIDLike, *, cascade: bool = False) -> bool:
        """软删相簿（成员行默认保留，恢复时原样回来）。

        Raises:
            AlbumNotFound / NotAMutableAlbum: 守卫失败。
        """
        self.ensure_mutable_album(album_uuid)
        return self._albums.soft_delete(album_uuid, cascade=cascade)

    def restore(self, album_uuid: _UUIDLike) -> bool:
        """撤销软删。

        与 ``delete`` 不同，这里**不**做可写性守卫：被软删的相簿已经不满足
        ``album_exists``，"恢复"本来就是对不可见对象的操作。只拒绝虚拟相册
        （它们没有行可恢复）。
        """
        uuid = to_uuid(album_uuid)
        if self._view.is_trash_album(uuid) or self._view.is_system_album(uuid):
            raise NotAMutableAlbum(uuid, reason="system")
        return self._albums.restore(uuid)

    # ------------------------------------------------------------------ #
    # 读（少量确认用；完整读走 ViewService）
    # ------------------------------------------------------------------ #
    def get(self, album_uuid: _UUIDLike) -> Album:
        """取相簿 DTO（不可见 / 不存在 → ``AlbumNotFound``）。

        只是为了"写完立刻回读"方便；列表 / 详情请走 ``ViewService``。
        """
        album = self._view.get_album(album_uuid)
        if album is None:
            raise AlbumNotFound(to_uuid(album_uuid))
        return album

    def exists(
            self, album_uuid: _UUIDLike, *, include_deleted: bool = False,
    ) -> bool:
        return self._albums.exists(
            album_uuid, include_deleted=include_deleted,
        )

    def count_members(self, album_uuid: _UUIDLike) -> int:
        """成员数（不过滤资产可见性）。"""
        return self._albums.count_members(album_uuid)
