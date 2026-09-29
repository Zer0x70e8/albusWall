#
""""""

from typing import TypedDict, Dict, Callable, Any

from albuswall.core import Container
from albuswall.utils.repository_registry import register_repositories

from .source import SourceService
from .task import TaskService
from .trigger import TriggerService
from .import_ import ImportService
from .view import ViewService
from .thumbnail import ThumbnailService


# ---------- 类型表：唯一事实来源 ----------
class Services(TypedDict):
    source_service: SourceService
    task_service: TaskService
    trigger_service: TriggerService
    import_service: ImportService
    view_service: ViewService
    thumbnail_service: ThumbnailService


# ---------- 构建表：声明每个服务"怎么造" ----------
# 签名统一为 (cls, container) -> instance，与 register_repositories.arg_map 对齐
_ARG_MAP: Dict[str, Callable[[Any, Container], Any]] = {
    "source_service": lambda cls, c: cls(
        c.get("ingest_source_repo"),
        c.get("import_repo"),
        c.get("task_service"),
    ),
    "trigger_service": lambda cls, c: cls(
        c.get("ingest_source_repo"),
        # 回调函数：调用 SourceService 的 update_source 方法
        lambda source_id: c.get("source_service").update_source(source_id),
    ),
    "import_service": lambda cls, c: cls(
        c.get("import_repo"),
        c.get("task_service"),
    ),
    "view_service": lambda cls, c: cls(
        c.get("view_repo"),
    ),
    "thumbnail_service": lambda cls, c: cls(
        c.get("task_service"),
        c.get("thumbnail_repo"),
        c.get("ingest_source_repo"),
    ),
}


def _default_factory(type_, _):
    """未在 _ARG_MAP 中声明的服务走无参构造（如 TaskService）。"""
    return type_()


def register_service(container: Container):
    # 从类型表取出 {字段名: 类型}，作为构建表传给注册器
    services = dict(Services.__annotations__)

    register_repositories(
        container,
        services,
        arg_map=_ARG_MAP,
        default_factory=_default_factory,
    )

    # 启动回调 (添加顺序会间接决定启动顺序)
    container.boot(lambda: container.get("source_service").start())
    container.boot(lambda: container.get("task_service"))  # 只是 get 一下，确保构造
    container.boot(lambda: container.get("trigger_service").start())
    container.boot(lambda: container.get("import_service").start())
    container.boot(lambda: container.get("thumbnail_service").start())
    container.boot(
        lambda: container.get("source_service").scan_finished.connect(
            lambda _event: container.get("import_service").trigger()
        )
    )

    # 关闭回调（顺序和启动相反）
    container.final(lambda: container.get("trigger_service").stop())
    container.final(lambda: container.get("task_service").shutdown())
