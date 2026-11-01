#
"""导入服务的**纯函数层**：在子进程里跑，无状态、无 DB 连接、可 pickle。

为什么单独成模块（而不是和 ``ImportService`` 放一起）
----------------------------------------------------
这一层的要求和 worker 完全相反：

* 可以被 ``TaskService`` 投到 **process 执行器** —— 因此不能是 lambda /
  局部函数 / 绑定方法，模块级参数必须可 pickle；
* 不能碰 DB、不能碰日志之外的全局状态（子进程里没有容器、没有连接池）；
* 不需要任何服务对象，也不该 import 仓储 / 容器（``import_.py`` 的
  ``TYPE_CHECKING`` 段就是这条边界的痕迹）。

放同一个文件时，这两个"能不能碰 DB"的语义只能靠读者自己分辨；拆开后
本模块的 import 列表就足以证明它没有 DB 依赖。

两段式文件类型判断的第二段
--------------------------
Stage 1 的 glob 粗筛在 ``service/source/runtime/executor.py``（``SourceExecutor``）；
本模块的 :func:`process_candidate` 是 Stage 2 精判，返回三态
:class:`ProcessResult`（``ok`` / ``skip`` / ``fail``），取代旧的二态
``Optional[AssetCreate]``（``None`` 同时表示"跳过"和"失败"，调用方无法区分）。
"""

from __future__ import annotations

import hashlib
import mimetypes
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Literal, Optional
from urllib.parse import unquote, urlparse

from albuswall.common.enums import FileTypeCheckMode
from albuswall.dto.import_ import AssetCandidateCache, AssetCreate
from albuswall.dto.source.query import MANUAL_SOURCE_ID
from albuswall.dto.utils import coerce_enum
from albuswall.log import getLogger

if TYPE_CHECKING:  # 只为类型标注
    pass

try:
    # 统一 EXIF 解析入口；缺失时不影响导入主流程。
    # 约定：返回 dict，键包括 width / height / taken_at / exif_json。
    from albuswall.repositories.utils.exif import (
        extract_image_metadata as _shared_extract_image_metadata,
    )

    HAS_EXIF = True
except ImportError:  # pragma: no cover - 环境缺依赖时的降级路径
    _shared_extract_image_metadata = None  # type: ignore[assignment]
    HAS_EXIF = False

__all__ = [
    "HAS_EXIF",
    "HAS_MAGIC",
    "ProcessResult",
    "detect_magic",
    "process_candidate",
]

_logger = getLogger(__name__)


# ============================================================
# 文件头（魔数）识别：内置表，不引第三方依赖
# ============================================================
#
# 取舍：内置表 vs ``filetype`` 库。选内置表——进程池里每个候选都要识别一次，
# 依赖越少冷启动越快；本项目只需要"是不是图片 / 音视频 / PDF"这一档粗粒度
# 判断，识别不出的边缘格式由 ``MAGIC_LAX`` 兜底成 application/octet-stream。
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
        _logger.warning("Failed to read file header %s: %s", path, e)
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

    三态语义：

    * ``"ok"``   —— 产出资产，``dto`` 必填，调用方走 ``finalize_candidate``；
    * ``"skip"`` —— 主动跳过（路径非法 / 魔数不认识），``reason`` 说明原因，
      候选落 ``SKIPPED``，**不算失败**；
    * ``"fail"`` —— 真失败（文件消失 / 读取异常），候选落 ``FAILED``。

    ``reason`` 只写日志与 ``ProcessResult``，**不落库**（候选表没有
    skip_reason 列）；UI 侧统一用 ``status=SKIPPED`` 识别"跳过"，
    "精判拒绝" vs "重复导入" 的细分靠日志里的 reason 区分。
    """

    status: Literal["ok", "skip", "fail"]
    dto: Optional[AssetCreate] = None
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
        candidate: AssetCandidateCache, source_path: str,
) -> Optional[Path]:
    """把 DB 里的 ``candidate.path`` 归一为可安全打开的绝对 Path。

    契约
    ----
    * 返回 ``None`` 表示路径非法（空串 / 越界 / 无法解析），调用方应置
      ``SKIPPED``（reason=path_invalid），不崩、不越权读；
    * 本函数 **不做 IO**，只做纯字符串 / Path 归一与越界判断，
      因此可以放心在进程池里调用；
    * 返回值只用于"读文件"，**不**写回 ``AssetCreate.file_path``
      （后者始终沿用 ``candidate.path`` 原值）。

    支持的脏数据形态（只在此处归一，不改 DB）
    ----------------------------------------
    * POSIX 相对 ``a/b.jpg`` / Windows 相对 ``a\\b.jpg``（反斜杠统一换 ``/``）
    * POSIX 绝对 ``/abs/a.jpg`` / Windows 绝对 ``C:/x.jpg``
    * ``file://`` URI（unquote 后取 path 部分）
    * 空串 / 纯空白 → ``None``

    越界防御
    --------
    ``source_id != 0``：归一后的路径必须落在 ``source_path`` 之内；
    ``source_id == 0``（手动导入 / 虚拟根）没有源根，按"绝对路径或 POSIX
    根相对"处理，**不做**越界判断 —— 否则手动导入会被误判为越界。
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
        candidate: AssetCandidateCache,
        source_path: str,
        type_check: FileTypeCheckMode = FileTypeCheckMode.SUFFIX,
) -> ProcessResult:
    """精判单个候选文件，返回三态 :class:`ProcessResult`。

    Stage 2（Stage 1 的 glob 粗筛在 ``SourceExecutor`` 里完成）：本函数只
    决定"要不要进一步验证、验证结果如何"，不重复做后缀筛选。

    ``type_check`` 策略
    -------------------
    * ``OFF``       —— 不校验，mime 用 ``mimetypes`` 猜；
    * ``SUFFIX``    —— 只信后缀（Stage 1 已保证），mime 用 ``mimetypes`` 猜；
    * ``MAGIC``     —— 读魔数，识别不出 → skip / magic_unknown；
    * ``MAGIC_LAX`` —— 读魔数，识别不出 → 接受，mime 降级为
      ``application/octet-stream``。

    ``type_check`` 由调用方（``ImportService``）从内存快照取得后作为参数传入
    —— 进程池里**不查 DB**，``AssetCandidateCache`` 也不为此加字段。

    路径语义（与 media_library_schema.sql 一致）
    ------------------------------------------
    ``candidate.path`` 的存储契约是"相对 source_path"，Stage 1 写入的就是
    相对路径；``assets.file_path`` 直接取 ``candidate.path``。历史绝对路径
    脏数据由 :func:`_resolve_candidate_path` 兼容**读取**，但不再回写。

    子进程契约：输入 / 输出均可 pickle，不访问 DB，无闭包捕获。
    """
    try:
        # 双保险 #1：纯字符串层拒绝 '..'（不依赖任何 IO / 平台语义）。
        if ".." in PurePosixPath(
                (candidate.path or "").replace("\\", "/"),
        ).parts:
            _logger.error(
                "Candidate path contains '..'; refusing to process: %s",
                candidate.path,
            )
            return ProcessResult("skip", reason="path_invalid")

        abs_path = _resolve_candidate_path(candidate, source_path)
        if abs_path is None:
            _logger.error(
                "Invalid candidate path (empty / out of bounds / "
                "unresolvable): %r (source_id=%s, source=%r)",
                candidate.path, candidate.source_id, source_path,
            )
            return ProcessResult("skip", reason="path_invalid")

        if not abs_path.exists() or not abs_path.is_file():
            _logger.error(
                "File does not exist or is not a regular file: %s", abs_path
            )
            return ProcessResult("fail", reason="missing")

        stat = abs_path.stat()
        guessed = candidate.mime_type or (
                mimetypes.guess_type(abs_path.name)[0]
                or "application/octet-stream"
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
                        "Magic-number detection failed; skipping candidate %s (%s)",
                        candidate.uuid,
                        abs_path,
                    )
                    return ProcessResult("skip", reason="magic_unknown")
                mime_type = "application/octet-stream"
            else:
                mime_type = magic

        width = height = 0
        taken_at = None
        exif_json = None
        # EXIF 时机：按**精判后**的 mime 判断（看 candidate.mime_type 会漏读
        # 后缀是 .jpg 实为 PNG 的文件 —— 现在 mime 已经是 image/png）。
        if mime_type.startswith("image/") and HAS_EXIF:
            width, height, taken_at, exif_json = _extract_image_metadata(abs_path)

        return ProcessResult("ok", dto=AssetCreate(
            uuid=candidate.uuid,
            # 直接沿用候选表的相对路径（相对 source_path 的 POSIX 路径）。
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
    except Exception as e:  # noqa: BLE001 —— 单个候选的意外不该炸掉整批
        _logger.exception("Failed to process candidate %s: %s", candidate.uuid, e)
        return ProcessResult("fail", reason="exception")


def _compute_file_hash(path: Path) -> str:
    sha256 = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


def _extract_image_metadata(path: Path):
    """统一走 ``repositories/utils/exif.py`` 的解析入口。

    :return: ``(width, height, taken_at, exif_json)``；任何失败返回
        ``(0, 0, None, None)``。

    契约：本函数保证不抛异常——上游返回脏数据（如 ``width="abc"``）也只降级，
    不让单个坏字段把整个候选判为失败。
    """
    if not HAS_EXIF or _shared_extract_image_metadata is None:
        return 0, 0, None, None

    try:
        meta = _shared_extract_image_metadata(path) or {}
    except Exception as e:  # noqa: BLE001
        _logger.warning("Failed to read image metadata %s: %s", path, e)
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
        # ``AssetCreate.taken_at`` 语义是 ISO8601 字符串；上游给 datetime
        # 是常见误用，容忍并转字符串。
        taken_at = str(taken_at)

    exif_json = meta.get("exif_json")
    if exif_json is not None and not isinstance(exif_json, str):
        # 契约要求 JSON 字符串；脏数据一律丢弃，不要塞进 DB。
        _logger.warning(
            "exif_json has unexpected type %s (%s); dropped",
            type(exif_json).__name__, path,
        )
        exif_json = None

    return width, height, taken_at, exif_json
