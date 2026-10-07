#
"""Import service: 从候选缓存表读取 pending 记录，提交给 TaskService 的进程池处理，主线程回收并落库。

结构：
- 配置层（ImportConfig）：
  把 ImportService 的可调参数集中声明，走 ConfigDeclaration / ConfigField 机制。
  模块级单例 _cfg，业务侧直接 `_cfg.xxx` 读取。
- 纯函数层（process_candidate / _compute_file_hash / _extract_image_metadata）：
  在子进程执行，无状态、无 DB 连接、可 pickle。
- ImportService：
  常驻后台 worker + 单轮 process_pending；
  外部（SourceService.scan_finished、调度器）通过 trigger() 唤醒一轮。
"""

import hashlib
import mimetypes
import threading
import time
from pathlib import Path, PurePosixPath
from typing import Dict, Optional, TYPE_CHECKING
from concurrent.futures import (
    FIRST_COMPLETED,
    wait,
    TimeoutError as FutureTimeoutError,
)

from albuswall.configue import ConfigField
from albuswall.dto.import_ import AssetCandidateCacheDTO, AssetCreateDTO
from albuswall.log import getLogger
from albuswall.utils.signal import Signal

try:
    # 统一 EXIF 解析入口；缺失时不影响导入主流程。
    # 约定：返回 dict，键包括 width / height / taken_at / exif_json（后两者可为 None）。
    from albuswall.repositories.utils.exif import (
        extract_image_metadata as _shared_extract_image_metadata,
    )

    HAS_EXIF = True
except ImportError:  # pragma: no cover - 环境缺依赖时的降级路径
    _shared_extract_image_metadata = None  # type: ignore[assignment]
    HAS_EXIF = False

if TYPE_CHECKING:
    from albuswall.repositories.import_ import ImportRepository
    from albuswall.infrastructure.task import TaskService

_logger = getLogger(__name__)


# ============================================================
# 配置声明
# ============================================================

class ImportConfig:
    """ImportService 的可调参数。

    对应配置文件（_config.toml / *.ini 等）：

        [import]
        batch_size      = 500     # 单轮 pending 批量上限
        process_timeout = 600.0   # 单任务等待上限（秒）
        initial_backoff = 1.0     # worker 异常后首次退避（秒）
        max_backoff     = 60.0    # worker 退避封顶（秒）

    生命周期：容器启动阶段调用一次 `ImportConfig.load(resolver)`，
    把类型与默认值注册进 Resolver。
    """

    # 单轮 pending 批量上限。
    batch_size: int = ConfigField("import", "batch_size", default=500)

    # 单个候选任务在进程池里的等待上限（秒）；超时由主线程取消 + 标失败。
    process_timeout: float = ConfigField(
        "import", "process_timeout", default=600.0,
    )

    # worker 异常后首次退避（秒）；连续失败按 2 倍指数增长。
    initial_backoff: float = ConfigField(
        "import", "initial_backoff", default=1.0,
    )

    # worker 退避上限（秒）。
    max_backoff: float = ConfigField(
        "import", "max_backoff", default=60.0,
    )


"""
如果希望「超时也能被 kill」，Python 的 ProcessPoolExecutor 做不到，
得换成显式的 multiprocessing.Process + join(timeout) + terminate()，
或者用 pebble 之类的库。
当前实现里 process_timeout 只能保证主线程不被卡死，
不能保证子进程真的停下来——这一点最好在 ImportConfig 的注释里点明，
避免以后有人以为超时后资源已经释放了。
"""

_cfg = ImportConfig()


# ============================================================
# 纯函数层：在子进程中执行，无状态、无 DB 连接、可 pickle
# ============================================================

def process_candidate(
        candidate: AssetCandidateCacheDTO,
        source_path: str,
) -> Optional[AssetCreateDTO]:
    """处理单个候选文件，返回 AssetCreateDTO 或 None。

    路径语义（与 media_library_schema.sql 保持一致）：
    - candidate.path 始终为**相对形式**。
    - source_id == 0：candidate.path 自带语义
        · POSIX  ：相对 '/'，如 "home/user/a.jpg"
        · Windows：带盘符，如 "C:/Users/a.jpg"
    - source_id != 0：candidate.path 相对 ingest_source.source_path。
    - 写入 assets.file_path 的仍然是 candidate.path（相对形式），
      实际绝对路径由 source_id 决定。

    子进程契约：
    - 输入 / 输出都可 pickle。
    - 不访问 DB，不持有除 logger 之外的共享资源。
    - 顶层函数，无闭包捕获。
    """
    try:
        # 基本防御：拒绝 '..'，避免越出 source_path。
        if ".." in PurePosixPath(candidate.path).parts:
            _logger.error("候选路径包含 '..'，拒绝处理: %s", candidate.path)
            return None

        if candidate.source_id == 0:
            abs_path = Path(candidate.path)
            if not abs_path.is_absolute():
                # POSIX 相对 '/' 的存储形式，补根。
                abs_path = Path("/") / candidate.path
        else:
            if not source_path:
                _logger.error(
                    "source_id=%s 的候选缺少 source_path，无法定位: %s",
                    candidate.source_id, candidate.path,
                )
                return None
            abs_path = Path(source_path) / candidate.path
            # 越界防御：解析后必须仍在 source_path 之下。
            try:
                abs_path.resolve().relative_to(Path(source_path).resolve())
            except ValueError:
                _logger.error(
                    "候选路径越过 source_path 边界: %s (source=%s)",
                    candidate.path, source_path,
                )
                return None

        if not abs_path.exists() or not abs_path.is_file():
            _logger.error("文件不存在或不是普通文件: %s", abs_path)
            return None

        stat = abs_path.stat()
        mime_type = candidate.mime_type or (
                mimetypes.guess_type(abs_path.name)[0] or "application/octet-stream"
        )

        width = height = 0
        taken_at = None
        exif_json = None
        if mime_type.startswith("image/") and HAS_EXIF:
            width, height, taken_at, exif_json = _extract_image_metadata(abs_path)

        return AssetCreateDTO(
            uuid=candidate.uuid,
            file_path=candidate.path,  # 相对形式，与 schema 注释一致
            original_name=abs_path.name,
            mime_type=mime_type,
            file_hash=_compute_file_hash(abs_path),
            file_size=stat.st_size,
            width=width,
            height=height,
            source_id=candidate.source_id,
            thumb_path=None,
            thumb_small_path=None,
            thumb_medium_path=None,
            thumb_large_path=None,
            taken_at=taken_at,
            exif_json=exif_json,
            is_favorite=0,
            is_deleted=0,
        )
    except Exception as e:
        _logger.exception("处理候选 %s 失败: %s", candidate.uuid, e)
        return None


def _compute_file_hash(path: Path) -> str:
    sha256 = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


def _extract_image_metadata(path: Path):
    """统一走 repositories/utils/exif.py 的解析入口。

    :return (width, height, taken_at, exif_json)；任何失败返回 (0, 0, None, None)。

    契约：本函数保证不抛异常——上游返回脏数据（如 width="abc"）也只降级，
    不让单个坏字段把整个候选判为失败。
    """
    if not HAS_EXIF or _shared_extract_image_metadata is None:
        return 0, 0, None, None

    try:
        meta = _shared_extract_image_metadata(path) or {}
    except Exception as e:
        _logger.warning("读取图像元数据失败 %s: %s", path, e)
        return 0, 0, None, None

    def _to_int(v) -> int:
        try:
            return int(v) if v is not None else 0
        except (TypeError, ValueError):
            return 0

    width = _to_int(meta.get("width"))
    height = _to_int(meta.get("height"))

    taken_at = meta.get("taken_at")
    if taken_at is not None and not isinstance(taken_at, str):
        # AssetCreateDTO.taken_at 语义是 ISO8601 字符串；
        # 上游给 datetime 是常见误用，容忍并转字符串。
        taken_at = str(taken_at)

    exif_json = meta.get("exif_json")
    if exif_json is not None and not isinstance(exif_json, str):
        # 契约要求 JSON 字符串；脏数据一律丢弃，不要塞进 DB。
        _logger.warning(
            "exif_json 类型异常 %s (%s)，已丢弃", type(exif_json).__name__, path
        )
        exif_json = None

    return width, height, taken_at, exif_json


# ============================================================
# 服务层：常驻 worker + 单轮处理
# ============================================================

# noinspection broad-exception
class ImportService:
    """asset_candidate_cache → assets 的导入闭环。

    生命周期：
    - start()：容器启动时调用，启动常驻 worker 并立即触发一轮。
    - trigger()：外部（SourceService.scan_finished / 调度器）调用，唤醒一轮。
    - stop_worker()：容器关闭时调用。

    同步调用：
    - process_pending()：直接执行一轮，幂等（_run_lock 防重入）。
    """
    assets_imported: Signal = Signal(name="AssetsImported")

    def __init__(self, repo: "ImportRepository", task: "TaskService"):
        self._repo = repo
        self._task = task

        self._wake = threading.Event()
        self._stop = threading.Event()
        self._run_lock = threading.Lock()
        self._worker: Optional[threading.Thread] = None

    # ---- 生命周期 ---------------------------------------------------------

    def start(self) -> None:
        """容器启动时调用一次。"""
        self.start_worker()

    # noinspection unresolved-references
    def start_worker(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._stop.clear()
        self._worker = threading.Thread(
            target=self._worker_loop,
            name="albuswall-import-worker",
            daemon=True,
        )
        self._worker.start()
        # 启动即扫一遍，避免错过启动前已存在的 pending。
        self.trigger()

    def stop_worker(self, timeout: float = 10.0) -> None:
        self._stop.set()
        self._wake.set()
        worker = self._worker
        if worker and worker.is_alive():
            worker.join(timeout=timeout)

    def trigger(self) -> None:
        """外部模块触发一轮导入（幂等，可重复调用）。"""
        self._wake.set()

    # ---- worker 主循环 ----------------------------------------------------

    def _worker_loop(self) -> None:
        # 退避参数在进入循环时读一次；若运行期需要热更新，
        # 改成每轮读 _cfg.initial_backoff / _cfg.max_backoff 即可。
        initial_backoff = _cfg.initial_backoff
        max_backoff = _cfg.max_backoff
        backoff = initial_backoff

        while not self._stop.is_set():
            self._wake.wait()
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                self.process_pending()
            except Exception:
                # 持续性故障（DB 锁、磁盘满、source_paths 反序列化异常等）下，
                # 立即自唤会空转刷屏；改为指数退避，退避等待可被 stop 打断。
                _logger.exception(
                    "导入 worker 处理异常，%.1fs 后重试", backoff,
                )
                # _stop.wait 返回 True 表示 stop_worker 被调用，直接退出。
                if self._stop.wait(backoff):
                    break
                # 退避结束，自我唤醒排下一轮。
                # 若退避期间外部已 trigger()，_wake 已置位，下轮 wait() 会立刻返回。
                self._wake.set()
                backoff = min(backoff * 2, max_backoff)
            else:
                # 一轮正常结束即复位退避，保持对正常 trigger 的低延迟响应。
                backoff = initial_backoff

    # ---- 单轮处理 ---------------------------------------------------------

    def process_pending(self) -> None:
        """执行一轮 pending 处理。带重入保护：已在执行则立即返回。"""
        if not self._run_lock.acquire(blocking=False):
            _logger.debug("已有导入任务在执行，跳过本轮 process_pending")
            return
        try:
            self._run_once()
        finally:
            self._run_lock.release()

    def _run_once(self) -> None:
        # 启动前回收崩溃残留。
        recovered = self._repo.recover_stale_candidates()
        if recovered:
            _logger.debug("Recovered %d stale candidate(s)", recovered)

        # 预取 source_id → source_path，供子进程解析绝对路径。
        source_paths: Dict[int, str] = self._repo.get_source_paths()

        batch_size = _cfg.batch_size

        totals = {"ok": 0, "failed": 0, "skipped": 0, "retry": 0, "timeout": 0}

        while not self._stop.is_set():
            candidates = self._repo.get_pending_candidates(limit=batch_size)
            if not candidates:
                break

            _logger.info("Processing batch of %d pending candidates", len(candidates))
            self._process_batch(candidates, source_paths, totals)

            # 不足一批说明已到底。
            if len(candidates) < batch_size:
                break

        _logger.info(
            "Import round finished. ok=%d failed=%d skipped=%d retry=%d timeout=%d",
            totals["ok"], totals["failed"], totals["skipped"],
            totals["retry"], totals["timeout"],
        )
        if totals["ok"] > 0:
            try:
                self.assets_imported.emit(self, **totals)
            except Exception:
                _logger.exception("emit assets_imported failed")

    # ---- 批处理 -----------------------------------------------------------

    def _process_batch(
            self,
            candidates,
            source_paths,
            totals
    ) -> None:
        futures = {}
        for cand in candidates:
            source_path = source_paths.get(cand.source_id, "")
            try:
                fut = self._task.submit(
                    process_candidate,
                    cand,
                    source_path,
                    executor="process",
                    priority=5,
                )
                futures[fut] = cand
            except Exception:
                _logger.exception("提交候选 %s 失败", cand.uuid)
                self._safe_mark_failed(cand.id, reason="submit_failed")
                totals["failed"] += 1

        if not futures:
            return

        timeout = _cfg.process_timeout
        now = time.monotonic()
        # 每个 future 独立的 deadline；从提交完成时刻开始计时。
        deadlines = {fut: now + timeout for fut in futures}
        pending = set(futures.keys())

        while pending:
            now = time.monotonic()
            next_deadline = min(deadlines[fut] for fut in pending)
            wait_timeout = max(0.0, next_deadline - now)

            try:
                done, _ = wait(
                    pending,
                    timeout=wait_timeout,
                    return_when=FIRST_COMPLETED,
                )
            except Exception:
                _logger.exception("等待 future 完成时异常，取消剩余任务")
                for fut in pending:
                    fut.cancel()
                break

            for fut in done:
                pending.discard(fut)
                self._handle_result(futures[fut], fut, totals)

            # 处理到期的任务（可能 wait_timeout=0 时立即进来）
            now = time.monotonic()
            expired = [fut for fut in pending if now >= deadlines[fut]]
            for fut in expired:
                pending.discard(fut)
                cand = futures[fut]
                _logger.error("候选 %s 处理超时（>%.0fs）", cand.uuid, timeout)
                fut.cancel()  # 尽力而为；进程池任务未必能真正中断
                self._safe_mark_failed(cand.id, reason="timeout")
                totals["timeout"] += 1

    def _handle_result(self, cand, fut, totals) -> None:
        try:
            asset_dto = fut.result()  # 已经 done 或 cancelled，不会阻塞
        except FutureTimeoutError:
            # 保留兜底：wait 循环之外的路径（例如未来有别的调用方）也会走这里。
            _logger.error("候选 %s 处理超时", cand.uuid)
            fut.cancel()
            self._safe_mark_failed(cand.id, reason="timeout")
            totals["timeout"] += 1
            return
        except Exception:
            _logger.exception("候选 %s 执行异常", cand.uuid)
            self._safe_mark_failed(cand.id, reason="process_error")
            totals["failed"] += 1
            return

        if asset_dto is None:
            self._safe_mark_failed(cand.id, reason="no_payload")
            totals["failed"] += 1
            return

        self._finalize(cand, asset_dto, totals)

    def _finalize(self, cand, asset_dto: AssetCreateDTO, totals: Dict[str, int]) -> None:
        try:
            asset_id = self._repo.finalize_candidate(cand.id, asset_dto)
        except Exception:
            # 临时 DB 错误：不动候选状态，下一轮重新取到后重试。
            # 若反复失败，可由 repo 侧根据 retry 计数做退避/上限。
            _logger.exception(
                "落库候选 %s 时 DB 异常，保持 pending 待重试", cand.uuid,
            )
            totals["retry"] += 1
            return

        if asset_id:
            totals["ok"] += 1
            _logger.debug("候选 %s → asset_uuid=%s", cand.uuid, asset_id)
            return

        # repo 判定为永久失败（唯一约束冲突 → skipped，其余 → failed）。
        status = self._repo.get_candidate_status(cand.id)
        if status == "skipped":
            totals["skipped"] += 1
            _logger.info("候选 %s 判定为重复资产，已跳过", cand.uuid)
        else:
            totals["failed"] += 1
            _logger.error("插入资产失败: 候选 %s status=%s", cand.uuid, status)

    def _safe_mark_failed(self, candidate_id, reason: str) -> None:
        try:
            self._repo.mark_candidate_failed(candidate_id)
        except Exception:
            _logger.exception(
                "标记候选 %s 失败状态出错 (reason=%s)", candidate_id, reason,
            )
