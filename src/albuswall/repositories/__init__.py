#
""""""

from typing import TYPE_CHECKING, TypedDict, Dict, Callable, Any

from .source import IngestSourceRepository
from .import_ import ImportRepository
from .view import ViewRepository

if TYPE_CHECKING:
    from albuswall.core import Container


class Repositories(TypedDict):
    ingest_source_repo: IngestSourceRepository
    import_repo: ImportRepository
    view_repo: ViewRepository


# Mapping of required args initialization cls.
_ARG_MAP: Dict[str, Callable[[Any, "Container"], Any]] = {}


def registry_repository(container: "Container"):
    for name, type_ in Repositories.__annotations__.items():
        if name in _ARG_MAP:
            container.reg(name, lambda n_=name:
            _ARG_MAP[n_](type_, container))
        else:
            container.reg(name, lambda t_=type_: t_(container.get("db")))
