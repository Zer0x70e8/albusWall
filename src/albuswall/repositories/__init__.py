#
""""""

from typing import TYPE_CHECKING, Callable, Any, TypedDict

from albuswall.utils.registry import register_tool

from .source import IngestSourceRepository
from .import_ import ImportRepository
from .thumbnail import ThumbnailRepository
from .view import ViewRepository
from .asset import AssetRepository

if TYPE_CHECKING:
    from albuswall.core import Container


class Repositories(TypedDict):
    ingest_source_repo: IngestSourceRepository
    import_repo: ImportRepository
    thumbnail_repo: ThumbnailRepository
    view_repo: ViewRepository
    asset_repo: AssetRepository


# 只有「需要特殊参数」的仓库才写进来，其余走默认工厂
_ARG_MAP: dict[str, Callable[[Any, "Container"], Any]] = {
    # 例如:
    # "asset": lambda t, c: t(c.get("db"), c.get("fs")),
}


def registry_repository(container: "Container"):
    register_tool(
        container, Repositories.__annotations__, arg_map=_ARG_MAP
    )
