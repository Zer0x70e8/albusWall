#
""""""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Optional, Sequence, TYPE_CHECKING

from albuswall.dto.task import ExecutorType
from albuswall.dto.thumbnail import (
    ThumbnailPaths,
    ThumbnailResult,
    ThumbnailStats,
    ThumbnailTaskInput,
)
from albuswall.repositories.thumbnail import ThumbnailRepository
from albuswall.services.task import TaskService
from albuswall.utils import path as path_util
from albuswall.log import getLogger

if TYPE_CHECKING:
    from PIL.Image import Image as PILImage
    from albuswall.repositories.source import IngestSourceRepository

_logger = getLogger(__name__)

PRIORITY_AFTER_IMPORT = 0
PRIORITY_MANUAL = 1
PRIORITY_BACKFILL = 5

ALL_SPECS: tuple[str, ...] = ("small", "medium", "large")


class _DefaultThumbnailConfig:
    version = "v1"
    specs = {"small": 128, "medium": 512, "large": 1024}
    image_format = "JPEG"
    quality = 85
    thumb_root = "/home/skyline/.cache/albuswall/thumbs"
    batch_size = 32


# ---------------------------------------------------------------------- #
# 渲染
# ---------------------------------------------------------------------- #
class _Renderer:
    """纯计算：源文件 → {spec: PIL.Image}。不碰 DB、不碰磁盘写入。"""

    _DEFAULT_SIZES: dict[str, int] = {
        "small": 128, "medium": 512, "large": 1024,
    }

    @classmethod
    def render(cls, src: Path, cfg) -> dict[str, "PILImage"]:
        from PIL import Image, ImageOps

        resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
        specs: dict[str, int] = getattr(cfg, "specs", None) or cls._DEFAULT_SIZES

        with Image.open(src) as im:
            im = ImageOps.exif_transpose(im)
            if im.mode not in ("RGB", "RGBA"):
                im = im.convert("RGB")

            result: dict[str, "PILImage"] = {}
            for spec, size in specs.items():
                thumb = im.copy()
                thumb.thumbnail((size, size), resample)
                result[spec] = thumb

        return result


def _is_permanent_render_error(exc: BaseException) -> bool:
    """True 表示“这个文件永远渲染不出来”，重试无意义。

    目前覆盖：PIL 无法识别的文件格式（非图片、损坏文件头）。
    视频、PDF 等也会落到这里，直接走永久性失败路径。
    """
    try:
        from PIL import UnidentifiedImageError
    except ImportError:  # pragma: no cover - Pillow 一定存在
        return False
    return isinstance(exc, UnidentifiedImageError)


# ---------------------------------------------------------------------- #
# 原子落盘
# ---------------------------------------------------------------------- #
class _AtomicFS:
    @staticmethod
    def write_image(img: "PILImage", dest: Path, *, fmt: str, quality: int) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".tmp")

        save_kwargs: dict = {}
        fmt_upper = fmt.upper()
        if fmt_upper in ("JPEG", "JPG", "WEBP"):
            save_kwargs["quality"] = quality

        committed = False
        try:
            img.save(tmp, format=fmt_upper, **save_kwargs)
            os.replace(tmp, dest)
            committed = True
        finally:
            if not committed and tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    _logger.trace("failed to remove temp file %s", tmp)


# ---------------------------------------------------------------------- #
# 服务
# ---------------------------------------------------------------------- #
class ThumbnailService:
    """
    缩略图编排者。

    - TaskService 做执行底座，不做自己的队列/并发/重试。
    - TriggerService/Scheduler 做定时兜底。
    - ThumbnailRepository 做落库。

    路径契约（与 schema / 仓储一致）：
        写：thumb_path 列 ← **绝对** base 目录；thumb_<spec>_path 列 ← 相对文件名。
        读：只从 DB 取 (thumb_path, spec_rel) 直接拼，绝不调 build_thumb_paths，
            否则 version 升级后会把旧资产映射到新版本目录（读到空文件）。

    仅支持图片缩略图；视频/PDF 由 PIL 抛 UnidentifiedImageError，
    走永久性失败路径（见 supports_video()）。
    """

    def __init__(
            self,
            task_service: TaskService,
            thumb_repo: ThumbnailRepository,
            source_repo: "IngestSourceRepository",
            cfg=None,  # ThumbnailConfig
    ):
        self._task_service = task_service
        self._repo = thumb_repo
        self._source_repo = source_repo
        self._cfg = cfg or _DefaultThumbnailConfig()

        # thumb_root 归一化成绝对路径，保证写库的 thumb_path 永远是绝对路径
        self._thumb_root: Path = Path(self._cfg.thumb_root).expanduser().resolve()

        self._stopped = False

        # P1 去重：在途任务键集合 (asset_id, version)
        # version 参与 key，老版本在途不会阻塞新版本重建
        self._inflight: set[tuple[int, str]] = set()
        self._inflight_lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        if self._stopped:
            raise RuntimeError("ThumbnailService is stopped")
        version = getattr(self._cfg, "version", "?")
        specs = getattr(self._cfg, "specs", None) or ALL_SPECS
        _logger.info("ThumbnailService started (version=%s, specs=%s, root=%s)",
                     version, tuple(specs), self._thumb_root)

    def stop(self) -> None:
        self._stopped = True
        _logger.info("ThumbnailService stopped")

    # ------------------------------------------------------------------ #
    # 能力声明
    # ------------------------------------------------------------------ #
    @staticmethod
    def supports_video() -> bool:
        """当前实现仅支持图片。调用方据此决定是否对视频资产提交任务。

        若要扩展视频，替换 _Renderer 为 ffmpeg 管道或 imageio，
        并把 UnidentifiedImageError 之外的新异常纳入永久性判断。
        """
        return False

    # ------------------------------------------------------------------ #
    # 提交
    # ------------------------------------------------------------------ #
    def submit(
            self,
            asset_id: int,
            *,
            priority: int = PRIORITY_AFTER_IMPORT,
            executor: ExecutorType = "thread",
    ):
        if self._stopped:
            raise RuntimeError("ThumbnailService is stopped")
        if executor != "thread":
            raise ValueError(
                f"executor={executor!r} not supported: _work_one is a bound "
                f"method and cannot be pickled into a process pool. "
                f"Use executor='thread'."
            )

        key = (asset_id, self._cfg.version)
        with self._inflight_lock:
            if key in self._inflight:
                _logger.trace(
                    "thumbnail submit skipped (in flight): asset=%d version=%s",
                    asset_id, self._cfg.version,
                )
                return None
            self._inflight.add(key)

        try:
            future = self._task_service.submit(
                self._work_one,
                asset_id,
                executor="thread",
                priority=priority,
            )
        except Exception:
            # 提交本身失败 → 立即撤销去重标记
            with self._inflight_lock:
                self._inflight.discard(key)
            raise

        # 无论任务是否真正跑到 _work_one，future 一旦进入 done 状态就释放 key。
        # 覆盖：
        #   - _put_back 队列满 → BackpressureError
        #   - shutdown 时 _drain_queue 置 RuntimeError
        #   - 进程池 / 线程池 submit 失败 → 直接置异常
        #   - 正常执行完成（成功 / 失败 / 永久性失败）
        def _release(_fut):
            with self._inflight_lock:
                self._inflight.discard(key)

        future.add_done_callback(_release)
        return future

    def submit_bulk(
            self,
            asset_ids: Sequence[int],
            *,
            priority: int = PRIORITY_AFTER_IMPORT,
            executor: ExecutorType = "thread",
    ) -> list:
        """批量提交。返回实际入队的 future 列表（去重后可能少于入参）。"""
        futures = []
        for aid in asset_ids:
            fut = self.submit(aid, priority=priority, executor=executor)
            if fut is not None:
                futures.append(fut)
        return futures

    # ------------------------------------------------------------------ #
    # 对外 API
    # ------------------------------------------------------------------ #
    def regenerate(self, uuid: str):
        task_input = self._repo.get_task_input_by_uuid(uuid)
        if task_input is None:
            raise KeyError(f"asset not found by uuid: {uuid!r}")
        _logger.info("manual thumbnail rebuild: uuid=%s asset=%d",
                     uuid, task_input.asset_id)
        return self.submit(task_input.asset_id, priority=PRIORITY_MANUAL)

    def scan_and_submit(self, batch: Optional[int] = None) -> int:
        limit = batch or self._cfg.batch_size
        rows = self._repo.list_missing(specs=ALL_SPECS, limit=limit)
        if not rows:
            return 0
        ids = [r.id for r in rows]
        _logger.info("thumbnail backfill: submitting %d tasks", len(ids))
        self.submit_bulk(ids, priority=PRIORITY_BACKFILL)
        return len(ids)

    # noinspection broad-exception
    def run_once(self, batch: Optional[int] = None) -> dict:
        limit = batch or self._cfg.batch_size
        rows = self._repo.list_missing(specs=ALL_SPECS, limit=limit)
        if not rows:
            return {"scanned": 0, "ok": 0, "failed": 0, "skipped": 0}

        futures = self.submit_bulk(
            [r.id for r in rows], priority=PRIORITY_BACKFILL,
        )
        skipped = len(rows) - len(futures)

        ok = failed = 0
        for fut in futures:
            try:
                result = fut.result()
                if result.ok:
                    ok += 1
                else:
                    failed += 1
            except Exception:
                failed += 1

        return {"scanned": len(rows), "ok": ok,
                "failed": failed, "skipped": skipped}

    def stats(self) -> ThumbnailStats:
        return self._repo.count_by_status()

    # ------------------------------------------------------------------ #
    # 读取：只走 DB，禁止重算路径
    # ------------------------------------------------------------------ #
    def get_thumbnail_path(
            self, asset_id: int, spec: str = "medium"
    ) -> Optional[str]:
        """获取某个 spec 缩略图的绝对路径；未生成或资产不存在返回 None。

        实现上直接读 (thumb_path, thumb_<spec>_path) 两列，不做任何
        build_thumb_paths 重算 —— version 升级后旧资产仍指向旧版本目录，
        这样读到的是实际存在的文件。
        """
        paths = self._repo.get_paths(asset_id)
        if paths is None:
            return None
        return paths.resolve(spec)

    def get_thumbnail_paths(
            self, asset_ids: Sequence[int], spec: str = "medium"
    ) -> dict[int, str]:
        """批量版 get_thumbnail_path；返回 {asset_id: abs_path}。

        已删除资产也会被读出（用于 Trash 预览）；只有该 spec 有值的才在结果里。
        """
        if not asset_ids:
            return {}
        rows = self._repo.get_paths_bulk(asset_ids)
        result: dict[int, str] = {}
        for aid, paths in rows.items():
            p = paths.resolve(spec)
            if p:
                result[aid] = p
        return result

    # ------------------------------------------------------------------ #
    # 永久删除：先删文件，再清库
    # ------------------------------------------------------------------ #
    def purge(self, asset_id: int) -> bool:
        """永久删除单个资产的所有缩略图。

        - 磁盘：删除各 spec 文件，尽量 rmdir base 目录。
        - 库：清空 thumb_path / thumb_*_path 列（仓储 purge）。
        返回 True 表示资产存在并已处理；False 表示资产不存在。
        """
        paths = self._repo.get_paths(asset_id)
        if paths is None:
            return False
        self._delete_files(paths)
        self._repo.purge(asset_id)
        _logger.info("thumbnail purged asset=%d", asset_id)
        return True

    def purge_bulk(self, asset_ids: Sequence[int]) -> int:
        """批量 purge；返回处理的资产数。"""
        if not asset_ids:
            return 0
        rows = self._repo.get_paths_bulk(asset_ids)
        if not rows:
            return 0
        for paths in rows.values():
            self._delete_files(paths)
        self._repo.purge_bulk(list(rows.keys()))
        _logger.info("thumbnail purged: %d assets", len(rows))
        return len(rows)

    @staticmethod
    def _delete_files(paths: ThumbnailPaths) -> None:
        """按 (绝对 base, 相对 spec) 删文件。best-effort，不抛异常。"""
        base = paths.base
        if not base:
            return
        base_path = Path(base)

        for spec in ALL_SPECS:
            rel = paths.for_spec(spec)
            if not rel:
                continue
            fp = Path(rel) if Path(rel).is_absolute() else base_path / rel
            try:
                fp.unlink(missing_ok=True)
            except OSError as exc:
                _logger.warning("thumbnail delete failed %s: %s", fp, exc)

        # 目录空了就顺手 rmdir，不为空则忽略
        try:
            base_path.rmdir()
        except OSError:
            pass

    # ------------------------------------------------------------------ #
    # Worker
    # ------------------------------------------------------------------ #
    def _work_one(self, asset_id: int) -> ThumbnailResult:
        """TaskService 调用的入口。去重标记由 submit() 挂在 future 上的
        done callback 统一释放，这里不再负责。"""
        return self._work_one_impl(asset_id)

    def _work_one_impl(self, asset_id: int) -> ThumbnailResult:
        started = time.monotonic()

        task_input: Optional[ThumbnailTaskInput] = \
            self._repo.get_task_input(asset_id)
        if task_input is None:
            # 永久性：资产不存在或已软删；不重试
            _logger.warning("thumbnail task skipped: asset %d not found", asset_id)
            return ThumbnailResult(
                ok=False, asset_id=asset_id,
                error="asset_not_found", retryable=False,
            )

        src = path_util.resolve_asset_source(
            source_id=task_input.source_id,
            source_path=task_input.source_path,
            file_path=task_input.file_path,
        )
        if not src.is_file():
            # 永久性：源文件已丢失；重试也不会出现
            _logger.warning("thumbnail skipped: source missing %s", src)
            return ThumbnailResult(
                ok=False, asset_id=asset_id,
                error="source_missing", retryable=False,
            )

        try:
            imgs = _Renderer.render(src, self._cfg)
        except Exception as exc:
            if _is_permanent_render_error(exc):
                # 永久性：非图片、损坏文件头；重试没有意义
                _logger.warning(
                    "thumbnail render rejected asset=%d (%s): %s",
                    asset_id, type(exc).__name__, exc,
                )
                return ThumbnailResult(
                    ok=False, asset_id=asset_id,
                    error=f"render:{type(exc).__name__}", retryable=False,
                )
            # 暂时性：OOM、IO 抖动等
            _logger.error("thumbnail render failed asset=%d: %s", asset_id, exc)
            return ThumbnailResult(
                ok=False, asset_id=asset_id,
                error=f"render:{type(exc).__name__}", retryable=True,
            )

        base_rel, spec_rels = path_util.build_thumb_paths(
            uuid=task_input.uuid,
            version=self._cfg.version,
            fmt=self._cfg.image_format,
        )
        # 绝对 base：thumb_root(绝对) + 相对 base_rel
        base_abs = self._thumb_root / base_rel

        try:
            for spec, img in imgs.items():
                dest = base_abs / spec_rels[spec]
                _AtomicFS.write_image(
                    img, dest,
                    fmt=self._cfg.image_format,
                    quality=self._cfg.quality,
                )
        except Exception as exc:
            # 暂时性：磁盘满、权限、IO 错误
            _logger.error("thumbnail write failed asset=%d: %s", asset_id, exc)
            return ThumbnailResult(
                ok=False, asset_id=asset_id,
                error=f"write:{type(exc).__name__}", retryable=True,
            )

        # 契约：base 绝对 / spec 相对
        self._repo.update_paths(
            asset_id=asset_id,
            paths=spec_rels,
            base_dir=str(base_abs),
        )

        duration_ms = int((time.monotonic() - started) * 1000)
        _logger.trace("thumbnail done asset=%d uuid=%s in %dms",
                      asset_id, task_input.uuid, duration_ms)
        return ThumbnailResult(
            ok=True, asset_id=asset_id, duration_ms=duration_ms,
        )
