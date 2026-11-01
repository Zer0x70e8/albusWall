#
"""``SourceTrashService`` —— 源回收站：软删源 → 异步硬删 DB 行 + 缩略图。

职责边界
--------
**持有**：一个 ``Reconciler`` 条目（名字来自配置）、一份 ``SourceTrashConfig``。

**依赖**（全部构造注入，不读配置系统、不读环境变量、不算路径）：

* :class:`~albuswall.repositories.source.IngestSourceRepository` —— 软删 /
  恢复 / 硬删 / 启动补清理都靠它；
* :class:`~albuswall.repositories.thumbnail.ThumbnailRepository` —— purge 前
  快照缩略图路径（purge 之后 assets 行已不存在，再也查不到）；
* :class:`~albuswall.infrastructure.reconcile.service.Reconciler` —— 异步
  purge 的唯一调度器（debounce 合并 + 失败重试 + 启动补交）。

**不做**：不碰 ``SourceStateService`` / ``SourceRuntime`` / 触发器桥接层。
它只发一个信号 ``sources_changed``，谁订阅、怎么反应由装配层
（``service/__init__.py`` 的 ``_dispatch_trash_change``）决定 —— 与
``SourceService`` 的三信号是同一套约定。

为什么删除要经过回收站而不是直接硬删
------------------------------------
用户按「删除」的意图是「不要这个源了」，但误删是常态：源行 + 旗下资产行
一起进回收站还能「恢复」，而硬删之后资产行（含收藏 / 相册归属）不可逆。
因此对外语义固定为：

::

    delete_source  →  is_deleted=1（立刻从 UI / 跟踪集消失）+ 调度 purge
    restore_source →  is_deleted=0（purge 还没跑就能整份救回）
    purge          →  debounce 之后由 Reconciler 在 TaskService 的工作线程上跑

``purge`` 一旦跑完就真的没了（``IngestSourceRepository.purge`` 在一个事务里
删 assets 行 + source 行，``asset_candidate_cache`` 由外键 CASCADE 顺带清）。
源文件（用户磁盘上的照片）**任何一步都不动** —— 回收站删的是库记录。

线程边界
--------
``delete_source`` / ``restore_source`` 是**跨线程安全**的：整个动作（写库 +
``emit``）搬到主循环线程执行，与 ``SourceService`` 的写入口一致（``Signal``
同步派发，而状态层只允许属主线程写）。

``_purge_batch`` / ``_purge_all_pending`` 例外：它们跑在 TaskService 的
**工作线程**上，绝不能再往主循环搬（那会把主线程卡在对账任务后面）。它们
只写 DB + 发 ``kind="purged"``，而装配层的分发器对 ``purged`` 直接 return
（见 ``service/__init__.py::_dispatch_trash_change``）—— 这个分工是刻意的，
不要「顺手」让它去刷新状态层。

信号
----
``sources_changed``，契约 ``cb(sender, *, source_id: int, kind: str)``，
``kind`` ∈ ``{"soft_delete", "restore", "purged"}``。**必须是关键字**
（``Signal`` 位置参数 payload 会被拒，见 ``tests/test_source_wiring.py``）。
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import TYPE_CHECKING, Dict, Optional, TypeVar

from albuswall.dto.source.events import SourcePurgeReport
from albuswall.dto.source.query import MANUAL_SOURCE_ID
from albuswall.dto.thumbnail.paths import ThumbnailPaths
from albuswall.infrastructure.reconcile.service import Reconciler
from albuswall.log import Logger, getLogger
from albuswall.repositories.source import IngestSourceRepository
from albuswall.repositories.thumbnail import ThumbnailRepository
from albuswall.utils.signals import SignalDescriptor

from ..errors import SourceVirtualRoot
from .config import SourceTrashConfig, load_source_trash_config

if TYPE_CHECKING:  # 只为类型标注；运行时鸭子类型，避免装配顺序依赖
    from ...main_loop_service import MainLoopService

__all__ = ["SourceTrashService"]

_logger: Logger = getLogger(__name__)

T = TypeVar("T")


class SourceTrashService:
    """源级回收站。配置与协作者全部由构造参数注入。"""

    # 类属性 → 必须用 SignalDescriptor（lazy、实例间共享）。
    # 契约：cb(sender: SourceTrashService, *, source_id: int, kind: str)
    sources_changed = SignalDescriptor("sources_changed")

    def __init__(
            self,
            source_repo: IngestSourceRepository,
            thumbnail_repo: ThumbnailRepository,
            reconciler: Reconciler,
            config: Optional[SourceTrashConfig] = None,
            *,
            main_loop: "MainLoopService | None" = None,
            log: Optional[Logger] = None,
    ) -> None:
        self._sources = source_repo
        self._thumbs = thumbnail_repo
        self._reconciler = reconciler
        self._config = config if config is not None else load_source_trash_config()
        self._main_loop = main_loop
        self._log = log or _logger
        # 注册必须在**构造期**完成：``Reconciler.start()`` 跑在 on_loop，而
        # on_startup=True 的条目要在那一刻补齐「上次异常退出时没删干净的源」。
        # 若把注册挪到 start()（同样在 on_loop），与 Reconciler.start() 的
        # 先后顺序就取决于钩子登记顺序 —— 本服务会漏掉启动补清理。
        self._register_reconciler()

    # ================================================================
    # 线程边界 / 自省
    # ================================================================
    def _on_main(self, fn: Callable[[], T]) -> T:
        """把 ``fn`` 送到主循环线程执行（已在该线程时直接跑）。"""
        if self._main_loop is None:
            return fn()
        return self._main_loop.call(fn)

    def describe(self, *, verbose: bool = False) -> str:
        base = (
            f"SourceTrashService(reconciler={self._config.reconciler_name!r}, "
            f"debounce={self._config.debounce})"
        )
        if verbose:
            base = base[:-1] + (
                f", sources={type(self._sources).__name__}, "
                f"thumbs={type(self._thumbs).__name__}, "
                f"reconciler={type(self._reconciler).__name__})"
            )
        return base

    # ================================================================
    # 公共 API（UI / IPC 写路径）
    # ================================================================
    def delete_source(self, source_id: int) -> bool:
        """软删 + 调度异步 purge。

        返回 ``False`` 说明「已经处于删除态 / 该 id 不存在」——幂等，不是错误。

        Raises:
            SourceVirtualRoot: 目标是虚拟根（``MANUAL_SOURCE_ID``）。这里刻意
                抛**与 ``SourceService.delete`` 同一个异常类型**：调用方按类型
                区分「虚拟根」与「别的参数错误」，旧实现抛裸 ``ValueError``
                做不到这一点。
        """
        return self._on_main(lambda: self._delete_source(source_id))

    def restore_source(self, source_id: int) -> bool:
        """撤销软删。行已被 purge 掉时返回 ``False``（救不回来）。

        与 ``delete_source`` 一样跑在主循环线程：恢复会让源重新回到跟踪集，
        而跟踪集只允许属主线程写。
        """
        return self._on_main(lambda: self._restore_source(source_id))

    def purge_source_now(self, source_id: int) -> SourcePurgeReport:
        """同步硬删，跳过 debounce。

        仅供管理端点 / 测试：正常路径一律走 ``delete_source`` 的异步 purge。
        线程无关（可以在任何线程调用），因为它不碰状态层。
        """
        return self._purge_one(source_id)

    # ================================================================
    # 私有实现（主线程）
    # ================================================================
    def _delete_source(self, source_id: int) -> bool:
        if source_id == MANUAL_SOURCE_ID:
            raise SourceVirtualRoot(source_id, operation="soft delete")

        if not self._sources.soft_delete(source_id):
            return False

        self._log.info("source %d soft-deleted, scheduling purge", source_id)
        self._schedule(source_id)
        self.sources_changed.emit(self, source_id=source_id, kind="soft_delete")
        return True

    def _restore_source(self, source_id: int) -> bool:
        restored = self._sources.restore(source_id)
        if restored:
            self._log.info("source %d restored from trash", source_id)
            self.sources_changed.emit(self, source_id=source_id, kind="restore")
        return restored

    # ================================================================
    # Reconciler 集成
    # ================================================================
    def _register_reconciler(self) -> None:
        """把 purge 挂到 Reconciler 上。

        * ``incremental``：软删时 ``invalidate(name, str(source_id))`` 送来的
          那批 key；
        * ``full``：启动补清理 —— 读 ``list_pending_purge_ids()``（上次异常
          退出时软删了但没来得及 purge 的源），这是 ``on_startup=True`` 的
          唯一理由。

        重复注册会抛 ``ValueError``（Reconciler 的契约）。容器里该服务是
        singleton，正常装配不会撞上；单测里请各自新建 Reconciler。
        """
        self._reconciler.register(
            self._config.reconciler_name,
            incremental=self._purge_batch,
            full=self._purge_all_pending,
            on_startup=True,
            debounce=self._config.debounce,
            priority=self._config.priority,
        )

    def _schedule(self, source_id: int) -> None:
        """投递一次 debounce 后的 purge。

        失败**不向上抛**：此时软删已经提交、信号已经发出，DB 已经是正确状态；
        投递失败只会让硬删延后到下次启动（``on_startup`` 会补），不该让用户的
        「删除」操作报错。
        """
        try:
            self._reconciler.invalidate(
                self._config.reconciler_name, str(source_id),
            )
        except Exception:  # noqa: BLE001 —— 见 docstring
            self._log.exception(
                "failed to enqueue purge for source %d; "
                "on_startup will retry", source_id,
            )

    def _purge_batch(self, source_ids: set[str]) -> None:
        """``incremental`` 回调：在 TaskService 工作线程上跑。"""
        for raw in source_ids:
            try:
                sid = int(raw)
            except (TypeError, ValueError):
                self._log.warning("ignore malformed source id: %r", raw)
                continue
            try:
                self._purge_one(sid)
            except Exception:  # noqa: BLE001 —— 单个源失败不该中断整批
                self._log.exception("purge source %d failed", sid)

    def _purge_all_pending(self) -> None:
        """``full`` 回调：启动补清理（同样在工作线程上跑）。"""
        ids = self._sources.list_pending_purge_ids()
        if not ids:
            return
        self._log.info("found %d pending source purge(s) at startup", len(ids))
        for sid in ids:
            try:
                self._purge_one(sid)
            except Exception:  # noqa: BLE001
                self._log.exception("purge source %d failed", sid)

    # ================================================================
    # 核心
    # ================================================================
    def _purge_one(self, source_id: int) -> SourcePurgeReport:
        """先快照缩略图 → 硬删 DB → 后删磁盘。

        顺序是刻意的：DB 先删，文件删失败只是垃圾文件、可事后由
        ``ThumbnailCleaner`` 对账清掉；反过来先删文件而 DB 删失败则是
        不可逆的数据丢失。

        ``purge`` 只在行仍处于 ``is_deleted=1`` 时生效 —— 期间用户点了「恢复」
        就整个空转（``purged=False``），这正是回收站要的语义。
        """
        thumbs = self._thumbs.get_paths_by_source(source_id)
        report = self._sources.purge(source_id)

        if not report.purged:
            return report

        self._log.info(
            "purged source %d: %d asset rows, %d thumb dirs",
            source_id, len(report.asset_ids), len(thumbs),
        )
        self._remove_thumbnail_files(source_id, thumbs)
        self.sources_changed.emit(self, source_id=source_id, kind="purged")
        return report

    def _remove_thumbnail_files(
            self,
            source_id: int,
            paths: Dict[str, ThumbnailPaths],
    ) -> None:
        """best-effort 删缩略图文件。任何失败都吞掉。

        三道防护，按顺序生效：

        1. **共享 md5 布局不按 asset 删文件**：新布局下 ``rel`` 形如
           ``<spec>/<md5(file_uri)>.png``、``base`` 是整根，同一源文件被多条
           asset 行引用时共享同一个 PNG —— 这里 unlink 会波及其它 asset 的
           缩略图。孤儿一律交给 ``ThumbnailCleaner``（按 PNG 内嵌
           ``Thumb::URI`` 判定）兜底。
        2. **XDG 防护**：``base`` 落在 XDG 缓存内说明 ``thumb_root`` 配错了，
           拒绝删除并记 warning —— 那是跨应用共享目录，删了会连累别的应用。
        3. **旧私有布局**（``{version}/{uuid[:2]}/{uuid}/…``）仍按 ``base/rel``
           逐个 unlink + rmdir base，供迁移期残留在旧树上的文件清理。

        现状提醒：当前仓库**没有任何代码写 ``assets.thumb_path``**
        （``ThumbnailService`` 尚未迁出），所以新导入资产的 ``base`` 恒为空、
        本函数恒为空转；真正会删到文件的只有「从旧版本升级上来的库」。
        """
        if not paths:
            return

        removed_files = 0
        removed_dirs = 0
        skipped_xdg = 0
        skipped_shared = 0

        for asset_uuid, tp in paths.items():
            base = getattr(tp, "base", None)
            if not base:
                continue

            # 1. 共享 md5 布局：只清 DB（调用方已清），文件交给 cleaner
            if self._uses_shared_md5_layout(tp):
                skipped_shared += 1
                self._log.debug(
                    "keep shared thumbnail file "
                    "(source=%d, uuid=%s, base=%s); "
                    "orphans are handled by ThumbnailCleaner",
                    source_id, asset_uuid, base,
                )
                continue

            # 2. XDG 防护（惰性 import，避免 utils 反向依赖服务层）
            from albuswall.utils.xdg_thumbnail import is_inside_xdg_cache
            if is_inside_xdg_cache(base):
                skipped_xdg += 1
                self._log.warning(
                    "refuse to delete thumbnail under XDG cache "
                    "(source=%d, uuid=%s, base=%s); thumb_root "
                    "is likely misconfigured",
                    source_id, asset_uuid, base,
                )
                continue

            # 3. 旧私有布局：按 base/rel 精确删
            for _spec, rel in tp.items():
                if not rel:
                    continue
                abs_path = os.path.join(base.rstrip("/\\"), rel)
                try:
                    os.unlink(abs_path)
                    removed_files += 1
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    self._log.warning(
                        "failed to unlink thumbnail %s "
                        "(source=%d, uuid=%s): %s",
                        abs_path, source_id, asset_uuid, exc,
                    )

            try:
                os.rmdir(base)
                removed_dirs += 1
            except OSError:
                pass

        self._log.debug(
            "source %d: removed %d files, %d dirs, skipped_xdg=%d, "
            "skipped_shared=%d",
            source_id, removed_files, removed_dirs, skipped_xdg,
            skipped_shared,
        )

    @staticmethod
    def _uses_shared_md5_layout(tp: ThumbnailPaths) -> bool:
        """判断该资产的路径快照是否属于「共享 md5 文件名」布局。

        判据：任一 spec 的相对路径形如 ``<spec>/<32hex>.png``
        （两种模式的规格目录名都在 ``XDG_SPECS`` 内）。旧私有布局的
        ``small.jpg`` / ``medium.jpg`` 等不允许被误判。
        """
        from albuswall.utils.xdg_thumbnail import XDG_SPECS

        for _spec, rel in tp.items():
            if not rel:
                continue
            norm = rel.replace("\\", "/")
            directory, _, name = norm.rpartition("/")
            if directory not in XDG_SPECS:
                continue
            if not name.endswith(".png") or len(name) != 36:
                continue
            if all(c in "0123456789abcdef" for c in name[:-4]):
                return True
        return False
