#
"""服务装配层。

本模块是**唯一**知道"哪些服务存在、彼此怎么接线"的地方。它做三件事：

1. 声明 ``Services`` 类型表（容器 key → 服务类型），交给
   :func:`~albuswall.utils.registry.build_getters` 懒构造；
2. 把**信号拓扑**接起来（源变更 → 状态层 / 触发器桥接层；扫描完成 →
   导入 worker；**访问到资产 → 按需生成缩略图**）；
3. 按"依赖在前"的顺序登记 ``on_boot``，按拆除顺序登记 ``on_exit``。

退出钩子的真实名字
------------------
``albuswall.core.Application``（``core/application.py``）只提供四个钩子：

``on_boot`` / ``on_init`` / ``on_loop`` / ``on_exit``。
**没有 ``on_final``** —— 规格书里的 ``on_final`` 是旧包的名字，本仓库不存在；
``CoreApplication.finally_()`` 里跑的就是 ``hooks_map["exit"]``，即
``on_exit`` 登记的那批钩子。因此本模块全部用 ``on_exit``，且
``run_hooks`` 按**登记顺序**执行（不反转），拆除顺序 = 登记顺序。

迁移期标注（UI 侧需改订阅方式）
-------------------------------
``TrackingState.observe`` 不再与旧 ``configue.state.ObservableNamespace``
同签名（旧回调是 ``cb(node, key)`` 且返回 unsubscribe 闭包，新回调是
``cb(state)`` 且用 ``unobserve`` 退订）；细节见
:class:`albuswall.service.source.state.tracking.TrackingState` 的 docstring。
**任何 UI 代码若订阅过旧 ``SourceServiceState``，必须改成按新签名订阅**
（目前仓库内没有这样的订阅方）。

业务域一览（每个包的 owner 表见各自 ``__init__`` 的 docstring）
-------------------------------------------------------------
===========================  ==========================================
包                           职责
===========================  ==========================================
``service/source/``          ``ingest_source``：五层（command / runtime /
                             state / trigger / trash）
``service/importer/``        ``asset_candidate_cache`` → ``assets``
``service/thumbnail/``       ``assets.thumb_*``：**按需**生成 + 孤儿清理
``service/view/``            只读门面（跨 assets / albums / album_assets）
``service/asset/``           写：收藏 / 软删 / 相册成员 / 资产↔标签；
                             资产级回收站（30 天保留期 + 定时清理）
``service/album/``           ``albums`` 生命周期
``service/tag/``             ``tags`` 树 + ``tag_aliases``（路径跳转）
===========================  ==========================================

包与包之间**没有** import 关系：它们只在装配层通过信号相遇 ——

::

    SourceRuntime.scan_finished  ─→ ImportService.trigger
    SourceService.source_*       ─→ SourceStateService / SourceTriggerService
    SourceTrashService.sources_changed ─→ State + Trigger（按 kind 分发）
    ViewService.assets_viewed    ─→ ThumbnailService.request_bulk（按需生成）

缩略图**没有**自动回填：导入完成后不会扫全库补图，"看到哪页生成哪页"。
"""

from collections.abc import Callable
from typing import Any, TypedDict

from albuswall.core import Application, Container
from albuswall.log import getLogger
from albuswall.utils.registry import build_getters, register_all
from albuswall.utils.signals import Signal

from .album import AlbumService
from .asset import (
    AssetOpsService,
    AssetTagService,
    TrashCleanupScheduler,
    TrashService,
)
from .command import CommandService, IpcRequestService
from .command.dispatch_errors import translate_service_error
from .importer import ImportService
from .main_loop_service import MainLoopService
from .source import (
    SourceRuntime,
    SourceService,
    SourceStateService,
    SourceTrashService,
    SourceTriggerService,
)
from .tag import AliasService, TagService
from .thumbnail import (
    ThumbnailCleaner,
    ThumbnailService,
    install_dirty_recovery,
    load_cleaner_config,
)
from .view import ViewService

__all__ = ["Services", "register_service"]

logger = getLogger(__name__)


class Services(TypedDict):
    # ---- 主线程调度（服务层写入口的线程边界，见 main_loop_service）----
    main_loop_service: MainLoopService
    command_service: CommandService
    ipc_request_service: IpcRequestService
    # ---- 导入 worker（``service/importer/``）----
    import_service: ImportService
    # ---- 导入源域（五层拆分，见 albuswall.service.source）----
    source_service: SourceService                 # 被动写命令
    source_runtime: SourceRuntime                 # 常驻任务
    source_state_service: SourceStateService      # 运行时数据
    source_trigger_service: SourceTriggerService  # 触发器桥接
    source_trash_service: SourceTrashService      # 回收站（软删 / 恢复 / 异步 purge）
    # ---- 缩略图域（按需生成，见 service/thumbnail/service.py）----
    thumbnail_service: ThumbnailService           # 渲染 worker（只按需排任务）
    thumbnail_cleaner: ThumbnailCleaner           # 孤儿 PNG 清理（脏退出后跑）
    # ---- 资产域（读写分离见 service/asset/__init__.py）----
    view_service: ViewService                     # 只读门面（相簿 / 资产查询）
    asset_ops_service: AssetOpsService            # 写：收藏 / 软删 / 相册成员
    asset_tag_service: AssetTagService            # 写：资产↔标签绑定
    trash_service: TrashService                   # 资产回收站（保留期清理）
    trash_cleanup_scheduler: TrashCleanupScheduler  # 定时投递 cleanup_expired
    # ---- 相簿 / 标签 ----
    album_service: AlbumService                   # 相簿生命周期（albums 写）
    tag_service: TagService                       # 标签树生命周期（tags 写）
    alias_service: AliasService                   # 标签别名（tag_aliases 写）


_ARG_MAP: dict[str, Callable[[Any, Container], Any]] = {
    # 主线程调度器：``main_loop`` 由 ``main.py`` 的 phase 4 放进容器（与
    # ``loop.run()`` 同一个对象）。这里用 ``get`` 而不是 ``require``：容器里
    # 没有主循环（单测直接调 ``register_service``、装配早期）是**合法状态**，
    # 此时 ``MainLoopService`` 退化为同步执行，不该在启动期抛
    # ``MissingDependencyError``。见 tests/test_source_wiring.py 的用法。
    "main_loop_service": lambda t, c: t(c.get("main_loop")),
    # 从容器取，而不是直接 get_default_registry()：两者当前是同一个对象
    # （command/__init__.py 的 registry_command_registry 就是把该单例注册成
    # "command_registry"），但从容器取让测试能用 register("command_registry",
    # ...) 覆盖它而不污染全局单例；缺 key 时构造期抛 MissingDependencyError
    # （KeyError 子类），启动期即炸。
    # 异常翻译器把服务层异常（ServiceError 家族）映射成带 code/exit_code 的
    # DispatchError，见 service/command/dispatch_errors.py。
    "command_service": lambda cls, c: cls(
        c.require("command_registry"),
        exception_translator=translate_service_error,
    ),
    # IPC 适配器：只依赖 ipc manager 和命令服务本身。
    "ipc_request_service": lambda cls, c: cls(
        c.get("ipc_service"),
        c.get("command_service"),
    ),
    # 导入 worker：``ImportService(repo, task, source_repo)``。
    # 三个 key 都由 main.py 的 phase 6/7 注册；``source_repo`` 用于每轮构建
    # file_type_check 快照（缺它也不致命，只是全部降级为 SUFFIX）。
    "import_service": lambda t, c: t(
        c.get("import_repo"),
        c.get("task_service"),
        c.get("ingest_source_repo"),
    ),
    # ---- 导入源域 ----------------------------------------------------
    # 依赖方向即构造顺序：command 无依赖 → runtime 依赖 repo/task →
    # state 依赖 runtime + 三个命令信号 → trigger 依赖 repo/facade + 同三个信号。
    #
    # ``main_loop`` 注入给两个**写入口**服务（command / state）：它们的 public
    # 方法要保证在主循环线程执行（TRACKING 的属主线程约定 + Signal 同步派发），
    # 详见 ``main_loop_service`` 与两个服务类 docstring 的"线程边界"段落。
    "source_service": lambda t, c: t(
        c.get("ingest_source_repo"),
        main_loop=c.require("main_loop_service"),
    ),
    "source_runtime": lambda t, c: t(
        c.get("import_repo"),
        c.get("task_service"),
        c.get("ingest_source_repo"),
        # 运行控制命令（force_scan / stop_scan）与 status() 会从 IPC 线程进来，
        # 而任务表只允许主线程改 —— 边界由 runtime 自己收口。
        main_loop=c.require("main_loop_service"),
    ),
    "source_state_service": lambda t, c: t(
        c.get("ingest_source_repo"), c.get("source_runtime"),
        main_loop=c.require("main_loop_service"),
        source_created=c.get("source_service").source_created,
        source_changed=c.get("source_service").source_changed,
        source_deleted=c.get("source_service").source_deleted,
        trash_changed=c.get("source_trash_service").sources_changed,
    ),
    "source_trigger_service": lambda t, c: t(
        c.get("ingest_source_repo"), c.get("trigger_facade"),
        source_created=c.get("source_service").source_created,
        source_changed=c.get("source_service").source_changed,
        source_deleted=c.get("source_service").source_deleted,
    ),
    # 回收站：唯一持有 Reconciler 的服务。构造即注册对账条目
    # （``on_startup=True`` —— 上次异常退出时软删了但没 purge 的源，靠
    # Reconciler.start() 那一刻补齐），所以它必须在 on_boot 就被构造出来，
    # 也就是**早于** on_loop 里的 ``reconciler.start()``。
    #
    # 依赖里 ``reconciler`` 用 ``get``：infrastructure bootstrap 注册了它，
    # 但只有在有人取用时才实例化（没人取用 = 不会有对账条目 = 不需要启动）。
    # 本服务是它唯一的取用方，「取用即实例化」正好让 on_loop 的
    # ``has_instance`` 判断为真。
    "source_trash_service": lambda t, c: t(
        c.get("ingest_source_repo"),
        c.get("thumbnail_repo"),
        c.get("reconciler"),
        main_loop=c.require("main_loop_service"),
    ),
    # ---- 缩略图域 ----------------------------------------------------
    # 只注入 worker 需要的东西：按需请求由装配层的信号驱动，本服务不认识
    # ViewService，也不认识 UI。
    "thumbnail_service": lambda t, c: t(
        c.get("task_service"),
        c.get("thumbnail_repo"),
        c.get("ingest_source_repo"),
    ),
    # 清理器只需要一份算好 root 的配置（root 与 ThumbnailService 严格同源）。
    # 用工厂 lambda 而不是默认工厂：默认工厂会传 ``db`` 进去。
    "thumbnail_cleaner": lambda t, c: t(load_cleaner_config()),
    # ---- 资产域 ------------------------------------------------------
    "view_service": lambda t, c: t(c.get("view_repo")),
    "asset_tag_service": lambda t, c: t(
        c.get("asset_repo"),
        c.get("tag_repo"),
        c.get("view_repo"),
    ),
    # 回收站：clock 走默认 ``utcnow``（测试可构造时自己注入）。
    "trash_service": lambda t, c: t(
        c.get("trash_repo"),
        c.get("trash_state_repo"),
    ),
    "trash_cleanup_scheduler": lambda t, c: t(
        c.get("trash_service"),
        c.get("task_service"),
    ),
    # 构造顺序：asset_ops 依赖 trash_service（恢复 / 清空）与 asset_tag_service
    # （将来批量打标签用）。两者都在上面，容器懒构造保证顺序成立。
    "asset_ops_service": lambda t, c: t(
        c.get("asset_repo"),
        c.get("view_repo"),
        c.get("trash_service"),
        c.get("asset_tag_service"),
    ),
    # ---- 相簿 / 标签 ----
    "album_service": lambda t, c: t(
        c.get("album_repo"),
        c.get("view_repo"),
    ),
    "tag_service": lambda t, c: t(c.get("tag_repo")),
    "alias_service": lambda t, c: t(c.get("tag_repo")),
}


def _dispatch_trash_change(container: Container) -> Callable[..., None]:
    """构造 ``SourceTrashService.sources_changed`` 的分发器。

    契约：``cb(sender: SourceTrashService, *, source_id: int, kind: str)``。

    分发规则（见规格书 §5）：

    * ``soft_delete`` / ``restore`` → 状态层重评估 **+** 触发器桥接层
      ``remove`` / ``upsert``。两者都是 UI 写路径（主线程），符合 state 的
      属主约定；
    * ``purged`` → **忽略**。它是软删的异步续集，跑在 reconciler 的工作
      线程上；此时状态层早已把该源踢出跟踪集，且绝不能在非属主线程写 state。

    状态层**不区分** ``soft_delete`` / ``restore``：它以 DB 当前状态为准
    （软删后 ``get_sync_candidate`` 返回 ``None``，恢复后重新可见），
    分支差异全在仓储查询里。
    """
    def _dispatch(
            _sender: object,
            *,
            source_id: int,
            kind: str,
    ) -> None:
        # purged 在 reconciler 工作线程 emit，禁止碰 state。
        if kind == "purged":
            return

        # soft_delete / restore 都跑在 UI 写路径（主线程）：状态层以 DB 当前
        # 状态为准重评估，触发器桥接层按 kind 补一次 upsert / remove。
        # payload 契约与真实信号一致：全部走关键字
        # （``cb(sender, *, source_id: int, kind: str)``），本函数的签名
        # ``(*, source_id, kind)`` 就是这条契约的强制点。
        container.get("source_state_service").refresh_source(source_id)

        trigger = container.get("source_trigger_service")
        if kind == "restore":
            trigger.on_source_upserted(source_id)
        else:
            # soft_delete（以及未来新增的"不可见"类事件）：先撤触发器。
            trigger.on_source_removed(source_id)

    return _dispatch


def _wire_scan_to_import(container: Container) -> None:
    """扫描完成 → 唤醒导入 worker。

    契约：``cb(sender: SourceExecutor, *, event: SourceScanFinished)``。
    ``ImportService.trigger()`` 不接受参数，用 ``lambda`` 把 sender 与
    payload 一起吞掉（``**_kw``），payload 只用于日志 / UI，不是导入的输入 ——
    导入 worker 永远自己从候选缓存表拉 pending，避免"信号里带数据"造成
    重放与漏扫。
    """
    # 契约：``cb(sender, *, event=...)``；trigger() 不收参数。
    container.get("source_runtime").scan_finished.connect(
        lambda _sender, **_kw: container.get("import_service").trigger()
    )


def _wire_trigger_refresh(container: Container) -> None:
    """触发器到点 → 单源重扫。

    契约：``cb(sender, *, source_id: int)``。出站链路固定走
    ``trigger_refresh_signal``（由 ``infrastructure/trigger`` 的 facade
    ``update_callback`` 发射），本层只负责把它接到状态层。
    """
    container.get("trigger_refresh_signal").connect(
        lambda _sender, *, source_id: (
            container.get("source_state_service").refresh_source(source_id)
        )
    )


def _wire_view_to_thumbnail(container: Container) -> None:
    """访问到资产 → 按需请求缩略图。

    契约：``cb(sender: ViewService, *, asset_uuids: tuple[UUID, ...])``。

    **这是缩略图唯一的自动入口**：导入完成后没有任何批量回填，因此
    "打开应用不干活，翻到哪页才生成哪页"。``request_bulk`` 自己有上限
    （``batch_size``）、有去重（``_inflight``）、有冷却（永久失败），所以这里
    只做"转发"，不在这里做任何过滤 —— 过滤规则归服务，装配层不做业务判断。

    ``include_deleted=True``：回收站里的资产也要预览缩略图；对活跃资产无影响。
    """
    container.get("view_service").assets_viewed.connect(
        lambda _sender, *, asset_uuids: container.get(
            "thumbnail_service"
        ).request_bulk(asset_uuids, include_deleted=True)
    )


def _wire_source_trash(container: Container) -> None:
    """回收站（软删 / 恢复 / 硬删）→ 状态层 + 触发器桥接层。

    接的是 ``SourceTrashService.sources_changed``（不再是占位信号）：占位信号
    的用途是「服务还没迁出、通道先留好」，服务已迁入 ``service/source/trash``，
    占位物随之删除 —— 留着两个信号只会让"到底谁在发"变得含糊。
    """
    container.get("source_trash_service").sources_changed.connect(
        _dispatch_trash_change(container)
    )


def register_service(container: Container):
    """注册全部服务并接线。

    容器里必须已有：``command_registry`` / ``ipc_service`` /
    ``ingest_source_repo`` / ``import_repo`` / ``task_service`` /
    ``trigger_facade``（由 ``main.py`` 的 phase 6 注册）。
    """
    # 触发器到点信号：出站链路的公共落点。既是基础设施（facade 发射它）
    # 与业务层（状态层订阅它）之间唯一的耦合点，就必须有人先把它注册好。
    container.register(
        "trigger_refresh_signal",
        lambda: Signal(name="TriggerRefreshRequested"),
        returns=Signal,
    )
    # 回收站变更**不再**有占位信号：它已经是真实服务
    # （``service/source/trash``）的 ``sources_changed``，见 ``_wire_source_trash``。

    register_all(
        build_getters(
            container,
            Services.__annotations__,
            arg_map=_ARG_MAP
        ),
        container.reg,
    )

    # ── 接线：信号拓扑 ────────────────────────────────
    Application.on_boot(lambda: _wire_scan_to_import(container))
    Application.on_boot(lambda: _wire_trigger_refresh(container))
    Application.on_boot(lambda: _wire_source_trash(container))
    Application.on_boot(lambda: _wire_view_to_thumbnail(container))

    # ── on_boot：按"依赖在前"的顺序注册 ────────────────
    # 1. runtime 先起：state.start() 会立刻产出第一份对账计划，宿主必须
    #    已经能接（reconcile 对未 started 的 runtime 是 no-op，会把首轮
    #    扫描静默吞掉）。
    Application.on_boot(lambda: container.get("source_runtime").start())
    # 2. import worker 必须早于 state：state 首轮 reconcile 就会提交扫描
    #    任务，任务产出的候选需要有人消费。
    Application.on_boot(lambda: container.get("import_service").start())
    # 3. state 再全量装载 + 建首轮任务。
    Application.on_boot(lambda: container.get("source_state_service").start())
    # 4. trigger 最后起：它全量读一次 trigger_config 交给 facade。
    Application.on_boot(
        lambda: container.get("source_trigger_service").start()
    )
    # 5. 回收站：构造即向 Reconciler 注册对账条目（on_startup=True），
    #    因此必须**早于** on_loop 里的 ``reconciler.start()`` —— 那一步才会
    #    补交「上次异常退出时没删干净」的 pending purge。它自己没有常驻线程，
    #    所以不需要 on_exit（生命周期归 Reconciler 与 TaskService 管）。
    Application.on_boot(
        lambda: container.get("source_trash_service")
    )
    # 6. 缩略图 worker：**不**在启动时回填任何东西（按需生成，见
    #    ``service/thumbnail/service.py``）；这里只是把根目录 / specs 打出来，
    #    并让 ``stop()`` 有东西可等。
    Application.on_boot(lambda: container.get("thumbnail_service").start())
    # 7. 资产回收站的定时清理：首次 tick 有 30s 延迟（让启动期 IO 先完成），
    #    周期 1h，实际"清不清"由 TrashService 的水位判定。
    Application.on_boot(
        lambda: container.get("trash_cleanup_scheduler").start()
    )
    # 8. 会话脏标：上次非正常退出 → 开机跑一次孤儿缩略图清理。
    #    它自己登记 on_boot / on_exit（顺序在 boot 尾部：先读标记再置脏，
    #    且必须在其它服务已就绪之后才去扫缓存）。
    install_dirty_recovery(container)

    # ── on_exit：注册顺序 = 拆除顺序 ──────────────────
    # 先停"周期性投递"与"按需 worker"，再拆业务服务：这样拆除过程中不会
    # 又有新任务被提交进来（尤其是 scheduler 的 tick 线程）。
    #
    # 注意：``infrastructure.task`` 把自己的 ``shutdown()`` 登记得更早
    # （基础设施阶段在服务阶段之前），所以 TaskService 实际上**先于**这里
    # 的钩子拆除。收尾期的提交竞态由各投递方自己兜住：
    # ``TrashCleanupScheduler`` 收到 ``TaskServiceShutdownError`` 即视为终态
    # 并退出 tick 线程（见 ``service/asset/trash/scheduler.py``），
    # ``SourceRuntime`` 同样在首次失败后提前 return。
    Application.on_exit(
        lambda: container.get("trash_cleanup_scheduler").stop(wait=True)
    )
    Application.on_exit(
        lambda: container.get("thumbnail_service").stop(wait=True)
    )
    # 钩子名就是 ``on_exit``（``Application`` 没有 ``on_final``，见模块
    # docstring）；``run_hooks`` 按登记顺序执行（不反转），因此这里直接按
    # "先断上游、后拆下游"的顺序登记。
    Application.on_exit(
        lambda: container.get("source_trigger_service").stop()
    )
    Application.on_exit(
        lambda: container.get("source_state_service").shutdown()
    )
    # import worker 在 state 拆完、runtime 拆之前停：此时不会再有新的扫描
    # 完成信号进来，worker 也还有机会把手上那一轮 pending 处理完。
    Application.on_exit(
        lambda: container.get("import_service").stop_worker()
    )
    Application.on_exit(
        lambda: container.get("source_runtime").shutdown()
    )

    # 顺序有意义，别对调：``_assemble_commands`` 必须先跑完（含 freeze），
    # 再安装 IPC handler。ipc manager 的 accept 线程早在 on_init 就起来了
    # （infrastructure/ipc/manager.py 的 ``Application.on_init(_setup)``），
    # 客户端随时可能连进来；若先 ``start()``，就会出现「handler 已挂上、
    # 注册表还空」的窗口，窗口内 ``help`` 拿到的是 unknown_command（exit 8），
    # 而优雅的 not-ready 语义是 no_handler。
    # 交给插件，这样就可禁用命令服务了
    # Application.on_loop(lambda app: _assemble_commands(container))
    #
    # ``main_loop_service.start()`` 必须**早于** handler 挂上：命令一进来就会
    # 调到服务，服务的写入口要用它回主线程（这里同时记录主循环属主线程）。
    Application.on_loop(lambda app: container.get("main_loop_service").start())
    Application.on_loop(lambda app: container.get("ipc_request_service").start())
    Application.on_exit(lambda: container.get("ipc_request_service").shutdown())
    # 最后关闭调度器：先摘 handler（不再受理新请求），再取消在途调用 ——
    # 调用方立刻收到 dispatch_unavailable，而不是干等主线程超时。
    Application.on_exit(lambda: container.get("main_loop_service").close())
