#
"""Import service: 从候选缓存表读取 pending 记录，提交给 TaskService 的进程池处理，主线程回收并落库。

结构：
- 配置层（ImportConfig）：
  把 ImportService 的可调参数集中声明，走 ConfigDeclaration / ConfigField 机制。
  模块级单例 _cfg，业务侧直接 `_cfg.xxx` 读取。
- 纯函数层（process_candidate / _compute_file_hash / _extract_image_metadata /
  detect_magic / _resolve_candidate_path）：
  在子进程执行，无状态、无 DB 连接、可 pickle。``process_candidate`` 是
  "两段式文件类型判断"的 Stage 2（Stage 1 的 glob 粗筛在
  ``services/source.py::SourceExecutor`` 里），返回三态 ``ProcessResult``
  （ok / skip / fail），取代旧的二态 ``Optional[AssetCreateDTO]``。
- ImportService：
  常驻后台 worker + 单轮 process_pending；
  外部（SourceService.scan_finished、调度器）通过 trigger() 唤醒一轮。
"""

import hashlib
import mimetypes
import threading
import time
from concurrent.futures import (
    FIRST_COMPLETED,
    wait,
    TimeoutError as FutureTimeoutError,
)
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Dict, Literal, Optional, TYPE_CHECKING
from urllib.parse import unquote, urlparse

from albuswall.common.enums import CandidateStatus, FileTypeCheckMode
from albuswall.configue import ConfigField
from albuswall.dto.import_ import AssetCandidateCacheDTO, AssetCreateDTO
from albuswall.dto.source import MANUAL_SOURCE_ID, IngestSource
from albuswall.dto.utils import coerce_enum
from albuswall.log import getLogger
from albuswall.utils.signals import SignalDescriptor

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
    from albuswall.repositories.source import IngestSourceRepository
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

# ---- 文件头（魔数）识别：内置表，不引第三方依赖 ----
#
# 取舍：内置表 vs ``filetype`` 库。选内置表——进程池里每个候选都要识别一次，
# 依赖越少冷启动越快；本项目只需要"是不是图片 / 音视频 / PDF"这一档粗粒度
# 判断，内置表够用，识别不出的边缘格式由 ``MAGIC_LAX`` 兜底成
# ``application/octet-stream``。
#
# 覆盖面（v1）：image/jpeg png gif webp heic avif；
#              video/mp4 quicktime x-matroska；
#              audio/mpeg flac wav；application/pdf。
#
# 表项 = (偏移, 签名, mime)，按顺序匹配，先具体后笼统：
# ``ftyp`` 品牌（HEIC / AVIF）必须排在通用 ``ftyp → video/mp4`` 之前。
_MAGIC_TABLE: tuple[tuple[int, bytes, str], ...] = (
    (0, b"\xff\xd8\xff", "image/jpeg"),
    (0, b"\x89PNG\r\n\x1a\n", "image/png"),
    (0, b"GIF87a", "image/gif"),
    (0, b"GIF89a", "image/gif"),
    (8, b"WEBP", "image/webp"),  # RIFF....WEBP
    (8, b"WAVE", "audio/wav"),  # RIFF....WAVE
    (4, b"ftypheic", "image/heic"),
    (4, b"ftypheix", "image/heic"),
    (4, b"ftyphevc", "image/heic"),
    (4, b"ftypheim", "image/heic"),
    (4, b"ftypheis", "image/heic"),
    (4, b"ftypmif1", "image/heic"),
    (4, b"ftypmsf1", "image/heic"),
    (4, b"ftypavif", "image/avif"),
    (4, b"ftypavis", "image/avif"),
    (4, b"ftypqt  ", "video/quicktime"),
    (4, b"ftyp", "video/mp4"),
    (0, b"\x1a\x45\xdf\xa3", "video/x-matroska"),  # EBML（mkv / webm）
    (0, b"ID3", "audio/mpeg"),
    (0, b"\xff\xfb", "audio/mpeg"),
    (0, b"\xff\xf3", "audio/mpeg"),
    (0, b"\xff\xf2", "audio/mpeg"),
    (0, b"fLaC", "audio/flac"),
    (0, b"%PDF", "application/pdf"),
)
"""内置魔数表；新增格式直接加表项，注意保持"先具体后笼统"的顺序。"""

# 读多少字节做识别：表内最大偏移 8，4096 留足余量给未来表项。
_MAGIC_HEADER_SIZE = 4096

HAS_MAGIC = True
"""魔数识别能力开关。

内置表恒可用，故恒为 True。保留该标志是为了让"缺失 → 降级为只信后缀"
这条路径可被测试/未来裁剪显式打开（置 False 后 ``MAGIC*`` 行为与
``SUFFIX`` 一致）。
"""


def detect_magic(path: Path) -> Optional[str]:
    """读文件头返回 mime；读失败或识别不出返回 ``None``。

    返回 ``None`` 覆盖两种语义（读失败 / 识别不出），由调用方策略区分：

    * ``MAGIC`` 模式     → 跳过（严格，候选落 SKIPPED）；
    * ``MAGIC_LAX`` 模式 → 接受但把 mime 降级为 ``application/octet-stream``。

    进程边界：只读文件前 ``_MAGIC_HEADER_SIZE`` 字节，不访问 DB、无模块级
    可变状态，可安全地在进程池里并发调用。
    """
    try:
        with open(path, "rb") as f:
            head = f.read(_MAGIC_HEADER_SIZE)
    except OSError as e:
        _logger.warning("读取文件头失败 %s: %s", path, e)
        return None

    if not head:
        return None

    for offset, signature, mime in _MAGIC_TABLE:
        end = offset + len(signature)
        if head[offset:end] == signature:
            return mime
    return None


@dataclass(frozen=True, slots=True)
class ProcessResult:
    """``process_candidate`` 的三态返回。

    取代旧契约 ``Optional[AssetCreateDTO]``：``None`` 同时表示"跳过"和
    "失败"，调用方无法区分（一律 ``mark_candidate_failed``）。三态语义：

    * ``"ok"``   —— 产出资产，``dto`` 必填，调用方走 ``finalize_candidate``；
    * ``"skip"`` —— 主动跳过（路径非法 / 魔数不认识），``reason`` 说明原因，
      候选落 ``SKIPPED``，**不算失败**；
    * ``"fail"`` —— 真失败（文件消失 / 读取异常），候选落 ``FAILED``。

    ``reason`` 只写日志与 ``ProcessResult``，**不落库**（候选表没有
    skip_reason 列）；UI 侧统一用 ``status=SKIPPED`` 识别"跳过"，
    "精判拒绝" vs "重复导入" 的细分靠日志里的 reason 区分。
    """

    status: Literal["ok", "skip", "fail"]
    dto: Optional[AssetCreateDTO] = None
    reason: Optional[str] = None

    @property
    def is_ok(self) -> bool:
        return self.status == "ok"


def _try_resolve(path: Path) -> Optional[Path]:
    """``Path.resolve()`` 的容错包装。

    :return: 绝对化后的 Path；``OSError``（权限 / 路径过长）或
        ``RuntimeError``（符号链接环）时返回 ``None``，由调用方当路径非法处理。
    """
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return None


def _resolve_candidate_path(
        candidate: AssetCandidateCacheDTO, source_path: str,
) -> Optional[Path]:
    """把 DB 里的 ``candidate.path`` 归一为可安全打开的绝对 Path。

    契约
    ----
    * 返回 ``None`` 表示路径非法（空串 / 越界 / 无法解析），调用方应置
      ``SKIPPED``（reason=path_invalid），不崩、不越权读；
    * 本函数 **不做 IO**，只做纯字符串 / Path 归一与越界判断，
      因此可以放心在进程池里调用；
    * 返回值只用于"读文件"，**不**写回 ``AssetCreateDTO.file_path``
      （后者始终沿用 ``candidate.path`` 原值）。Stage 1 自路径契约修复起
      写入的就是相对路径，因此新数据的 ``file_path`` 天然合规；历史绝对
      路径只在此处归一读取，不再回写。

    支持的历史脏数据形态（全部只在此处归一，本函数不改 DB；存量绝对路径
    不做迁移：项目仍处早期开发，老库删库重建）
    ----------------------------------------------------
    * POSIX 相对     ``a/b.jpg``
    * Windows 相对   ``a\\b.jpg``      （反斜杠统一换 ``/``）
    * 混合           ``a\\b/c.jpg``
    * POSIX 绝对     ``/abs/a.jpg``
    * Windows 绝对   ``C:/x.jpg`` / ``C:\\x.jpg``
    * ``file://`` URI ``file:///abs/a.jpg``（unquote 后取 path 部分）
    * 空串 / 纯空白  → ``None``

    越界防御
    --------
    ``source_id != 0``：归一后的路径 ``resolve()`` 必须落在
    ``source_path`` 之内，否则 ``None``。

    ``source_id == 0``（手动导入 / 虚拟根）：没有 source 根，按"绝对路径
    或 POSIX 根相对"处理，**不做**越界判断——与旧行为一致，否则手动导入
    会被误判为越界。
    """
    raw = (candidate.path or "").strip()
    if not raw:
        return None

    if raw.lower().startswith("file://"):
        raw = unquote(urlparse(raw).path or "").strip()
        if not raw:
            return None

    # 统一分隔符：DB 里可能残留 Windows 反斜杠。
    raw = raw.replace("\\", "/")

    if candidate.source_id == MANUAL_SOURCE_ID:
        p = Path(raw)
        if not p.is_absolute():
            # 手动导入的 POSIX 形态是"相对 '/'"，补根。
            p = Path("/") / raw.lstrip("/")
        return _try_resolve(p)

    if not source_path:
        return None

    root = _try_resolve(Path(source_path))
    if root is None:
        return None

    p = Path(raw)
    abs_path = _try_resolve(p if p.is_absolute() else root / raw)
    if abs_path is None:
        return None
    try:
        abs_path.relative_to(root)
    except ValueError:
        return None
    return abs_path


def process_candidate(
        candidate: AssetCandidateCacheDTO,
        source_path: str,
        type_check: FileTypeCheckMode = FileTypeCheckMode.SUFFIX,
) -> ProcessResult:
    """精判单个候选文件，返回三态 :class:`ProcessResult`。

    两段式中的 Stage 2（Stage 1 的 glob 粗筛在 ``SourceExecutor`` 里完成）：
    本函数只决定"要不要进一步验证、验证结果如何"，不重复做后缀筛选。

    ``type_check`` 策略
    -------------------
    * ``OFF``       —— 不校验，mime 用 ``mimetypes`` 猜；
    * ``SUFFIX``    —— 只信后缀（Stage 1 已保证），mime 用 ``mimetypes`` 猜；
    * ``MAGIC``     —— 读魔数，识别不出 → skip / magic_unknown；
    * ``MAGIC_LAX`` —— 读魔数，识别不出 → 接受，mime 降级为
      ``application/octet-stream``。

    ``type_check`` 由调用方（``ImportService``）从内存快照取得后作为参数传入
    —— 进程池里**不查 DB**，``AssetCandidateCacheDTO`` 也不为此加字段。

    路径语义（与 media_library_schema.sql 保持一致）
    ----------------------------------------------
    * ``candidate.path`` 的存储契约是"相对 source_path"；Stage 1
      （``SourceExecutor``）自本次修复起写入的就是相对路径。
    * 历史脏数据（绝对路径）仍由 :func:`_resolve_candidate_path` 兼容读取，
      但**不再回写绝对路径**：``assets.file_path`` 直接取 ``candidate.path``，
      所以新数据的 file_path 也是相对路径。
    * 项目仍处早期开发（``SCHEMA_VERSION = 0``），不提供数据迁移，老库
      直接删库重建：兼容分支保留作读取侧兜底，重建后自然不再命中。

    子进程契约：输入 / 输出均可 pickle，不访问 DB，无闭包捕获。
    """
    try:
        # 双保险 #1：纯字符串层拒绝 '..'（不依赖任何 IO / 平台语义）。
        if ".." in PurePosixPath(
                (candidate.path or "").replace("\\", "/"),
        ).parts:
            _logger.error("候选路径包含 '..'，拒绝处理: %s", candidate.path)
            return ProcessResult("skip", reason="path_invalid")

        abs_path = _resolve_candidate_path(candidate, source_path)
        if abs_path is None:
            _logger.error(
                "候选路径非法（空 / 越界 / 无法解析）: %r (source_id=%s, source=%r)",
                candidate.path, candidate.source_id, source_path,
            )
            return ProcessResult("skip", reason="path_invalid")

        if not abs_path.exists() or not abs_path.is_file():
            _logger.error("文件不存在或不是普通文件: %s", abs_path)
            return ProcessResult("fail", reason="missing")

        stat = abs_path.stat()
        guessed = candidate.mime_type or (
                mimetypes.guess_type(abs_path.name)[0] or "application/octet-stream"
        )

        check = coerce_enum(
            type_check, FileTypeCheckMode, FileTypeCheckMode.SUFFIX,
        )
        if check in (FileTypeCheckMode.OFF, FileTypeCheckMode.SUFFIX) or not HAS_MAGIC:
            # OFF / SUFFIX 都不读文件头；HAS_MAGIC=False 时 MAGIC* 降级为只信后缀。
            mime_type = guessed
        else:
            magic = detect_magic(abs_path)
            if magic is None:
                if check is FileTypeCheckMode.MAGIC:
                    _logger.info(
                        "魔数识别失败，跳过候选 %s (%s)", candidate.uuid, abs_path,
                    )
                    return ProcessResult("skip", reason="magic_unknown")
                mime_type = "application/octet-stream"
            else:
                mime_type = magic

        width = height = 0
        taken_at = None
        exif_json = None
        # EXIF 时机修正：按**精判后**的 mime 判断（旧实现看 candidate.mime_type，
        # 后缀是 .jpg 实为 PNG 时会漏读 —— 现在 mime 已经是 image/png）。
        if mime_type.startswith("image/") and HAS_EXIF:
            width, height, taken_at, exif_json = _extract_image_metadata(abs_path)

        return ProcessResult("ok", dto=AssetCreateDTO(
            uuid=candidate.uuid,
            # 直接沿用候选表的相对路径（自本次修复起 candidate.path 即相对
            # source_path 的 POSIX 路径）。历史绝对路径脏数据由
            # _resolve_candidate_path 兼容读取，但不会回写到 assets。
            file_path=candidate.path,
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
            is_deleted=False,
        ))
    except Exception as e:
        _logger.exception("处理候选 %s 失败: %s", candidate.uuid, e)
        return ProcessResult("fail", reason="exception")


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
    # 类属性 → 必须用 SignalDescriptor（lazy、实例间共享）。
    # 契约：cb(sender: ImportService, *, ok, failed, skipped, retry, timeout)
    assets_imported = SignalDescriptor("assets_imported")

    def __init__(
            self,
            repo: "ImportRepository",
            task: "TaskService",
            source_repo: "Optional[IngestSourceRepository]" = None,
    ):
        self._repo = repo
        self._task = task
        # 可选注入：用于每轮构建 {source_id: IngestSource} 内存快照，
        # 把 file_type_check 带给进程池里的 process_candidate。
        # 未注入时全部降级为 SUFFIX（只信后缀），行为与改造前一致。
        self._source_repo = source_repo

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
        # 每轮一份内存快照：把 file_type_check 随候选一起交给进程池，
        # 避免子进程查 DB，也不给候选表加 type_check 冗余列。
        source_map = self._load_source_map()

        batch_size = _cfg.batch_size

        totals = {"ok": 0, "failed": 0, "skipped": 0, "retry": 0, "timeout": 0}

        while not self._stop.is_set():
            candidates = self._repo.get_pending_candidates(limit=batch_size)
            if not candidates:
                break

            _logger.info("Processing batch of %d pending candidates", len(candidates))
            self._process_batch(candidates, source_paths, source_map, totals)

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
                # 契约：cb(sender, *, ok, failed, skipped, retry, timeout)
                self.assets_imported.emit(self, **totals)
            except Exception:
                _logger.exception("emit assets_imported failed")

    # ---- 批处理 -----------------------------------------------------------

    def _process_batch(
            self,
            candidates,
            source_paths,
            source_map: Dict[int, IngestSource],
            totals
    ) -> None:
        futures = {}
        for cand in candidates:
            source_path = source_paths.get(cand.source_id, "")
            # manual source（id=0）不在 get_source_candidates 里，
            # 以及快照缺失时，一律降级为 SUFFIX（只信后缀）——与改造前一致。
            source = source_map.get(cand.source_id)
            type_check = (
                source.file_type_check
                if source is not None
                else FileTypeCheckMode.SUFFIX
            )
            try:
                fut = self._task.submit(
                    process_candidate,
                    cand,
                    source_path,
                    type_check,
                    executor="process",
                    priority=5,
                )
                futures[fut] = cand
            except Exception:
                _logger.exception("提交候选 %s 失败", cand.uuid)
                self._safe_mark_failed(cand.id, "submit_failed")
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
                self._safe_mark_failed(cand.id, "timeout")
                totals["timeout"] += 1

    def _handle_result(self, cand, fut, totals) -> None:
        """回收单个候选的三态结果。

        分派规则（与 :class:`ProcessResult` 一一对应）：

        * ``skip``   → ``SKIPPED``（不是失败，计数进 skipped，reason 只写日志）；
        * ``fail``   → ``FAILED``（走 ``_safe_mark_failed``，依赖状态机放行
          ``PENDING → FAILED``——当前链路不 claim，候选就是 PENDING）；
        * ``ok``     → ``finalize_candidate``；返回 ``None`` 时再由候选终态区分
          ``SKIPPED``（唯一约束冲突 / 重复导入）与 ``FAILED``（真失败）。
        """
        try:
            result = fut.result()  # 已经 done 或 cancelled，不会阻塞
        except FutureTimeoutError:
            # 保留兜底：wait 循环之外的路径（例如未来有别的调用方）也会走这里。
            _logger.error("候选 %s 处理超时", cand.uuid)
            fut.cancel()
            self._safe_mark_failed(cand.id, "timeout")
            totals["timeout"] += 1
            return
        except Exception:
            _logger.exception("候选 %s 执行异常 (worker_exception)", cand.uuid)
            self._safe_mark_failed(cand.id, "worker_exception")
            totals["failed"] += 1
            return

        if not isinstance(result, ProcessResult):
            # 旧契约（Optional[AssetCreateDTO]）兜底：不应发生，但语义上
            # None 现在明确属于失败而不是"跳过"。
            _logger.error("候选 %s 返回了非 ProcessResult 结果: %r", cand.uuid, result)
            self._safe_mark_failed(cand.id, "bad_result")
            totals["failed"] += 1
            return

        if result.status == "skip":
            self._safe_mark_skipped(cand.id, result.reason or "unknown")
            totals["skipped"] += 1
            return

        if result.status == "fail":
            self._safe_mark_failed(cand.id, result.reason or "process_error")
            totals["failed"] += 1
            return

        # status == "ok"
        if result.dto is None:
            # 契约要求 ok 必带 dto；防御性降级为失败，避免 finalize 里抛 TypeError。
            _logger.error("候选 %s 返回 ok 但缺少 dto", cand.uuid)
            self._safe_mark_failed(cand.id, "no_payload")
            totals["failed"] += 1
            return

        self._finalize(cand, result.dto, totals)

    def _finalize(self, cand, asset_dto: AssetCreateDTO, totals: Dict[str, int]) -> None:
        try:
            asset_uuid = self._repo.finalize_candidate(cand.id, asset_dto)
        except Exception:
            # 临时 DB 错误：不动候选状态，下一轮重新取到后重试。
            # 若反复失败，可由 repo 侧根据 retry 计数做退避/上限。
            _logger.exception(
                "落库候选 %s 时 DB 异常，保持 pending 待重试", cand.uuid,
            )
            totals["retry"] += 1
            return

        if asset_uuid:
            totals["ok"] += 1
            _logger.debug("候选 %s → asset_uuid=%s", cand.uuid, asset_uuid)
            return

        # repo 判定为永久失败（唯一约束冲突 → skipped，其余 → failed）。
        # get_candidate_status 返回 CandidateStatus 枚举，必须与枚举比较，
        # 不能拿字符串字面量比较（否则永远走 failed 分支）。
        status = self._repo.get_candidate_status(cand.id)
        if status is CandidateStatus.SKIPPED:
            totals["skipped"] += 1
            _logger.info("候选 %s 判定为重复资产，已跳过", cand.uuid)
        else:
            totals["failed"] += 1
            _logger.error("插入资产失败: 候选 %s status=%s", cand.uuid, status)

    def _safe_mark_failed(self, candidate_id, reason: str) -> None:
        """把候选置为 FAILED；失败只记日志，不再向上抛。

        与 :meth:`_safe_mark_skipped` 对称：走 repo 的 ``mark_candidate_failed``
        （状态机已放行 ``PENDING → FAILED`` 与 ``PROCESSING → FAILED``；
        当前链路不 claim，候选是 PENDING，改前缺少这条转移会让失败候选
        永远停在 pending、被下一轮反复捞出重试）。

        ``reason`` 只落日志——本任务不新增 DB 列。
        """
        try:
            self._repo.mark_candidate_failed(candidate_id)
            _logger.warning(
                "candidate %s failed (status=failed, reason=%s)",
                candidate_id, reason,
            )
        except Exception:
            _logger.exception(
                "mark failed 也失败，候选停在原状态 "
                "(recover_stale_candidates 会在超时后回收) cand=%s reason=%s",
                candidate_id, reason,
            )

    def _safe_mark_skipped(self, candidate_id, reason: str) -> None:
        """把候选置为 SKIPPED；失败时降级到 FAILED 并记日志。

        走 repo 的 ``update_candidate_status``（状态机已放行
        ``PENDING → SKIPPED`` 与 ``PROCESSING → SKIPPED``）。

        ``skip_reason`` **只落日志**——候选表没有 skip_reason 列，本次也不加。
        UI 侧统一用 ``status=SKIPPED`` 识别"跳过"，与"重复导入"
        （finalize 内部置 SKIPPED）在日志层面用 reason 区分。
        """
        try:
            self._repo.update_candidate_status(candidate_id, CandidateStatus.SKIPPED)
            _logger.info(
                "candidate %s skipped (status=skipped, reason=%s)",
                candidate_id, reason,
            )
        except Exception:
            _logger.exception("标记候选 %s 为跳过失败，降级为失败", candidate_id)
            try:
                self._repo.mark_candidate_failed(candidate_id)
            except Exception:
                _logger.exception("降级 mark failed 也失败 cand=%s", candidate_id)

    def _load_source_map(self) -> Dict[int, IngestSource]:
        """构建 ``{source_id: IngestSource}`` 内存快照（每轮一次）。

        为什么不在候选表里冗余 type_check 列：``candidate.source_id`` 已能
        反查到源配置，内存快照足够；进程池里不查 DB，所以 type_check 只能在
        **提交任务时**从快照取出，作为参数传给 ``process_candidate``。

        取不到（未注入 source_repo / DB 抖动）时返回空 dict，调用侧
        fallback 到 ``SUFFIX``，不影响导入主流程。
        """
        if self._source_repo is None:
            return {}
        try:
            return {s.id: s for s in self._source_repo.get_source_candidates()}
        except Exception:
            _logger.exception(
                "加载 source 快照失败，本轮 type_check 全部降级为 suffix",
            )
            return {}
