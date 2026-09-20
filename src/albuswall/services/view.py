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


class _VirtualAlbumSpec(NamedTuple):
    """虚拟相册的静态描述。"""
    title: str
    description: str
    scope: str
    sentinel_id: int


class ViewService:
    def __init__(self, repo: "ViewRepository"):
        self._repo = repo

    # ---------------- 唯一分派点 ----------------
    # 返回 None 表示物理相册；返回 (scope,) 表示虚拟相册
    _VIRTUAL: dict[str, _VirtualAlbumSpec] = {
        # uuid_str: _VirtualAlbumSpec(title, description, scope, sentinel_id)
        "00000000-0000-0000-0000-000000000001": _VirtualAlbumSpec(
            title="All",
            description="All active albums",
            scope="active",
            sentinel_id=-1,
        ),
        "00000000-0000-0000-0000-000000000002": _VirtualAlbumSpec(
            title="Trash",
            description="Deleted albums",
            scope="deleted",
            sentinel_id=-2,
        ),
    }

    def _spec(self, uuid) -> Optional[_VirtualAlbumSpec]:
        return self._VIRTUAL.get(self._as_uuid_str(uuid))

    @staticmethod
    def _as_uuid_str(value: UUID | str) -> str:
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

    def get_active_album_uuids(self) -> list[UUID]:
        virtual = [UUID(u) for u in self._VIRTUAL]
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

    @overload
    def get_asset_full_path(self, asset: AssetDTO) -> Optional[Path]:
        ...

    @overload
    def get_asset_full_path(self, asset: UUID | str) -> Optional[Path]:
        ...

    def get_asset_full_path(
            self, asset: Union[UUID, str, AssetDTO]
    ) -> Optional[Path]:
        """根据资产（DTO 或 UUID）获取磁盘上的完整路径。

        Args:
            asset: 资产 UUID（UUID 对象或字符串）或 AssetDTO 实例。

        Returns:
            完整路径 Path；若资产不存在、已软删除或无法定位，返回 None。
        """
        if isinstance(asset, AssetDTO):
            asset_uuid = getattr(asset, "uuid", None)
            if not asset_uuid:
                return None
            # noinspection string-conversion-without-dunder-method
            return self._repo.get_asset_full_path(str(asset_uuid))
        return self._repo.get_asset_full_path(self._as_uuid_str(asset))

    @overload
    def get_cover_full_path(self, album: Album) -> Optional[Path]:
        ...

    @overload
    def get_cover_full_path(self, album: UUID | str) -> Optional[Path]:
        ...

    def get_cover_full_path(
            self, album: Union[UUID, str, Album]
    ) -> Optional[Path]:
        """根据相簿（DTO 或 UUID）获取其封面资产的完整磁盘路径。

        组合流程：
            相簿 → 封面 AssetDTO → 资产 UUID → 完整路径

        Args:
            album: 相簿 UUID（UUID 对象或字符串）或 Album DTO 实例。

        Returns:
            封面资产的完整路径 Path；若相簿不存在、未设置封面、
            封面已被软删除或路径无法定位，返回 None。
        """
        if isinstance(album, Album):
            # Album DTO 的 cover 字段本身可能就是封面资产 uuid
            cover_uuid = getattr(album, "cover", None)
            if cover_uuid:
                # noinspection string-conversion-without-dunder-method
                return self._repo.get_asset_full_path(str(cover_uuid))
            # 若 DTO 未携带 cover，则回退到按 uuid 再查一次
            return self._repo.get_asset_full_path(
                self._as_uuid_str(album.uuid)
            ) if False else self._get_cover_path_by_album_uuid(album.uuid)

        return self._get_cover_path_by_album_uuid(album)

    def _get_cover_path_by_album_uuid(
            self, album_uuid: UUID | str
    ) -> Optional[Path]:
        cover = self._repo.get_album_cover_by_uuid(self._as_uuid_str(album_uuid))
        if cover is None:
            return None
        cover_uuid = getattr(cover, "uuid", None)
        if not cover_uuid:
            return None
        # noinspection string-conversion-without-dunder-method
        return self._repo.get_asset_full_path(str(cover_uuid))

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
