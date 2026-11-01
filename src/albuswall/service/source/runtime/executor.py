#
"""``SourceExecutor`` —— 扫描单个源的执行体（可调用对象，交给 TaskService）。

为什么它是"任务"而不是"服务"
-----------------------------
``SourceExecutor`` 被 :class:`~albuswall.service.source.runtime.service.SourceRuntime`
构造，然后**整个对象**作为 ``fn`` 提交给 ``TaskService``：它实现
``__call__``，自带 ``seq``（同时用作 ``correlation_id``）与源快照，
不需要再包一层 DTO。

线程归属
--------
``__call__`` 可能跑在 ``TaskService`` 的工作线程 / 进程池里，因此：

* 它**只读**源快照与 ``ImportRepository``，从不碰运行时的任务表 /
  跟踪集（那两处归 runtime 的主线程所有）；
* 唯一的向外通信是 ``finished_signal``（``scan_finished``）：payload 是
  不可变的 :class:`SourceScanFinished`；
* 订阅者抛出的异常就地接住 —— 扫描本身已经成功，让 UI 回调的异常冒出去
  会被 TaskService 记成"任务失败"，属于错误归因。

``finished_signal`` 由 runtime 构造时注入（本模块**不** import
``runtime.service``，避免环）。
"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from traceback import format_exc
from typing import Callable, List, Optional
from uuid import uuid4

from albuswall.dto.import_ import AssetCandidateCache
from albuswall.dto.source.events import SourceScanFinished
from albuswall.dto.source.query import IngestSource
from albuswall.log import getLogger
from albuswall.repositories.import_ import ImportRepository
from albuswall.utils.path import iter_files_depth_first
from albuswall.utils.signals import Signal

from .scanning import (
    GlobMatcher,
    SCAN_BATCH_SIZE,
    chunked,
    normalize_file_globs,
)

__all__ = ["SourceExecutor"]

_logger = getLogger(__name__)


class SourceExecutor:
    """扫描单个 source，把新发现的文件写入候选缓存。"""

    def __init__(
            self,
            source: IngestSource,
            repo: ImportRepository,
            seq: int,
            finished_signal: Optional[Signal] = None,
            is_source_active: Optional[Callable[[int], bool]] = None,
    ):
        self.source = source
        self.repo = repo
        self.seq = seq
        self._finished_signal = finished_signal
        # 源存活探针（``IngestSourceRepository.exists``）。任务从提交到真正
        # 执行之间，源可能已被回收站删除：扫下去只会往已消失的 source_id
        # 塞候选并撞外键，所以开扫前先问一句。``None`` 表示调用方不提供
        # 该能力（老测试替身 / 无 DB 场景）。
        self._is_source_active = is_source_active

    # ------------------------------------------------------------------ 快照
    @property
    def source_id(self) -> int:
        return self.source.id

    def update_source(self, source: IngestSource) -> None:
        """原地替换本任务持有的源快照（runtime 的 ``snapshot`` 分支专用）。

        纯展示字段（``title`` / ``description`` / ``tags`` / ``trigger_config``）
        变化时**不重建任务**：``seq`` 不变、不重提，只让在途 / 已排队的任务
        看到新快照。扫描行为字段（``source_path`` / ``file_types`` …）变化
        必须走 ``rebuild``，否则新配置要等下一轮才生效。

        只应由 runtime 在主线程调用；执行体本人在扫描时只读该字段。
        """
        self.source = source
        _logger.trace(
            "Updated snapshot of scan task source=%d seq=%d",
            self.source.id, self.seq,
        )

    # ------------------------------------------------------------------ 日志
    def describe(self, *, verbose: bool = False) -> str:
        base = (
            f"SourceExecutor(source_id={self.source.id}, "
            f"seq={self.seq}, "
            f"path={self.source.source_path!r})"
        )
        if verbose:
            base = base[:-1] + (
                f", target={self.source.target_path!r}, "
                f"mount_point={self.source.mount_point!r})"
            )
        return base

    def __str__(self) -> str:
        return self.describe()

    def __repr__(self) -> str:
        return self.describe()

    def __format__(self, spec: str) -> str:
        if spec == "":
            return self.describe()
        if spec in ("v", "verbose"):
            return self.describe(verbose=True)
        raise ValueError(f"Unsupported format spec: {spec!r}")

    # ------------------------------------------------------------------ 执行
    def __call__(self) -> None:
        result: Optional[SourceScanFinished] = None
        try:
            result = self.run()
        except Exception as exc:
            err = format_exc()
            _logger.error("Scan failed for source %d: %s", self.source.id, exc)
            _logger.trace("Traceback for source %d:\n%s", self.source.id, err)
            result = self._failed(err)
        finally:
            # 无论成功 / 失败 / 无新内容，均发出一次完成信号，
            # 保证上层能可靠收到 "该 source 已处理完毕" 的通知。
            # 订阅者异常必须就地接住：扫描本身已经成功（缓存已写入），
            # 让 UI 回调的异常从这里冒出去会被 TaskService 的 done 回调
            # 记成任务失败，属于错误归因。
            if self._finished_signal is not None and result is not None:
                try:
                    # payload 必须具名：cb(sender, *, event)
                    self._finished_signal.emit(self, event=result)
                except Exception as exc:
                    _logger.error(
                        "scan_finished signal raised for source %d: %s",
                        self.source.id, exc,
                    )

    def run(self) -> SourceScanFinished:
        _logger.debug("Scanning %s", self)

        # 源可能在任务排队期间被删除（回收站软删 → 异步 purge）。这时扫描
        # 毫无意义：候选会指向一个已经不存在 / 已标记删除的 source_id。
        # 直接跳过，而不是让外键约束去报错。
        if not self._source_is_active():
            _logger.debug(
                "Source %d is no longer active (deleted / purged); "
                "skip scan (seq=%d)",
                self.source.id, self.seq,
            )
            return self._skipped()

        source_path = Path(self.source.source_path).resolve()
        if not source_path.exists():
            _logger.error(
                "Source path does not exist: source_id=%d path=%s",
                self.source.id, source_path,
            )
            return self._failed(f"Source path does not exist: {source_path}")

        # Stage 1 粗筛：file_types 语义是 glob 列表（历史 "jpg" / ".jpg" /
        # "*.jpg" 三种写法经 normalize_file_globs 归一）。
        # 显式空规则 = 该源不导入任何文件（与改造前 get_allowed_extensions()
        # 返回空集合时"一个都不匹配"的行为一致）。
        globs = normalize_file_globs(self.source.file_types)
        if not globs:
            _logger.debug(
                "Source %d has no file_types rule; nothing will be imported",
                self.source.id,
            )
        else:
            _logger.trace(
                "File globs for source %d: %s", self.source.id, globs,
            )

        rel_paths = self._collect_candidate_paths(source_path, globs)
        total_files = len(rel_paths)
        _logger.debug(
            "Source %d: matched %d file(s) under %s",
            self.source.id, total_files, source_path,
        )

        missing_paths = self._find_missing_paths(rel_paths)
        missing_count = len(missing_paths)
        _logger.debug(
            "Source %d: %d/%d file(s) are new",
            self.source.id, missing_count, total_files,
        )

        inserted_count = self._insert_candidate_cache(missing_paths)

        return SourceScanFinished(
            source_id=self.source.id,
            task_seq=self.seq,
            scanned_file_count=total_files,
            new_file_count=missing_count,
            has_new_content=missing_count > 0,
            inserted_count=inserted_count,
            error=None,
        )

    # ------------------------------------------------------------------ 内部
    def _skipped(self) -> SourceScanFinished:
        """任务被主动跳过（源已失效）：既非成功也非失败。"""
        return SourceScanFinished(
            source_id=self.source.id,
            task_seq=self.seq,
            scanned_file_count=0,
            new_file_count=0,
            has_new_content=False,
            inserted_count=0,
            error=None,
            skipped=True,
        )

    def _source_is_active(self) -> bool:
        """源是否仍可写候选；未注入探针时视为有效（兼容测试替身）。"""
        if self._is_source_active is None:
            return True
        try:
            return bool(self._is_source_active(self.source.id))
        except Exception as exc:  # noqa: BLE001
            # 探针只是"提前止损"，查不到/查炸了不应让扫描失败：
            # 真出问题还有 create_candidate_cache_batch 的外键剔除兜底。
            _logger.warning(
                "source liveness probe failed for source %d: %s",
                self.source.id, exc,
            )
            return True

    def _failed(self, reason: str) -> SourceScanFinished:
        return SourceScanFinished(
            source_id=self.source.id,
            task_seq=self.seq,
            scanned_file_count=0,
            new_file_count=0,
            has_new_content=False,
            error=reason,
        )

    @staticmethod
    def _collect_candidate_paths(
            source_path: Path, globs: List[str]
    ) -> List[str]:
        """Stage 1 粗筛：收集匹配任一 glob 的文件路径（相对 source_path）。

        为什么是"零 IO 字符串匹配"：候选数量可能是几万，逐个开文件读内容
        不现实；这里只遍历目录 + 纯字符串比较，把"要不要精判"的决定留给
        Stage 2 的 ``process_candidate``。

        返回值契约
        ----------
        每一项都是**相对 ``source_path`` 的 POSIX 路径**（如 ``sub/a.jpg``），
        与 ``asset_candidate_cache.path`` / ``assets.file_path`` 的存储契约
        一致（见 ``media_library_schema.sql`` 文件头「路径语义」）。

        :param globs: 已经过 ``normalize_file_globs`` 的 glob 列表；
            空列表 → 返回空（显式空规则 = 不导入）。
        """
        if not globs:
            return []

        matcher = GlobMatcher(globs)
        if not matcher:
            return []

        root = source_path.resolve()
        result: List[str] = []
        for file_path in iter_files_depth_first(source_path, include_dirs=False):
            abs_fp = file_path if file_path.is_absolute() else root / file_path
            try:
                abs_fp = abs_fp.resolve()
                rel = abs_fp.relative_to(root).as_posix()
            except (OSError, RuntimeError, ValueError):
                # 防御：遍历根之外的符号链接 / 解析失败 —— 不导入也不崩。
                _logger.trace("Skipping unresolvable path: %s", file_path)
                continue
            if not matcher.matches(rel):
                _logger.trace("Skipping non-matched file: %s", rel)
                continue
            result.append(rel)
        return result

    def _find_missing_paths(self, rel_paths: List[str]) -> List[str]:
        """分批在数据库中查询缺失（尚未导入）的相对路径。

        入参与 ``asset_candidate_cache.path`` 的存储形式一致（相对
        ``source_path``），否则 ``find_missing_paths`` 的等值匹配会全部落空。
        """
        missing: List[str] = []
        for batch in chunked(rel_paths, SCAN_BATCH_SIZE):
            found = self.repo.find_missing_paths(self.source.id, *batch)
            _logger.trace(
                "Missing lookup batch: source=%d queried=%d missing=%d",
                self.source.id, len(batch), len(found),
            )
            missing.extend(found)
        return missing

    def _insert_candidate_cache(self, missing_rel_paths: List[str]) -> int:
        """把缺失的相对路径转换为候选 DTO，分批写入候选缓存。

        入参 ``missing_rel_paths`` 是相对 ``source_path`` 的 POSIX 路径，
        原样写入 ``asset_candidate_cache.path``（不做 ``str(Path(p))``
        这种形态转换，避免把契约偷偷改回平台原生形态）。

        仓储会在事务内剔除"源已消失"的候选（见
        ``ImportRepository.create_candidate_cache_batch``），因此这里返回
        的是**仓储实际写入**的行数，而不是候选总数。

        Returns:
            实际提交插入的候选记录数。
        """
        if not missing_rel_paths:
            return 0

        candidates = [
            AssetCandidateCache(
                uuid=uuid4(),
                path=p,
                source_id=self.source.id,
                mime_type=(
                        mimetypes.guess_type(p)[0]
                        or "application/octet-stream"
                ),
            )
            for p in missing_rel_paths
        ]

        inserted = 0
        for batch in chunked(candidates, SCAN_BATCH_SIZE):
            inserted += self.repo.create_candidate_cache_batch(batch)
            _logger.trace(
                "Inserted candidate batch: source=%d size=%d",
                self.source.id, len(batch),
            )

        _logger.debug(
            "Source %d: cached %d/%d candidate(s)",
            self.source.id, inserted, len(candidates),
        )
        return inserted
