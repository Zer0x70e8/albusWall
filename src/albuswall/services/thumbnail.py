#
""""""

from __future__ import annotations

import os
import time
from logging import getLogger
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from albuswall.dto.task import ExecutorType
from albuswall.dto.thumbnail import (
    ThumbnailResult,
    ThumbnailStats,
    ThumbnailTaskInput,
)
from albuswall.repositories.thumbnail import ThumbnailRepository
from albuswall.services.task import TaskService
from albuswall.utils import path as path_util
from albuswall.log import TRACE

if TYPE_CHECKING:
    from PIL.Image import Image as PILImage
    from albuswall.log import Logger
    from albuswall.repositories.source import IngestSourceRepository

_logger = getLogger(__name__)
# noinspection statement-effect
_logger  # type: Logger
# noinspection unresolved-references
_logger.trace = lambda msg, *args, **kwargs: _logger.log(TRACE, msg, *args, **kwargs)

PRIORITY_AFTER_IMPORT = 0
PRIORITY_MANUAL = 1
PRIORITY_BACKFILL = 5


class _DefaultThumbnailConfig:
    version = "v1"
    specs = {"small": 128, "medium": 512, "large": 1024}
    image_format = "JPEG"
    quality = 85
    thumb_root = "/home/skyline/.cache/albuswall/thumbs"
    batch_size = 32


# ---------------------------------------------------------------------- #
# 内部工具：渲染
# ---------------------------------------------------------------------- #
class _Renderer:
    """纯计算：源文件 → {spec: PIL.Image}。不碰 DB、不碰磁盘写入。"""

    _DEFAULT_SIZES: dict[str, int] = {
        "small": 128,
        "medium": 512,
        "large": 1024,
    }

    @classmethod
    def render(cls, src: Path, cfg) -> dict[str, "PILImage"]:
        from PIL import Image, ImageOps

        # ✅ 修复：Pillow 10+ 把 LANCZOS 挪到了 Image.Resampling 下，
        #    顶层 Image.LANCZOS 在类型存根里可能不可见。
        #    用 getattr 链做兼容，同时避开静态检查器。
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


# ---------------------------------------------------------------------- #
# 内部工具：原子落盘
# ---------------------------------------------------------------------- #
class _AtomicFS:
    """写文件到临时路径 → os.replace，避免读到半截文件。"""

    @staticmethod
    def write_image(img: "PILImage", dest: Path, *, fmt: str, quality: int) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".tmp")

        save_kwargs: dict = {}
        fmt_upper = fmt.upper()
        if fmt_upper in ("JPEG", "JPG", "WEBP"):
            save_kwargs["quality"] = quality

        # ✅ 修复：「异常子句过于宽泛」
        #    用 finally + 成功标志做清理，不再吞掉任何异常。
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
                    # 清理失败不影响主异常向上抛出
                    _logger.trace("failed to remove temp file %s", tmp)


# ---------------------------------------------------------------------- #
# 服务
# ---------------------------------------------------------------------- #
class ThumbnailService:
    """
    缩略图编排者。

    依赖 TaskService 做执行底座，不做自己的队列/并发/重试。
    依赖 TriggerService/Scheduler 做定时兜底。
    依赖 ThumbnailRepository 做落库。
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
        self._stopped = False

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """注册到 trigger / scheduler。"""
        # ✅ 修复：「方法 'start' 可能为 'static'」
        #    方法体里显式引用 self，保持实例方法语义（后续挂 trigger 也要用）。
        if self._stopped:
            raise RuntimeError("ThumbnailService is stopped")
        version = getattr(self._cfg, "version", "?")
        specs = getattr(self._cfg, "specs", None) or ("small", "medium", "large")
        _logger.info("ThumbnailService started (version=%s, specs=%s)",
                     version, tuple(specs))

    def stop(self) -> None:
        self._stopped = True
        _logger.info("ThumbnailService stopped")

    # ------------------------------------------------------------------ #
    # 对外 API
    # ------------------------------------------------------------------ #
    def submit(self, asset_id: int, *,
               priority: int = PRIORITY_AFTER_IMPORT,
               executor: ExecutorType = "thread"):
        if self._stopped:
            raise RuntimeError("ThumbnailService is stopped")
        return self._task_service.submit(
            self._work_one,
            asset_id,
            executor=executor,
            priority=priority,
        )

    def submit_bulk(self, asset_ids, *,
                    priority: int = PRIORITY_AFTER_IMPORT,
                    executor: ExecutorType = "thread"):
        return [self.submit(aid, priority=priority, executor=executor)
                for aid in asset_ids]

    def regenerate(self, uuid: str):
        """手动重建：按 uuid 查 asset，再提交。"""
        task_input = self._repo.get_task_input_by_uuid(uuid)
        if task_input is None:
            raise KeyError(f"asset not found by uuid: {uuid!r}")
        _logger.info("manual thumbnail rebuild: uuid=%s asset=%d",
                     uuid, task_input.asset_id)
        return self.submit(
            task_input.asset_id,
            priority=PRIORITY_MANUAL,
        )

    def scan_and_submit(self, batch: Optional[int] = None) -> int:
        limit = batch or self._cfg.batch_size
        rows = self._repo.list_missing(
            specs=("small", "medium", "large"),
            limit=limit,
        )
        if not rows:
            return 0

        # ✅ 修复：MissingThumbnailRow 是 dataclass，用属性访问
        ids = [r.id for r in rows]
        _logger.info("thumbnail backfill: submitting %d tasks", len(ids))
        self.submit_bulk(ids, priority=PRIORITY_BACKFILL)
        return len(ids)

    def run_once(self, batch: Optional[int] = None) -> dict:
        limit = batch or self._cfg.batch_size
        rows = self._repo.list_missing(
            specs=("small", "medium", "large"),
            limit=limit,
        )
        if not rows:
            return {"scanned": 0, "ok": 0, "failed": 0}

        # ✅ 同上，属性访问
        futures = self.submit_bulk([r.id for r in rows],
                                   priority=PRIORITY_BACKFILL)

        ok = failed = 0
        for fut in futures:
            # noinspection broad-exception
            try:
                result = fut.result()
                if result.ok:
                    ok += 1
                else:
                    failed += 1
            except Exception:
                failed += 1

        return {"scanned": len(rows), "ok": ok, "failed": failed}

    def stats(self) -> ThumbnailStats:
        # ✅ 修复：「应为类型 dict[Any, Any]，但实际为 ThumbnailStats」
        #    把返回注解改成 ThumbnailStats，和实现对齐。
        return self._repo.count_by_status()

    # ------------------------------------------------------------------ #
    # Worker：TaskService 实际执行的可调用对象
    # ------------------------------------------------------------------ #
    def _work_one(self, asset_id: int) -> ThumbnailResult:
        started = time.monotonic()

        task_input: Optional[ThumbnailTaskInput] = \
            self._repo.get_task_input(asset_id)
        if task_input is None:
            _logger.warning("thumbnail task skipped: asset %d not found", asset_id)
            return ThumbnailResult(ok=False, asset_id=asset_id,
                                   error="asset_not_found")

        src = path_util.resolve_asset_source(
            source_id=task_input.source_id,
            source_path=task_input.source_path,
            file_path=task_input.file_path,
        )
        if not src.is_file():
            _logger.warning("thumbnail skipped: source missing %s", src)
            return ThumbnailResult(ok=False, asset_id=asset_id,
                                   error="source_missing")

        try:
            imgs = _Renderer.render(src, self._cfg)
        except Exception as exc:
            _logger.error("thumbnail render failed asset=%d: %s", asset_id, exc)
            return ThumbnailResult(ok=False, asset_id=asset_id,
                                   error=f"render:{type(exc).__name__}")

        base_rel, spec_rels = path_util.build_thumb_paths(
            uuid=task_input.uuid,
            version=self._cfg.version,
            fmt=self._cfg.image_format,
        )

        try:
            for spec, img in imgs.items():
                dest = Path(self._cfg.thumb_root) / base_rel / spec_rels[spec]
                _AtomicFS.write_image(img, dest,
                                      fmt=self._cfg.image_format,
                                      quality=self._cfg.quality)
        except Exception as exc:
            _logger.error("thumbnail write failed asset=%d: %s", asset_id, exc)
            return ThumbnailResult(ok=False, asset_id=asset_id,
                                   error=f"write:{type(exc).__name__}")

        self._repo.update_paths(
            asset_id=asset_id,
            paths=spec_rels,
            base_dir=str(Path(self._cfg.thumb_root) / base_rel),
        )

        duration_ms = int((time.monotonic() - started) * 1000)
        _logger.trace("thumbnail done asset=%d uuid=%s in %dms",
                      asset_id, task_input.uuid, duration_ms)
        return ThumbnailResult(ok=True, asset_id=asset_id, duration_ms=duration_ms)
