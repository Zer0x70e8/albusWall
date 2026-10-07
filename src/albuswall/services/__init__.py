#
""""""
from datetime import datetime
from typing import TypedDict, Dict, Callable, Any

from albuswall.core import Container, Application
from albuswall.utils.registry import register_all, build_getters
from albuswall.utils.signal import Signal

from .source import SourceService
from .trigger_sync import TriggerSyncService
from .thumbnail import ThumbnailService
from .view import ViewService
from .import_ import ImportService
from .trash import TrashService, utcnow, TrashCleanupScheduler
from .source_deletion import SourceTrashService


# ---------- 类型表：唯一事实来源 ----------
class Services(TypedDict):
    source_service: SourceService
    trigger_sync_service: TriggerSyncService
    thumbnail_service: ThumbnailService
    view_service: ViewService
    import_service: ImportService
    trash_service: TrashService  # 被动服务
    trash_cleanup_scheduler: TrashCleanupScheduler
    source_trash_service: SourceTrashService


# ---------- 构建表：声明每个服务"怎么造" ----------
# 签名统一为 (cls, container) -> instance，与 register.arg_map 对齐
_ARG_MAP: Dict[str, Callable[[Any, Container], Any]] = {
    "source_service": lambda cls, c: cls(
        c.get("ingest_source_repo"),
        c.get("import_repo"),
        c.get("task_service"),
    ),
    "trigger_sync_service": lambda cls, c: cls(
        c.get("ingest_source_repo"),
        c.get("trigger_facade"),  # ← 从容器拿
        source_added=c.get("source_service").source_added,
        source_updated=c.get("source_service").source_updated,
        source_removed=c.get("source_service").source_removed,
    ),
    "thumbnail_service": lambda cls, c: cls(
        c.get("task_service"),
        c.get("thumbnail_repo"),
        c.get("ingest_source_repo"),
    ),
    "view_service": lambda cls, c: cls(
        c.get("view_repo")
    ),
    "import_service": lambda cls, c: cls(
        c.get("import_repo"),
        c.get("task_service")
    ),
    "trash_service": lambda cls, c: cls(
        c.get("trash_repo"),
        c.get("trash_state_repo"),
        c.get("trash_clock"),
    ),
    "trash_cleanup_scheduler": lambda cls, c: cls(
        c.get("trash_service"),
        c.get("task_service"),
    ),
    "source_trash_service": lambda cls, c: cls(
        source_repo=c.get("source_repository"),
        thumbnail_repo=c.get("thumbnail_repository"),
        task_service=c.get("task_service"),
        reconciler=c.get("reconciler"),
    ),
}


def _default_factory(type_, _):
    """未在 _ARG_MAP 中声明的服务走无参构造（如 TaskService）。"""
    return type_()


def register_service(container: Container):
    container.register(
        "trigger_refresh_signal",
        lambda: Signal(name="TriggerRefreshRequested"),
        returns=Signal,
    )
    # 默认时钟：返回函数本身（不是调用结果），由 TrashService 决定何时调用。
    container.register(
        "trash_clock",
        lambda: utcnow,
        returns=Callable[[], datetime],
    )

    register_all(
        build_getters(
            container,
            Services.__annotations__,
            arg_map=_ARG_MAP,
            default_factory=_default_factory
        ),
        container.reg,
    )

    # ── 连接：扫描完成 → 唤醒导入 worker ────────────────
    def _wire_scan_to_import():
        source_service = container.get("source_service")
        import_service = container.get("import_service")
        # scan_finished payload 是 SourceScanFinished，trigger() 不接受参数，
        # 用 lambda 吞掉 payload。
        source_service.scan_finished.connect(
            lambda _result: import_service.trigger()
        )

    # ── 连接：导入完成 → 触发缩略图补齐 ────────────────
    def _wire_import_to_thumb():
        import_service = container.get("import_service")
        thumbnail_service = container.get("thumbnail_service")
        # assets_imported payload 是 (sender, **totals)，scan_and_submit 无参。
        import_service.assets_imported.connect(
            lambda *a, **kw: thumbnail_service.scan_and_submit()
        )

    # ── on_boot：按"依赖在前"的顺序注册 ────────────────
    Application.on_boot(_wire_scan_to_import)  # ← container → Application
    Application.on_boot(_wire_import_to_thumb)
    Application.on_boot(lambda: container.get("trigger_refresh_signal").connect(
        container.get("source_service").update_source,
    ))
    Application.on_boot(lambda: container.get("trigger_sync_service").start())
    Application.on_boot(lambda: container.get("source_service").start())
    Application.on_boot(lambda: container.get("import_service").start())
    Application.on_boot(lambda: container.get("thumbnail_service").start())
    Application.on_boot(lambda: container.get("trash_cleanup_scheduler").start())

    # ── on_final：注册顺序 = 拆除顺序.reversed ──────────────────
    Application.on_final(lambda: container.get("trash_cleanup_scheduler").stop(wait=True))
    Application.on_final(lambda: container.get("thumbnail_service").stop(wait=True))
    Application.on_final(lambda: container.get("import_service").stop_worker())
    Application.on_final(lambda: container.get("trigger_sync_service").stop())
    Application.on_final(lambda: container.get("source_service").shutdown())
