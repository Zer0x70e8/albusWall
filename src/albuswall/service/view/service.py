#
"""``ViewService`` —— 相簿 / 资产的**只读**组合门面。

接口契约
--------
* 全部入参 / 出参只走 **uuid**；``assets.id`` / ``albums.id`` 不暴露；
* 虚拟相册（All / Trash）与物理相册走同一批接口，分派对调用方透明；
* 写操作（收藏 / 软删 / 恢复 / 硬删 / 相簿成员增删）在
  :mod:`albuswall.service.asset.command`，本服务不提供。

组合规则（唯一分派点 :meth:`get_scope`）::

    is_virtual(album) := get_scope(album) is not None
    is_trash(album)   := get_scope(album) == SCOPE_DELETED

因此不再单独提供 ``is_virtual_album`` / ``should_include_deleted``。

为什么需要 ``assets_viewed`` 信号
---------------------------------
缩略图是**按需生成**的（见 ``service/thumbnail/service.py``）：没有任何地方会
在导入后批量回填。判断"哪些资产真的被看到了"的唯一位置就是本服务 ——
它就是 UI 取数据的那道门。因此本服务在**返回资产 DTO** 的三处广播
``assets_viewed``：

* :meth:`list_assets`     —— 一页列表；
* :meth:`get_asset`       —— 单张详情；
* :meth:`get_album_cover` —— 封面。

契约：``cb(sender: ViewService, *, asset_uuids: tuple[UUID, ...])``。装配层把它
接到 ``ThumbnailService.request_bulk``（有上限、去重、冷却，见那边的 docstring）。

为什么**不**在 :meth:`get_asset_uuids` / :meth:`get_asset_neighbours` 里发：它们
只给"上一张 / 下一张"提供 uuid，真正的加载会走 ``get_asset``；在那里再发一次
会让同一批资产被排两遍队（去重能兜住，但没必要）。

线程语义
--------
本服务是纯读：不写任何运行时状态，任何线程调用都安全，因此**不**接
``MainLoopService``。订阅方（缩略图服务）自己负责把任务丢给 TaskService。

读侧异常约定：``repo`` 的返回是``Optional`` / 空列表，本服务如实透传，
**不**把 ``None`` 变成异常 —— UI 对"相册不存在"和"相册是空的"有不同呈现。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple, Optional, Union
from uuid import UUID

from albuswall.common.constants.albums import (
    VIRTUAL_ALBUM_ALL_UUID,
    VIRTUAL_ALBUM_TRASH_UUID,
)
from albuswall.dto.album import Album, Asset
from albuswall.log import Logger, getLogger
from albuswall.utils.signals import SignalDescriptor
from albuswall.utils.time import now_str

if TYPE_CHECKING:  # 只为类型标注；运行时鸭子类型，避免装配顺序依赖
    from albuswall.repositories.view import ViewRepository

__all__ = [
    "SCOPE_ACTIVE",
    "SCOPE_DELETED",
    "ViewService",
]

_logger: Logger = getLogger(__name__)

# 模块加载时间，用作虚拟相册的创建/修改时间（import 时定格一次）。
# 与落库时间同格式（utils.time），避免 UI 上出现两种时间形态。
_MODULE_STARTED_AT: str = now_str()

SCOPE_ACTIVE: str = "active"
SCOPE_DELETED: str = "deleted"

_UuidLike = Union[UUID, str]


class _VirtualAlbumSpec(NamedTuple):
    """虚拟相册的静态描述（不对应 ``albums`` 表的任何一行）。"""

    title: str
    description: str
    scope: str
    sentinel_id: int


class ViewService:
    """相簿 / 资产只读组合。"""

    # 类属性 → 必须用 SignalDescriptor（lazy、实例间共享）。
    # 契约：cb(sender: ViewService, *, asset_uuids: tuple[UUID, ...])
    assets_viewed = SignalDescriptor("assets_viewed")

    # 系统相册哨兵 uuid 的唯一真源在 common/constants/albums.py；
    # 判定函数见 ViewRepository.is_system_album / is_trash_album。
    _VIRTUAL: dict[str, _VirtualAlbumSpec] = {
        str(VIRTUAL_ALBUM_ALL_UUID): _VirtualAlbumSpec(
            title="All",
            description="All active albums",
            scope=SCOPE_ACTIVE,
            sentinel_id=-1,
        ),
        str(VIRTUAL_ALBUM_TRASH_UUID): _VirtualAlbumSpec(
            title="Trash",
            description="Deleted albums",
            scope=SCOPE_DELETED,
            sentinel_id=-2,
        ),
    }

    def __init__(self, repo: "ViewRepository") -> None:
        self._repo = repo

    def describe(self, *, verbose: bool = False) -> str:
        base = f"ViewService(virtual={len(self._VIRTUAL)})"
        if verbose:
            base = base[:-1] + f", repo={type(self._repo).__name__})"
        return base

    # ==================================================================
    # 内部工具
    # ==================================================================
    def _spec(self, uuid: _UuidLike) -> Optional[_VirtualAlbumSpec]:
        return self._VIRTUAL.get(self._as_uuid_str(uuid))

    @staticmethod
    def _as_uuid_str(value: _UuidLike) -> str:
        """UUID / 字符串统一规范化为标准字符串；非法输入原样透传。"""
        if isinstance(value, UUID):
            return str(value)
        try:
            return str(UUID(str(value)))
        except (ValueError, AttributeError, TypeError):
            return str(value)

    def _announce_viewed(self, assets) -> None:
        """广播"这些资产被看到了"（按需缩略图的唯一触发点）。"""
        if not assets:
            return
        uuids = tuple(
            a.uuid for a in assets
            if isinstance(getattr(a, "uuid", None), UUID)
        )
        if not uuids:
            return
        try:
            self.assets_viewed.emit(self, asset_uuids=uuids)
        except Exception:  # noqa: BLE001 —— 订阅方异常不该打断读路径
            _logger.exception("emit assets_viewed failed (%d uuids)", len(uuids))

    # ==================================================================
    # 相册
    # ==================================================================
    def get_scope(self, album_uuid: _UuidLike) -> Optional[str]:
        """返回相册的 scope；物理相册返回 ``None``。

        同时充当虚拟 / Trash 判定的唯一出口::

            is_virtual(album) := get_scope(album) is not None
            is_trash(album)   := get_scope(album) == SCOPE_DELETED
        """
        spec = self._spec(album_uuid)
        return spec.scope if spec else None

    @classmethod
    def list_virtual_album_uuids(cls) -> list[UUID]:
        """按定义顺序返回全部虚拟相册 uuid。"""
        return [UUID(u) for u in cls._VIRTUAL]

    def list_album_uuids(self) -> list[UUID]:
        """返回所有可见相册 uuid（虚拟在前，物理在后）。"""
        virtual = self.list_virtual_album_uuids()
        return virtual + self._repo.list_album_uuids()

    def get_album(self, album_uuid: _UuidLike) -> Optional[Album]:
        """取相册 DTO；虚拟相册即时构造，物理相册走 repo。"""
        spec = self._spec(album_uuid)
        if spec is None:
            return self._repo.get_album(self._as_uuid_str(album_uuid))
        cover = self._repo.get_cover_asset_by_scope(spec.scope)
        return Album(
            uuid=UUID(self._as_uuid_str(album_uuid)),
            title=spec.title,
            description=spec.description,
            cover=cover.uuid if cover else None,
            type=0,
            created_at=_MODULE_STARTED_AT,
            modified_at=_MODULE_STARTED_AT,
        )

    def get_album_cover(self, album_uuid: _UuidLike) -> Optional[Asset]:
        """取相册封面资产 DTO（虚拟相册取 scope 首项）。"""
        spec = self._spec(album_uuid)
        if spec is None:
            cover = self._repo.get_album_cover(
                self._as_uuid_str(album_uuid))
        else:
            cover = self._repo.get_cover_asset_by_scope(spec.scope)
        self._announce_viewed([cover] if cover else [])
        return cover

    def get_album_uuids_of_asset(self, asset_uuid: _UuidLike) -> list[UUID]:
        """返回包含指定资产的全部可见相册 uuid。"""
        return self._repo.get_album_uuids_of_asset(
            self._as_uuid_str(asset_uuid)
        )

    # ==================================================================
    # 资产（DTO / 列表 / uuid 列表）
    # ==================================================================
    def get_asset(
            self,
            asset_uuid: _UuidLike,
            *,
            include_deleted: bool = False,
    ) -> Optional[Asset]:
        """按 uuid 取 Asset。

        Args:
            asset_uuid: ``assets.uuid``。
            include_deleted: 回收站场景传 True。
        """
        asset = self._repo.get_asset(
            self._as_uuid_str(asset_uuid), include_deleted=include_deleted
        )
        self._announce_viewed([asset] if asset else [])
        return asset

    def list_assets(
            self,
            album_uuid: _UuidLike,
            *,
            offset: int = 0,
            limit: int = 100,
    ) -> list[Asset]:
        """列出相册内资产 DTO。

        * 物理相册：分页，走 repo；
        * 虚拟相册：忽略 offset/limit，全量返回（分页由上层自行处理）。
        """
        spec = self._spec(album_uuid)
        if spec is not None:
            assets = self._repo.list_assets_by_scope(spec.scope) or []
        else:
            assets = self._repo.list_assets(
                self._as_uuid_str(album_uuid), offset=offset, limit=limit
            ) or []
        self._announce_viewed(assets)
        return assets

    def get_asset_uuids(self, album: Union[Album, _UuidLike]) -> list[UUID]:
        """按相册列出可见 asset uuid。

        排序契约由 ``repositories/utils/asset_order.py`` 单点决定，本方法与
        ``list_assets`` / ``get_asset_neighbours`` 完全同源。
        """
        uuid = album.uuid if isinstance(album, Album) else album
        spec = self._spec(uuid)
        if spec is None:
            return self._repo.list_asset_uuids_by_album(
                self._as_uuid_str(uuid)
            )
        return self._repo.list_asset_uuids_by_scope(spec.scope)

    # ==================================================================
    # 磁盘完整路径
    # ==================================================================
    def get_asset_full_path(
            self,
            asset_uuid: _UuidLike,
            *,
            include_deleted: bool = False,
    ) -> Optional[str]:
        """按资产 uuid 取磁盘完整路径。

        Args:
            asset_uuid: 资产 uuid。
            include_deleted: True 时连同软删资产一起返回路径（回收站用）。
        """
        return self._repo.get_asset_path(
            self._as_uuid_str(asset_uuid), include_deleted=include_deleted
        )

    def get_cover_full_path(
            self,
            album_uuid: _UuidLike,
            *,
            include_deleted: bool = False,
    ) -> Optional[str]:
        """按相册 uuid 取封面磁盘完整路径（原图）。

        优先级：

        1. ``albums.cover_asset_id`` 指向的显式封面；
        2. 相册内排序意义上的最新资产（物理相册，见 :meth:`_fallback_cover`）；
        3. ``None``。

        第 2 条是「移入资产后封面不刷新」的修复点：新建 / 空相册的
        ``cover_asset_id`` 是 NULL，成员关系一变就必须按当前成员重新取首项，
        否则封面会一直停在空白上。
        """
        cover = self.get_album_cover(album_uuid)
        if cover is None:
            cover = self._fallback_cover(album_uuid)
        if cover is None:
            return None

        # 虚拟相册的 cover 已经由 scope 本身保证了可见性（Trash 的 cover
        # 天然是软删资产），这里不能再二次过滤。
        if self._spec(album_uuid) is not None:
            include_deleted = True

        return self.get_asset_full_path(
            cover.uuid, include_deleted=include_deleted
        )

    def _fallback_cover(self, album_uuid: _UuidLike) -> Optional[Asset]:
        """没有显式封面时的兜底：物理相册内排序意义上的**最新资产**。

        排序契约与列表 / 邻居同源（``repositories/utils/asset_order.py``：
        ``taken_at DESC, id DESC``），所以「刚移入的那张」会排在首位。
        虚拟相册（All / Trash）不走这里 —— 它们的封面由 scope 自己给出。
        """
        if self._spec(album_uuid) is not None:
            return None
        try:
            assets = self._repo.list_assets(
                self._as_uuid_str(album_uuid), offset=0, limit=1)
        except Exception:  # noqa: BLE001 - 取不到封面不该打断读路径
            _logger.exception(
                "cover fallback failed for album=%s", album_uuid)
            return None
        cover = assets[0] if assets else None
        self._announce_viewed([cover] if cover else [])
        return cover

    # ==================================================================
    # 上/下一张定位
    # ==================================================================
    def get_asset_neighbours(
            self,
            album_uuid: _UuidLike,
            current_asset_uuid: _UuidLike,
    ) -> tuple[Optional[UUID], Optional[UUID], int, int]:
        """定位资产在相册中的上/下一张。

        分派规则与 ``get_asset_uuids`` / ``list_assets`` 完全一致；排序契约由
        ``repositories/utils/asset_order.py`` 单点决定。

        Returns:
            ``(prev_uuid, next_uuid, index, total)``：``index`` 从 1 开始；
            当前资产不在相册中、相册为空或不存在时返回
            ``(None, None, 0, 0)``。
        """
        spec = self._spec(album_uuid)
        if spec is not None:
            return self._repo.get_neighbours_by_scope(
                spec.scope, self._as_uuid_str(current_asset_uuid)
            )
        return self._repo.get_neighbours_by_album(
            self._as_uuid_str(album_uuid),
            self._as_uuid_str(current_asset_uuid),
        )

    def locate_in_album(
            self,
            album_uuid: _UuidLike,
            asset_uuid: _UuidLike,
    ) -> Optional[int]:
        """只返回资产在相册中的 1-based 位置；不在相册中返回 ``None``。

        用于只关心「第几张 / 共几张」的 UI，避免走 ``get_asset_neighbours``
        的多列窗口计算。
        """
        spec = self._spec(album_uuid)
        if spec is not None:
            return self._repo.locate_in_scope(
                spec.scope, self._as_uuid_str(asset_uuid)
            )
        return self._repo.locate_in_album(
            self._as_uuid_str(album_uuid), self._as_uuid_str(asset_uuid)
        )

    def __str__(self) -> str:
        return "\n".join((
            f"{type(self).__name__} (",
            "\t_VIRTUAL: [",
            *[f"\t\t{i}," for i in self._VIRTUAL],
            "\t]",
            ")",
        ))
