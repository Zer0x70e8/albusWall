#
""""""

from typing import TypedDict, Dict, Callable, Any

from albuswall.core import Container

from .source import SourceService
from .task import TaskService
from .trigger import TriggerService
from .import_ import ImportService
from .view import ViewService
from .thumbnail import ThumbnailService


class Services(TypedDict):
    source_service: SourceService
    task_service: TaskService
    trigger_service: TriggerService
    import_service: ImportService
    view_service: ViewService
    thumbnail_service: ThumbnailService


# Mapping of required args initialization cls.
_ARG_MAP: Dict[str, Callable[[Any, Container], Any]] = {
    "source_service": lambda cls, c: cls(
        c.get("ingest_source_repo"),
        c.get("import_repo"),
        c.get("task_service")
    ),
    "trigger_service": lambda cls, c: cls(
        c.get("ingest_source_repo"),
        # 回调函数：调用 SourceService 的 update_source 方法
        lambda source_id: c.get("source_service").update_source(source_id)
    ),
    "import_service": lambda cls, c: cls(
        c.get("import_repo"),
        c.get("task_service")
    ),
    "view_service": lambda cls, c: cls(
        c.get("view_repo"),
    ),
    "thumbnail_service": lambda cls, c: cls(
        c.get("task_service"),
        c.get("thumbnail_repo"),
        c.get("ingest_source_repo"),
    )
}


def _check(name, expected, value):
    if not isinstance(value, expected):
        raise TypeError(
            f"service '{name}' 期望 {expected.__name__}，实得 {type(value).__name__}"
        )


def register_service(container: Container):
    for name, service_type in Services.__annotations__.items():
        if name in _ARG_MAP:
            container.reg(
                name,
                lambda n_=name, st=service_type: _ARG_MAP[n_](st, container),
                    # _check(n_, st, _ARG_MAP[n_](st, container)))
                returns=service_type
            )
        else:
            container.reg(name, lambda t=service_type: t(), returns=service_type)

    # 启动回调
    container.on(lambda: container.get("source_service").start())
    container.on(lambda: container.get("task_service"))  # 只是 get 一下，确保构造
    container.on(lambda: container.get("trigger_service").start())
    container.on(lambda: container.get("import_service").start())
    container.on(lambda: container.get("thumbnail_service").start())

    # 关闭回调（顺序和启动相反更安全）
    # container.final(lambda: container.get("import_service").stop())
    container.final(lambda: container.get("trigger_service").stop())
    # container.final(lambda: container.get("source_service").stop())
    container.final(lambda: container.get("task_service").shutdown)
