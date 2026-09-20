#
"""Import service: 从候选缓存表读取 pending 记录，提交给 TaskService 的进程池处理，主线程回收并落库。"""

import hashlib
import json
import mimetypes
from datetime import datetime
from logging import getLogger
from pathlib import Path
from typing import List, Optional, TYPE_CHECKING

from albuswall.dto.import_ import AssetCandidateCacheDTO, AssetCreateDTO
from albuswall.log import TRACE

try:
    # noinspection PyUnusedImports
    from PIL import Image
    # noinspection PyUnusedImports
    from PIL.ExifTags import TAGS
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

if TYPE_CHECKING:
    from albuswall.repositories.import_ import ImportRepository
    from albuswall.log import Logger
    from .task import TaskService

_logger = getLogger(__name__)
_logger: "Logger"
_logger.trace = lambda msg, *args: _logger.log(TRACE, msg, *args)


# ============================================================
# 纯函数层：在子进程中执行，无状态、无 DB 连接、可 pickle
# ============================================================

def process_candidate(candidate: AssetCandidateCacheDTO) -> Optional[AssetCreateDTO]:
    """处理单个候选文件，返回 AssetCreateDTO 或 None。

    子进程契约：
    - 输入 / 输出都是可 pickle 的 DTO
    - 不访问 DB、不持有 logger 之外的共享资源
    - 顶层函数，无闭包捕获
    """
    try:
        path = Path(candidate.path)
        if not path.is_absolute():
            _logger.error("候选路径不是绝对路径: %s", path)
            return None
        if not path.exists() or not path.is_file():
            _logger.error("文件不存在或不是普通文件: %s", path)
            return None

        stat = path.stat()
        mime_type = candidate.mime_type or (
            mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
        )

        width = height = 0
        taken_at = None
        exif_json = None
        if mime_type.startswith('image/') and HAS_PIL:
            width, height, taken_at, exif_json = _extract_image_metadata(path)

        return AssetCreateDTO(
            uuid=candidate.uuid,
            file_path=str(path),
            original_name=path.name,
            mime_type=mime_type,
            file_hash=_compute_file_hash(path),
            file_size=stat.st_size,
            width=width,
            height=height,
            source_id=candidate.source_id,
            thumb_path=None,
            thumb_small_path=None,
            thumb_medium_path=None,
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
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(4096), b''):
            sha256.update(chunk)
    return sha256.hexdigest()


def _extract_image_metadata(path: Path):
    """返回 (width, height, taken_at, exif_json)；失败返回 (0, 0, None, None)。"""
    width = height = 0
    taken_at = exif_json = None
    try:
        with Image.open(path) as img:
            width, height = img.size
            exif_data = img.getexif()
            if exif_data:
                exif_dict = {
                    TAGS.get(tag_id, tag_id): str(value)
                    for tag_id, value in exif_data.items()
                }
                exif_json = json.dumps(exif_dict, ensure_ascii=False)

                datetime_str = exif_data.get(36867)  # DateTimeOriginal
                if datetime_str:
                    try:
                        taken_at = datetime.strptime(
                            datetime_str, '%Y:%m:%d %H:%M:%S'
                        ).isoformat()
                    except ValueError:
                        pass
    except Exception as e:
        _logger.warning("无法读取图像信息 %s: %s", path, e)
    return width, height, taken_at, exif_json


# ============================================================
# 服务层：主线程调度 + 主线程落库
# ============================================================

class ImportService:
    """处理 asset_candidate_cache 表中所有 pending 候选。

    职责：
    1. 取待处理候选
    2. 逐条提交为 process 任务（并发由 TaskService 统一控制）
    3. 主线程回收结果并落库
    """

    def __init__(self, repo: "ImportRepository", task: "TaskService"):
        self._repo = repo
        self._task = task

    def start(self) -> None:
        # recover
        count = self._repo.recover_stale_candidates()
        _logger.debug("Trying to restore the task status after a forced interrupt, "
                      "successfully recovered %d so far", count)

        candidates = self._repo.get_pending_candidates()
        if not candidates:
            _logger.info("No pending candidates to process.")
            return

        _logger.info("Found %d pending candidates to process.", len(candidates))

        # 1. 全部提交为 process 任务
        submitted: List[tuple] = []
        for cand in candidates:
            fut = self._task.submit(
                process_candidate,       # 顶层纯函数
                cand,                    # 可 pickle 的 DTO
                executor="process",
                priority=5,
            )
            submitted.append((cand, fut))

        # 2. 主线程串行回收 + 落库（避免子进程并发写 SQLite）
        ok = failed = 0
        for cand, fut in submitted:
            # noinspection PyBroadException
            try:
                asset_dto = fut.result(timeout=600)
            except Exception:
                _logger.exception("候选 %s 执行异常", cand.uuid)
                self._repo.mark_candidate_failed(cand.id)
                failed += 1
                continue

            if asset_dto is None:
                self._repo.mark_candidate_failed(cand.id)
                failed += 1
                continue

            asset_id = self._repo.finalize_candidate(cand.id, asset_dto)
            if asset_id:
                ok += 1
                _logger.debug("候选 %s → asset_id=%s", cand.uuid, asset_id)
            else:
                failed += 1
                _logger.error("插入资产失败: 候选 %s", cand.uuid)

        _logger.info("Import finished. ok=%d, failed=%d", ok, failed)
