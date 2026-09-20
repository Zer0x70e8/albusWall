#
""""""

from typing import TypedDict, Dict, Callable, Any

from albuswall.core import Container

from .source import SourceService
from .task import TaskService
from .trigger import TriggerService
from .import_ import ImportService
from .view import ViewService


class Services(TypedDict):
    source_service: SourceService
    task_service: TaskService
    trigger_service: TriggerService
    import_service: ImportService
    view_service: ViewService


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
}


def _check(name, expected, value):
    if not isinstance(value, expected):
        raise TypeError(
            f"service '{name}' 期望 {expected.__name__}，实得 {type(value).__name__}"
        )


def register_service(container: Container):
    for name, service_type in Services.__annotations__.items():
        if name in _ARG_MAP:
            container.reg(name, lambda n_=name, st=service_type:
            # _check(n_, st, _ARG_MAP[n_](st, container)))
            _ARG_MAP[n_](st, container))
        else:
            container.reg(name, lambda t=service_type: t())

    container.on(lambda: container.get("source_service").start())
    container.on(lambda: container.get("trigger_service").start())
    container.on(lambda: container.get("import_service").start())

    container.final(lambda: container.get("trigger_service").stop())
