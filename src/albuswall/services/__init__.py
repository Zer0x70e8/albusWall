#
""""""
from datetime import datetime
from typing import TypedDict, Dict, Callable, Any

from albuswall.log import getLogger
from albuswall.core import Container, Application
from albuswall.utils.registry import register_all, build_getters
from albuswall.utils.signals import Signal
from .asset_tag import AssetTagService

from .source import SourceService
from .tag import TagService
from .trigger_sync import TriggerSyncService
from .thumbnail import ThumbnailService
from .view import ViewService
from .import_ import ImportService
from .trash import TrashService, utcnow, TrashCleanupScheduler
from .source_trash import (
    SourceTrashService, ThumbnailCleaner,
    load_thumbnail_cleaner_config, load_source_trash_config,
    install_source_trash_lifecycle
)

logger = getLogger(__package__)


def on_source_trash_changed(
        source_service: SourceService,
        _sender,
        *,
        source_id: int,
        kind: str,
) -> None:
    """``SourceTrashService.sources_changed`` 的处理函数。

    * ``soft_delete`` / ``restore``：源行的可见性变了，让调度侧重新评估
      该源（软删 → ``get_sync_candidate`` 返回 ``None`` → 剔除跟踪记录与
      扫描任务；恢复 → 重新挂载 + 建任务）。这两个事件由 UI 在写路径上
      同步触发，跑在主线程，符合 state 的属主约定。
    * ``purged``：软删的异步续集，跑在 reconciler 的工作线程上。此时
      state 早已排斥该源，且绝不能在非属主线程写 state —— 直接忽略。
    """
    if kind == "purged":
        return
    source_service.update_source(source_id)


# ---------- 类型表：唯一事实来源 ----------
class Services(TypedDict):
    source_service: SourceService
    trigger_sync_service: TriggerSyncService
    thumbnail_service: ThumbnailService
    view_service: ViewService
    import_service: ImportService
    trash_service: TrashService  # 被动服务
    trash_cleanup_scheduler: TrashCleanupScheduler
    thumbnail_cleaner: ThumbnailCleaner
    source_trash_service: SourceTrashService
    tag_service: TagService
    asset_tag_service: AssetTagService


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
        c.get("task_service"),
        # 每轮读一次源配置，把 file_type_check 快照给进程池里的精判。
        c.get("ingest_source_repo"),
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
    "thumbnail_cleaner": lambda cls, c: cls(
        load_thumbnail_cleaner_config(),
    ),
    "source_trash_service": lambda cls, c: cls(
        c.get("ingest_source_repo"),
        c.get("thumbnail_repo"),
        c.get("reconciler"),
        load_source_trash_config(),
    ),
    "tag_service": lambda t, c: t(
        c.get("tag_repo"),
    ),
    "asset_tag_service": lambda t, c: t(
        c.get("asset_repo"),
        c.get("tag_repo"),
        c.get("view_repo"),
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

    install_source_trash_lifecycle(container)

    # ── 连接：app 扫描完成 → 唤醒导入 worker ────────────────
    def _wire_scan_to_import():
        source_service = container.get("source_service")
        import_service = container.get("import_service")
        # 契约：cb(sender, *, event: SourceScanFinished)；trigger() 不接受参数，
        # 用 lambda 吞掉 sender 与 payload。
        source_service.scan_finished.connect(
            lambda _sender, **_kwargs: import_service.trigger()
        )

    # ── 连接：导入完成 → 触发缩略图补齐 ────────────────
    def _wire_import_to_thumb():
        import_service = container.get("import_service")
        thumbnail_service = container.get("thumbnail_service")
        # 契约：cb(sender, *, ok, failed, skipped, retry, timeout)；
        # scan_and_submit 无参。
        import_service.assets_imported.connect(
            lambda _sender, **_totals: thumbnail_service.scan_and_submit()
        )

    # ── 连接：触发器到点 → 刷新单个源 ────────────────
    def _wire_trigger_refresh():
        source_service = container.get("source_service")
        # 契约：cb(sender, *, source_id: int)；update_source 是普通方法。
        container.get("trigger_refresh_signal").connect(
            lambda _sender, *, source_id: source_service.update_source(source_id)
        )

    # ── 连接：源回收站（软删 / 恢复）→ 同步 source_service 跟踪集 ────
    def _wire_source_trash_to_source():
        """回收站改了 ingest_source 的行，调度侧必须跟着改。

        没有这条连接时，软删的源会一直留在 ``SourceService.state`` 里：
        它的扫描任务仍是"已提交"快照，下一次任何写路径（例如新建源）都会
        把它重新提交一遍 —— 扫一个已经不存在 / 已被 purge 的 source_id，
        候选插入撞 ``FOREIGN KEY constraint failed``。
        """
        source_service = container.get("source_service")
        source_trash = container.get("source_trash_service")
        # 契约：cb(sender: SourceTrashService, *, source_id: int, kind: str)
        source_trash.sources_changed.connect(
            lambda sender, **kwargs: on_source_trash_changed(
                source_service, sender, **kwargs
            )
        )

    # ── on_boot：按"依赖在前"的顺序注册 ────────────────
    Application.on_boot(_wire_scan_to_import)  # ← container → Application
    Application.on_boot(_wire_import_to_thumb)
    Application.on_boot(_wire_trigger_refresh)
    Application.on_boot(_wire_source_trash_to_source)
    Application.on_boot(lambda: container.get("trigger_sync_service").start())
    Application.on_boot(lambda: container.get("source_service").start())
    Application.on_boot(lambda: container.get("import_service").start())
    Application.on_boot(lambda: container.get("thumbnail_service").start())
    Application.on_boot(lambda: container.get("trash_cleanup_scheduler").start())
    Application.on_boot(lambda: container.get("source_trash_service"))

    # ── on_final：注册顺序 = 拆除顺序.reversed ──────────────────
    Application.on_final(lambda: container.get("trash_cleanup_scheduler").stop(wait=True))
    Application.on_final(lambda: container.get("thumbnail_service").stop(wait=True))
    Application.on_final(lambda: container.get("import_service").stop_worker())
    Application.on_final(lambda: container.get("trigger_sync_service").stop())
    Application.on_final(lambda: container.get("source_service").shutdown())
