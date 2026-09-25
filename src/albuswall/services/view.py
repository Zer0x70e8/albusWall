#
""""""

from datetime import datetime
from pathlib import Path
from logging import getLogger
from typing import TYPE_CHECKING, NamedTuple, Optional, Union, overload
from uuid import UUID

from albuswall.log import TRACE, Logger
from albuswall.dto.album import Album, AssetDTO

if TYPE_CHECKING:
    from albuswall.repositories import ViewRepository

_logger: Logger = getLogger(__name__)  # type: ignore
_logger.trace = lambda msg, *args: _logger.log(TRACE, msg, *args)

# 模块加载时间，用作虚拟相册的创建/修改时间（在本模块 import 时定格一次）
_MODULE_STARTED_AT: str = datetime.now().isoformat()


# ======================================================================
# 公开常量（P2）
# ======================================================================

#: 虚拟相册 “All” 的固定 uuid
VIRTUAL_ALBUM_ALL_UUID: UUID = UUID("00000000-0000-0000-0000-000000000001")
#: 虚拟相册 “Trash” 的固定 uuid
VIRTUAL_ALBUM_TRASH_UUID: UUID = UUID("00000000-0000-0000-0000-000000000002")

#: scope 字面量
SCOPE_ACTIVE: str = "active"
SCOPE_DELETED: str = "deleted"


class _VirtualAlbumSpec(NamedTuple):
    """虚拟相册的静态描述。"""
    title: str
    description: str
    scope: str
    sentinel_id: int


class ViewService:
    """相簿视图服务。

    职责边界：
        只做读组合（repo 查询 + 虚拟相册分派）。
        收藏 / 软删 / 恢复等写操作走独立的 AssetRepository，
        不要塞进来污染视图职责。
    """

    def __init__(self, repo: "ViewRepository"):
        self._repo = repo

    # ---------------- 唯一分派点 ----------------
    # key: uuid 字符串；value: _VirtualAlbumSpec
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
        """将 UUID 或字符串统一规范化为标准字符串形式。

        传入 UUID 时转成标准小写带连字符格式；
        传入字符串时尽量解析成标准 UUID 后再输出，
        非法字符串则原样透传，交由底层查询返回空结果。
        """
        if isinstance(value, UUID):
            return str(value)
        try:
            return str(UUID(str(value)))
        except (ValueError, AttributeError, TypeError):
            return str(value)

    # ==================================================================
    # 虚拟相册 / scope 公开出口（P0）
    # ==================================================================

    def get_scope(self, album_uuid: Union[UUID, str]) -> Optional[str]:
        """返回相册的 scope。

        Args:
            album_uuid: 相册 uuid。

        Returns:
            ``"active"`` / ``"deleted"``；物理相册返回 None。
        """
        spec = self._spec(album_uuid)
        return spec.scope if spec else None

    def is_virtual_album(self, album_uuid: Union[UUID, str]) -> bool:
        """判断给定 uuid 是否对应虚拟相册（All / Trash）。"""
        return self._spec(album_uuid) is not None

    def should_include_deleted(self, album_uuid: Union[UUID, str]) -> bool:
        """根据相册 uuid 推断是否需要 include_deleted=True 取原图。

        Trash 虚拟相册返回 True；其余一律返回 False。
        供 DetailPresenter 在调用 get_asset_full_path* 前统一判断。
        """
        return self.get_scope(album_uuid) == SCOPE_DELETED

    @classmethod
    def list_virtual_album_uuids(cls) -> list[UUID]:
        """按定义顺序返回全部虚拟相册 uuid。"""
        return [UUID(u) for u in cls._VIRTUAL]

    # ==================================================================
    # 相簿（专辑）
    # ==================================================================

    def get_active_album_uuids(self) -> list[UUID]:
        virtual = self.list_virtual_album_uuids()
        result = virtual + [UUID(i) for i in self._repo.get_active_album_uuids()]
        _logger.trace("active uuids: virtual=%s, result=%s", virtual, result)
        return result

    def get_album_by_uuid(self, uuid) -> Optional[Album]:
        spec = self._spec(uuid)
        if spec is None:
            return self._repo.get_album_by_uuid(self._as_uuid_str(uuid))
        cover = self._repo.get_cover_asset_by_scope(spec.scope)
        return Album(
            id=spec.sentinel_id,
            uuid=UUID(self._as_uuid_str(uuid)),
            title=spec.title,
            description=spec.description,
            cover=UUID(cover.uuid) if cover else None,
            type=0,
            created_at=_MODULE_STARTED_AT,
            modified_at=_MODULE_STARTED_AT,
        )

    def get_album_cover_by_uuid(self, uuid) -> Optional[AssetDTO]:
        spec = self._spec(uuid)
        if spec is None:
            return self._repo.get_album_cover_by_uuid(self._as_uuid_str(uuid))
        return self._repo.get_cover_asset_by_scope(spec.scope)

    def get_album_uuids_of_asset(self, asset_uuid: Union[UUID, str]) -> list[UUID]:
        """返回包含指定资产的全部可见相册 uuid。"""
        rows = self._repo.get_album_uuids_of_asset(self._as_uuid_str(asset_uuid))
        return [UUID(u) for u in (rows or [])]

    # ==================================================================
    # 资产 DTO / 元数据（P0 补全）
    # ==================================================================

    def get_asset_by_id(
        self, asset_id: int, *, include_deleted: bool = False
    ) -> Optional[AssetDTO]:
        """按整数主键取 AssetDTO。

        Args:
            asset_id: assets.id。
            include_deleted: Trash 场景传 True。
        """
        return self._repo.get_asset_by_id(
            int(asset_id), include_deleted=include_deleted
        )

    def get_asset_by_uuid(
        self, asset_uuid: Union[UUID, str], *, include_deleted: bool = False
    ) -> Optional[AssetDTO]:
        """按 uuid 取 AssetDTO。"""
        return self._repo.get_asset_by_uuid(
            self._as_uuid_str(asset_uuid), include_deleted=include_deleted
        )

    def list_asset_dtos_by_album(
        self,
        album_uuid: Union[UUID, str],
        *,
        offset: int = 0,
        limit: int = 100,
    ) -> list[AssetDTO]:
        """列出相册内资产 DTO。

        - 物理相册：分页，走 repo.list_asset_dtos_by_album。
        - 虚拟相册：忽略 offset/limit，直接返回 scope 全量（分页由上层做）。
        """
        spec = self._spec(album_uuid)
        if spec is not None:
            return self._repo.list_asset_dtos_by_scope(spec.scope) or []
        return self._repo.list_asset_dtos_by_album(
            self._as_uuid_str(album_uuid), offset=offset, limit=limit
        ) or []

    def list_asset_dtos_by_scope(self, scope: str) -> list[AssetDTO]:
        """按 scope（active / deleted）列出全部资产 DTO。"""
        return self._repo.list_asset_dtos_by_scope(scope) or []

    def get_asset_metadata(
        self, asset_uuid: Union[UUID, str], *, include_deleted: bool = False
    ) -> Optional[dict]:
        """返回资产元数据浅层 dict（等同 AssetDTO.to_dict()）。"""
        return self._repo.get_asset_metadata(
            self._as_uuid_str(asset_uuid), include_deleted=include_deleted
        )

    # ==================================================================
    # 磁盘完整路径（P0 Trash 支持）
    # ==================================================================

    @overload
    def get_asset_full_path(
        self, asset: AssetDTO, *, include_deleted: bool = ...
    ) -> Optional[Path]:
        ...

    @overload
    def get_asset_full_path(
        self, asset: Union[UUID, str], *, include_deleted: bool = ...
    ) -> Optional[Path]:
        ...

    def get_asset_full_path(
        self,
        asset: Union[UUID, str, AssetDTO],
        *,
        include_deleted: bool = False,
    ) -> Optional[Path]:
        """根据资产（DTO 或 UUID）获取磁盘上的完整路径。

        Args:
            asset: 资产 UUID（UUID 对象或字符串）或 AssetDTO 实例。
            include_deleted: 为 True 时连同软删资产一起返回路径，
                供 Trash（回收站）中打开原图使用。

        Returns:
            完整路径 Path；无法定位时返回 None。
        """
        if isinstance(asset, AssetDTO):
            asset_uuid = getattr(asset, "uuid", None)
            if not asset_uuid:
                return None
            return self._repo.get_asset_full_path(
                str(asset_uuid), include_deleted=include_deleted
            )
        return self._repo.get_asset_full_path(
            self._as_uuid_str(asset), include_deleted=include_deleted
        )

    def get_asset_full_path_by_id(
        self, asset_id: int, *, include_deleted: bool = False
    ) -> Optional[Path]:
        """按 asset 整数主键取磁盘完整路径。

        供 ThumbnailGridPresenter 的 item_activated 消费；
        Trash 场景由调用方按 should_include_deleted() 决定是否传 True。
        """
        return self._repo.get_asset_full_path_by_id(
            int(asset_id), include_deleted=include_deleted
        )

    # ==================================================================
    # 封面路径
    # ==================================================================

    @overload
    def get_cover_full_path(
        self, album: Album, *, include_deleted: bool = ...
    ) -> Optional[Path]:
        ...

    @overload
    def get_cover_full_path(
        self, album: Union[UUID, str], *, include_deleted: bool = ...
    ) -> Optional[Path]:
        ...

    def get_cover_full_path(
        self,
        album: Union[UUID, str, Album],
        *,
        include_deleted: bool = False,
    ) -> Optional[Path]:
        """根据相簿（DTO 或 UUID）获取其封面资产的完整磁盘路径。

        组合流程：
            相簿 → 封面 AssetDTO → 资产 UUID → 完整路径

        P1 修复：
            此前当 ``album.cover`` 为空时，误将 ``album.uuid`` 当作
            asset uuid 去查路径。现在回退到按 album uuid 查其封面资产。
        """
        if isinstance(album, Album):
            cover_uuid = getattr(album, "cover", None)
            if cover_uuid:
                return self._repo.get_asset_full_path(
                    str(cover_uuid), include_deleted=include_deleted
                )
            # 回退：按相册 uuid 查其封面资产（此前是错误地用 album.uuid 当 asset uuid）
            return self._get_cover_path_by_album_uuid(
                album.uuid, include_deleted=include_deleted
            )
        return self._get_cover_path_by_album_uuid(
            album, include_deleted=include_deleted
        )

    def _get_cover_path_by_album_uuid(
        self,
        album_uuid: Union[UUID, str],
        *,
        include_deleted: bool = False,
    ) -> Optional[Path]:
        cover = self._repo.get_album_cover_by_uuid(self._as_uuid_str(album_uuid))
        if cover is None:
            return None
        cover_uuid = getattr(cover, "uuid", None)
        if not cover_uuid:
            return None
        return self._repo.get_asset_full_path(
            str(cover_uuid), include_deleted=include_deleted
        )

    # ==================================================================
    # 供 ThumbnailGridPresenter 使用
    # ==================================================================

    def get_asset_ids(self, album: Union[Album, UUID, str]) -> list[int]:
        """按相册列出可见的 asset 整数 id。

        排序契约（P1）：
            虚拟相册走 repo 的 scope 排序（active / deleted）；
            物理相册走 repo 的相册内排序。两者在 repo 层已同源
            （见 ViewRepository._ASSET_ORDER_SQL），本方法不再干预排序。
            返回 [] 表示无 asset。

        Args:
            album: Album DTO、UUID 或 uuid 字符串。
        """
        uuid = album.uuid if isinstance(album, Album) else album
        spec = self._spec(uuid)
        if spec is None:
            return self._repo.list_asset_ids_by_album(
                self._as_uuid_str(uuid)
            ) or []
        return self._repo.list_asset_ids_by_scope(spec.scope) or []

    # ==================================================================
    # 杂项
    # ==================================================================

    def __str__(self) -> str:
        return "\n".join((
            f"{type(self).__name__} (",
            "\t_VIRTUAL: [",
            *[f"\t\t{i}," for i in self._VIRTUAL],
            "\t]",
            ")"
        ))

    # alias
    get_asset_full_path_by_uuid = get_asset_full_path
    get_album_cover_full_path = get_cover_full_path
    get_cover_full_path_by_album_uuid = get_cover_full_path
    get_asset_full_path_by_asset_id = get_asset_full_path_by_id
