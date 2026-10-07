#
""""""

from datetime import datetime
from logging import getLogger
from typing import TYPE_CHECKING, NamedTuple, Optional, Union
from uuid import UUID

from albuswall.log import TRACE, Logger
from albuswall.dto.album import Album, AssetDTO

if TYPE_CHECKING:
    from albuswall.repositories import ViewRepository

_logger: Logger = getLogger(__name__)  # type: ignore
_logger.trace = lambda msg, *args: _logger.log(TRACE, msg, *args)

# 模块加载时间，用作虚拟相册的创建/修改时间（import 时定格一次）
_MODULE_STARTED_AT: str = datetime.now().isoformat()

# ======================================================================
# 公开常量
# ======================================================================

VIRTUAL_ALBUM_ALL_UUID: UUID = UUID("00000000-0000-0000-0000-000000000001")
VIRTUAL_ALBUM_TRASH_UUID: UUID = UUID("00000000-0000-0000-0000-000000000002")

SCOPE_ACTIVE: str = "active"
SCOPE_DELETED: str = "deleted"


class _VirtualAlbumSpec(NamedTuple):
    """虚拟相册的静态描述。"""
    title: str
    description: str
    scope: str
    sentinel_id: int


class ViewService:
    """相簿视图服务（只读组合）。

    接口契约（P1 收敛）：
        · 全部入参 / 出参只走 uuid；``assets.id`` / ``albums.id`` 不暴露。
        · 虚拟相册（All / Trash）与物理相册走同一批接口，分派对调用方透明。
        · 写操作（收藏 / 软删 / 恢复 / 硬删 / 相簿成员增删）见 AssetRepository。

    组合规则：
        - ``is_virtual(album)  := get_scope(album) is not None``
        - ``trash_album(album) := get_scope(album) == SCOPE_DELETED``
        - 因此不再单独提供 is_virtual_album / should_include_deleted。
    """

    def __init__(self, repo: "ViewRepository"):
        self._repo = repo

    # ---------------- 唯一分派点 ----------------
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

    def _spec(self, uuid) -> Optional[_VirtualAlbumSpec]:
        return self._VIRTUAL.get(self._as_uuid_str(uuid))

    @staticmethod
    def _as_uuid_str(value: Union[UUID, str]) -> str:
        """UUID / 字符串统一规范化为标准字符串形式；非法字符串原样透传。"""
        if isinstance(value, UUID):
            return str(value)
        try:
            return str(UUID(str(value)))
        except (ValueError, AttributeError, TypeError):
            return str(value)

    # ==================================================================
    # 相册
    # ==================================================================

    def get_scope(self, album_uuid: Union[UUID, str]) -> Optional[str]:
        """返回相册的 scope；物理相册返回 None。

        同时充当虚拟 / Trash 判定的唯一出口：

            is_virtual(album)  := get_scope(album) is not None
            is_trash(album)    := get_scope(album) == SCOPE_DELETED
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
        physical = [UUID(i) for i in self._repo.list_album_uuids()]
        result = virtual + physical
        _logger.trace("album uuids: virtual=%s, result=%s", virtual, result)
        return result

    def get_album(self, album_uuid: Union[UUID, str]) -> Optional[Album]:
        """取相册 DTO；虚拟相册即时构造，物理相册走 repo。"""
        spec = self._spec(album_uuid)
        if spec is None:
            return self._repo.get_album(self._as_uuid_str(album_uuid))
        cover = self._repo.get_cover_asset_by_scope(spec.scope)
        return Album(
            id=spec.sentinel_id,
            uuid=UUID(self._as_uuid_str(album_uuid)),
            title=spec.title,
            description=spec.description,
            cover=UUID(cover.uuid) if cover else None,
            type=0,
            created_at=_MODULE_STARTED_AT,
            modified_at=_MODULE_STARTED_AT,
        )

    def get_album_cover(
            self, album_uuid: Union[UUID, str]
    ) -> Optional[AssetDTO]:
        """取相册封面资产 DTO（虚拟相册取 scope 首项）。"""
        spec = self._spec(album_uuid)
        if spec is None:
            return self._repo.get_album_cover(self._as_uuid_str(album_uuid))
        return self._repo.get_cover_asset_by_scope(spec.scope)

    def get_album_uuids_of_asset(
            self, asset_uuid: Union[UUID, str]
    ) -> list[UUID]:
        """返回包含指定资产的全部可见相册 uuid。"""
        rows = self._repo.get_album_uuids_of_asset(self._as_uuid_str(asset_uuid))
        return [UUID(u) for u in (rows or [])]

    # ==================================================================
    # 资产（DTO / 列表 / uuid 列表）
    # ==================================================================

    def get_asset(
            self,
            asset_uuid: Union[UUID, str],
            *,
            include_deleted: bool = False,
    ) -> Optional[AssetDTO]:
        """按 uuid 取 AssetDTO。

        Args:
            asset_uuid: assets.uuid。
            include_deleted: Trash 场景传 True。
        """
        return self._repo.get_asset(
            self._as_uuid_str(asset_uuid), include_deleted=include_deleted
        )

    def list_assets(
            self,
            album_uuid: Union[UUID, str],
            *,
            offset: int = 0,
            limit: int = 100,
    ) -> list[AssetDTO]:
        """列出相册内资产 DTO。

        - 物理相册：分页，走 repo。
        - 虚拟相册：忽略 offset/limit，全量返回（分页由上层自行处理）。
        """
        spec = self._spec(album_uuid)
        if spec is not None:
            return self._repo.list_assets_by_scope(spec.scope) or []
        return self._repo.list_assets(
            self._as_uuid_str(album_uuid), offset=offset, limit=limit
        ) or []

    def get_asset_uuids(self, album: Union[Album, UUID, str]) -> list[UUID]:
        """按相册列出可见 asset uuid。

        排序契约由 ViewRepository._ASSET_ORDER_SQL 单点决定，
        本方法与 list_assets / get_asset_neighbours 完全同源。
        """
        uuid = album.uuid if isinstance(album, Album) else album
        spec = self._spec(uuid)
        if spec is None:
            rows = self._repo.list_asset_uuids_by_album(self._as_uuid_str(uuid))
        else:
            rows = self._repo.list_asset_uuids_by_scope(spec.scope)
        return [UUID(u) for u in (rows or [])]

    # ==================================================================
    # 磁盘完整路径
    # ==================================================================

    def get_asset_full_path(
            self,
            asset_uuid: Union[UUID, str],
            *,
            include_deleted: bool = False,
    ) -> Optional[str]:
        """按资产 uuid 取磁盘完整路径。

        Args:
            asset_uuid: 资产 uuid。
            include_deleted: True 时连同软删资产一起返回路径（Trash 用）。
        """
        return self._repo.get_asset_path(
            self._as_uuid_str(asset_uuid), include_deleted=include_deleted
        )

    def get_cover_full_path(
            self,
            album_uuid: Union[UUID, str],
            *,
            include_deleted: bool = False,
    ) -> Optional[str]:
        """按相册 uuid 取封面磁盘完整路径。

        组合流程：album_uuid → 封面 AssetDTO → asset uuid → 完整路径。
        """
        cover = self.get_album_cover(album_uuid)
        if cover is None:
            return None
        return self.get_asset_full_path(
            cover.uuid, include_deleted=include_deleted
        )

    # ==================================================================
    # 上/下一张定位
    # ==================================================================

    def get_asset_neighbours(
            self,
            album_uuid: Union[UUID, str],
            current_asset_uuid: Union[UUID, str],
    ) -> tuple[Optional[UUID], Optional[UUID], int, int]:
        """定位资产在相册中的上/下一张。

        分派规则与 get_asset_uuids / list_assets 完全一致；排序契约由
        ViewRepository._ASSET_ORDER_SQL 单点决定。

        Returns:
            (prev_uuid, next_uuid, index, total)
              - index 1-based；
              - 当前资产不在相册中时 (None, None, 0, 0)；
              - 相册为空或不存在时同样返回 (None, None, 0, 0)。
        """
        spec = self._spec(album_uuid)
        if spec is not None:
            prev_s, next_s, index, total = self._repo.get_neighbours_by_scope(
                spec.scope, self._as_uuid_str(current_asset_uuid)
            )
        else:
            prev_s, next_s, index, total = self._repo.get_neighbours_by_album(
                self._as_uuid_str(album_uuid),
                self._as_uuid_str(current_asset_uuid),
            )
        return (
            UUID(prev_s) if prev_s else None,
            UUID(next_s) if next_s else None,
            index,
            total,
        )

    def locate_in_album(
            self,
            album_uuid: Union[UUID, str],
            asset_uuid: Union[UUID, str],
    ) -> Optional[int]:
        """只返回资产在相册中的 1-based 位置；不在相册中返回 None。

        用于只关心「第几张 / 共几张」的 UI，避免走 get_asset_neighbours
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

    # ==================================================================
    # 杂项
    # ==================================================================

    def __str__(self) -> str:
        return "\n".join((
            f"{type(self).__name__} (",
            "\t_VIRTUAL: [",
            *[f"\t\t{i}," for i in self._VIRTUAL],
            "\t]",
            ")",
        ))
