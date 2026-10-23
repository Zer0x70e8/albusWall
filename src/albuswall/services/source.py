#
"""SourceService：导入源扫描与调度服务。

设计要点
--------
* **单类 + 单 state**：原先 ``SourceServiceWorker`` 持有一半状态
  （``_sources`` / ``_persist_tasks_by_source``），``SourceFacedService``
  持有另一半（``_submitted_seqs``），任何一处更新路径都要同时改两个对象，
  否则就会出现"看得到源但看不到任务"或反之。现在所有可变状态收敛到
  :class:`SourceServiceState`，:class:`SourceService` **只持有该对象的引用**。

* **不做跨线程同步**：state 容器本身不加锁。若调用方从多线程访问，
  请自行串行化（例如统一走事件循环，或外部加锁）。这与旧实现一致，
  只是把"要同步哪几块"从隐式知识变成了显式的对象。

  唯一的异步来源是 TaskService 的 done 回调（工作线程）：它**不**直接写
  state，只把 seq 记进 ``SourceService._settled_seqs``（自带锁），由主线程
  的 ``_drain_settled`` 统一回收 —— 所以 observe 回调始终在属主线程触发。

* **约定**：
    - 读模型统一为 :class:`IngestSource`（``IngestSourceSyncCandidate`` /
      ``IngestSourceViewDTO`` 是它的过渡别名）；
    - PATCH 语义走 ``UNSET`` / ``None`` 二分（见 ``dto.sentinel``）；
    - 状态字面量走枚举，禁止裸字符串；
    - "是否启用"是 DB 列（``ingest_source.disabled``），不再有内存 set 副本。
"""

from __future__ import annotations

import mimetypes
import threading
from concurrent.futures import Future
from itertools import count as _count
from pathlib import Path, PurePosixPath
from pprint import pformat
from traceback import format_exc
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    TYPE_CHECKING,
    TypeVar,
)
from uuid import uuid4

from albuswall.repositories import (
    IngestSourceRepository,
    ImportRepository,
)
from albuswall.repositories.import_ import MAX_PATHS_PER_QUERY
from albuswall.configue.state import (
    ObservableNamespace,
    StateField,
    ObservableContext
)
from albuswall.log import getLogger
from albuswall.common.enums import ExecutorType
from albuswall.common.exceptions import (
    BackpressureError,
    TaskServiceShutdownError,
)
from albuswall.dto.import_ import AssetCandidateCacheDTO
from albuswall.dto.source import (
    MANUAL_SOURCE_ID,
    IngestSource,
    IngestSourceCreate,
    IngestSourceUpdate,
    SourceScanFinished,
)
from albuswall.utils.signals import Signal, SignalDescriptor
from albuswall.utils.mount import auto_mount
from albuswall.utils.path import iter_files_depth_first

if TYPE_CHECKING:
    from albuswall.infrastructure.task import TaskService

_logger = getLogger(__name__)

_SCAN_BATCH_SIZE = 500

# 批次上限必须落在 SQLite 单条查询的变量上限之内：
# ``ImportRepository.find_missing_paths`` 的 SQL 固定占用 1 个变量
# （source_id），因此单批最多能带 ``MAX_PATHS_PER_QUERY`` 个 path 占位符。
# 调大 ``_SCAN_BATCH_SIZE`` 前先看这里，否则会在运行期抛 ValueError。
assert _SCAN_BATCH_SIZE <= MAX_PATHS_PER_QUERY, (
    f"_SCAN_BATCH_SIZE ({_SCAN_BATCH_SIZE}) must be <= "
    f"MAX_PATHS_PER_QUERY ({MAX_PATHS_PER_QUERY})"
)

# 扫描任务的固定调度参数（原来是内联在 ``Task(priority=8, executor=THREAD)``
# 里的；``Task`` DTO 删掉后集中到这里，避免魔法数散落）。
_SCAN_TASK_PRIORITY = 8
_SCAN_TASK_EXECUTOR = ExecutorType.THREAD
_T = TypeVar("_T")

# ---------------------------------------------------------------------------
# Stage 1 粗筛：file_types 的 glob 语义
# ---------------------------------------------------------------------------
# ``file_types`` 在 DB 里仍是 JSON 字符串数组，语义从"后缀集合"升级为
# "glob 列表"：读取时归一（"jpg" / ".jpg" / "*.jpg" 都变成 "*.jpg"），
# DB 不迁移 —— 历史配置天然兼容。

# 视为"已经是 glob"的元字符；含这些字符的模式原样保留（只转小写）。
_GLOB_META_CHARS = frozenset("*?[]")

# 后缀切分符；``str.rpartition`` 用它取最后一个点之后的部分。
_EXT_SPLIT = "."


# noinspection unreachable-code
def normalize_file_globs(raw: Iterable[str] | None) -> List[str]:
    """把 ``ingest_source.file_types`` 归一为小写 glob 列表。

    归一规则（读取时做，DB 不迁移）：

    ==================  ==============  ============================
    输入                输出            说明
    ==================  ==============  ============================
    ``"jpg"``           ``"*.jpg"``     裸后缀补 ``*.``
    ``".jpg"``          ``"*.jpg"``     去掉前导点后补 ``*.``
    ``"*.jpg"``         ``"*.jpg"``     已是 glob，保留
    ``"IMG_*.jpeg"``    ``"img_*.jpeg"``含元字符 → 保留并转小写
    ``""`` / ``"  "``   （丢弃）        空规则不是规则
    ==================  ==============  ============================

    为什么小写：匹配统一按小写路径做（大小写不敏感），在归一阶段一次转好，
    避免每个文件都 ``lower()`` 一遍模式。
    """
    if not raw:
        return []

    normalized: List[str] = []
    for item in raw:
        if item is None:
            continue
        text = str(item).strip().lower()
        if not text:
            continue
        if _GLOB_META_CHARS & set(text):
            # 已经是 glob：原样保留（只转小写）。
            normalized.append(text)
            continue
        # 裸后缀 / 带点后缀 → "*.ext"
        normalized.append("*." + text.lstrip(_EXT_SPLIT))
    # 去重但保序（配置里同时写 "jpg" 和 "*.jpg" 很常见）。
    return list(dict.fromkeys(normalized))


class _GlobMatcher:
    """glob 粗筛器：零 IO 的字符串匹配，跑在扫描线程上。

    语义（与 :meth:`PurePosixPath.match` 对齐，**右锚定**）
    ----------------------------------------------------
    * 模式从右往左匹配相对路径的尾部；
    * ``*`` / ``?`` 不跨 ``/``；
    * **不支持** ``**``（跨目录由"右锚定"天然覆盖，真正的全路径匹配是
      另一个语义，本任务不做）；
    * 大小写不敏感（路径与模式两侧都按小写比较）。

    右锚定的实际含义：``img_*.jpeg`` 也命中 ``sub/img_01.jpeg``——
    与 ``*.jpg`` 命中 ``sub/a.jpg`` 是同一语义。这是历史
    ``file_types=["jpg"]``（等价 ``*.jpg``，允许任意深度）行为兼容的
    必要条件；若要"整段匹配"需要另开 full_match 语义。

    性能：``*.ext`` 这类纯后缀模式走 ``set`` 快速路径（O(1) 查表），
    混合了 ``*`` / ``?`` / ``[]`` 的复杂模式才构造 ``PurePosixPath``。
    """

    __slots__ = ("_pattern_count", "_exact_suffixes", "_complex")

    def __init__(self, globs: Iterable[str]):
        self._pattern_count = 0
        self._exact_suffixes: set[str] = set()
        self._complex: list[str] = []

        for pattern in globs:
            text = str(pattern).strip().lower()
            if not text:
                continue
            self._pattern_count += 1
            if text.startswith("*.") and not (
                    _GLOB_META_CHARS & set(text[2:])
            ):
                # 纯后缀模式：quick path，只比后缀。
                if text[2:]:
                    self._exact_suffixes.add("." + text[2:])
                continue
            self._complex.append(text)

    def __bool__(self) -> bool:
        """是否至少有一条有效规则（空规则集 = 不导入任何文件）。"""
        return self._pattern_count > 0

    def matches(self, rel_path: str) -> bool:
        """``rel_path`` 是否命中任一 glob（相对路径、POSIX 分隔符）。"""
        if not self._pattern_count:
            return False

        low = rel_path.lower()
        name = low.rpartition("/")[2] or low
        if _EXT_SPLIT in name and self._exact_suffixes:
            suffix = "." + name.rpartition(_EXT_SPLIT)[2]
            if suffix in self._exact_suffixes:
                return True

        if not self._complex:
            return False

        p = PurePosixPath(low)
        return any(p.match(pattern) for pattern in self._complex)


def _chunked(seq: Sequence[_T], size: int) -> Iterable[Sequence[_T]]:
    """把序列切成固定大小的批次（最后一批可能更短）。"""
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


class SourceServiceState(ObservableNamespace):
    """SourceService 的全部可变状态容器。

    与 ObservableNamespace 的关系：
      * 字段用 StateField 声明 → 类型校验 / 默认值语义和别的 state 子树一致；
      * 写字段 → 走 _ctx.mark_dirty → 触发 observe 回调（UI 可订阅）；
      * **不**挂到 DynamicConfig、**不** autosave、**不**落盘 —— 值里含运行时对象
        （Source 实例、Task 句柄……），天生不可序列化，因此继承 ObservableNamespace
        而不是 StatefulNamespace。
    """

    # ── 声明式字段 ─────────────────────────────────
    sources = StateField(dict)  # nested 推断为 False → 原样存
    tasks_by_source = StateField(dict)  # False
    submitted_seqs = StateField(set)  # False

    #
    _ctx: ObservableContext
    _initializing: bool

    def __init__(self, *, _ctx=None, **entries):
        entries.setdefault("sources", {})
        entries.setdefault("tasks_by_source", {})
        entries.setdefault("submitted_seqs", set())
        super().__init__(_ctx=_ctx, **entries)

    # 不再需要 _PLAIN_FIELDS
    # 不再需要 __setattr__ 重写
    # 不再需要手动 mark_dirty —— 走基类写路径即可

    # ── 视图 ──────────────────────────────────────
    @property
    def source_ids(self) -> set[int]:
        return set(self.sources)

    @property
    def tasks(self) -> list:
        return list(self.tasks_by_source.values())

    def require_source(self, source_id: int) -> IngestSource:
        """读取被跟踪源的快照；不存在时抛 ``KeyError``。

        只读访问器：``sources`` 的写路径必须走 :meth:`upsert_source` /
        :meth:`remove_source`，读路径收敛到这里，避免调用方散落
        ``self.sources[...]`` 而漏掉标脏。
        """
        return self.sources[source_id]

    # ── 变更（原地操作需手动标脏）───────────────
    def clear_tasks(self) -> None:
        if self.tasks_by_source:
            self.tasks_by_source.clear()
            self._ctx.mark_dirty(self, "tasks_by_source")

    def drop_source(self, source_id: int) -> None:
        if self.sources.pop(source_id, None) is not None:
            self._ctx.mark_dirty(self, "sources")
        if self.tasks_by_source.pop(source_id, None) is not None:
            self._ctx.mark_dirty(self, "tasks_by_source")

    def upsert_source(self, source_id: int, source: IngestSource) -> None:
        """新增 / 覆盖一条源快照，并广播 ``sources`` 变更。"""
        self.sources[source_id] = source
        self._ctx.mark_dirty(self, "sources")

    def remove_source(self, source_id: int) -> None:
        """移除一条源快照；确实删掉了才标脏（避免空通知）。"""
        if self.sources.pop(source_id, None) is not None:
            self._ctx.mark_dirty(self, "sources")

    def upsert_task(self, source_id: int, executor: Any) -> None:
        """新增 / 覆盖某源的扫描任务（``SourceExecutor``），并标脏。"""
        self.tasks_by_source[source_id] = executor
        self._ctx.mark_dirty(self, "tasks_by_source")

    def remove_task(self, source_id: int) -> None:
        """移除某源的扫描任务；确实删掉了才标脏。"""
        if self.tasks_by_source.pop(source_id, None) is not None:
            self._ctx.mark_dirty(self, "tasks_by_source")

    def mark_submitted(self, seq: int) -> None:
        """把 seq 记为在途；已存在时不重复标脏。"""
        if seq not in self.submitted_seqs:
            self.submitted_seqs.add(seq)
            self._ctx.mark_dirty(self, "submitted_seqs")

    def discard_submitted(self, seq: int) -> None:
        """回收一个 seq：该任务已到终态（完成 / 失败 / 被丢弃）。

        **线程契约**：本方法会 ``mark_dirty`` 并同步触发 observe 回调，因此
        只能在 state 的属主线程（主线程）调用 —— 与模块 docstring 里
        "state 不做跨线程同步" 的约定一致。工作线程（TaskService 的 done
        回调）**不得**直接调用它：``SourceService._on_task_done`` 只把 seq
        记进待回收集合，由主线程的 ``_drain_settled`` 在这里消费。
        """
        if seq in self.submitted_seqs:
            self.submitted_seqs.discard(seq)
            self._ctx.mark_dirty(self, "submitted_seqs")

    def prune_submitted(self, alive_seqs: set[int]) -> int:
        stale = self.submitted_seqs - alive_seqs
        if stale:
            self.submitted_seqs &= alive_seqs
            self._ctx.mark_dirty(self, "submitted_seqs")
        return len(stale)

    #
    def describe(self, *, verbose: bool = False) -> str:
        base = (
            f"SourceServiceState(sources={len(self.sources)}, "
            f"tasks={len(self.tasks_by_source)}, "
            f"submitted={len(self.submitted_seqs)})"
        )
        if verbose:
            details = ", ".join(
                f"{sid}->{s.source_path!r}" for sid, s in self.sources.items()
            )
            base += f" [sources={{{details}}}]"
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


# noinspection string-conversion-without-dunder-method
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

    # log
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

    def __format__(self, spec: str) -> str:
        if spec == "":
            return self.describe()
        if spec in ("v", "verbose"):
            return self.describe(verbose=True)
        raise ValueError(f"Unsupported format spec: {spec!r}")

    # run
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

    #
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

        历史实现返回的是 ``abs_fp.as_posix()``（绝对路径），这是一个 bug：
        候选表与资产表因此被写成了绝对路径。已在本函数修正为返回 ``rel``。
        项目仍处早期开发（``SCHEMA_VERSION = 0``，见 Connector），不提供数据
        迁移：老库直接删库重建即可，历史绝对路径无需兼容性回填。

        :param globs: 已经过 ``normalize_file_globs`` 的 glob 列表；
            空列表 → 返回空（显式空规则 = 不导入）。
        """
        if not globs:
            return []

        matcher = _GlobMatcher(globs)
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
        for batch in _chunked(rel_paths, _SCAN_BATCH_SIZE):
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
        原样写入 ``asset_candidate_cache.path``（不再做 ``str(Path(p))``
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
            AssetCandidateCacheDTO(
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
        for batch in _chunked(candidates, _SCAN_BATCH_SIZE):
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


# noinspection PyNoneFunctionAssignment,string-conversion-without-dunder-method
class SourceService:
    """导入源服务门面：状态收敛到 :class:`SourceServiceState`。

    对外 API
    --------
    * :meth:`start` / :meth:`update` / :meth:`update_source`
    * :meth:`create_source` / :meth:`update_source_config` / :meth:`delete_source`
    * :meth:`enable_source` / :meth:`disable_source`
    * :meth:`list_sources` / :meth:`get_source` / :meth:`has_source`
    * :attr:`scan_finished`（单 source 扫描完成信号）
    * :attr:`state`（唯一可变状态；调用方自行保证线程安全）

    启用 / 禁用语义
    --------------
    "是否启用"是 ``ingest_source.disabled`` 这一 DB 列，没有内存 set 副本。
    ``enable_source`` / ``disable_source`` 走 ``update_source_config`` →
    repo.update → ``update_source``，由 ``IngestSource.is_void()`` 的
    ``disabled`` 短路分支驱动 ``_drop_source``。禁用后的源不出现在
    ``state.sources`` 里，不参与调度。
    """
    # 类属性 → 必须用 SignalDescriptor（lazy、实例间共享）。
    # 契约：cb(sender: SourceService, *, source_id: int)
    source_added = SignalDescriptor("source_added")
    source_updated = SignalDescriptor("source_updated")
    source_removed = SignalDescriptor("source_removed")

    # 类级 seq 计数器：跨实例保证 seq 唯一（防止一次误重建把旧 seq 复用）。
    _seq_counter = _count()

    def __init__(
            self,
            source_repo: IngestSourceRepository,
            import_repo: ImportRepository,
            task_service: "TaskService",
            *,
            state: Optional[SourceServiceState] = None,
    ):
        self._source_repo = source_repo
        self._import_repo = import_repo
        self._task_service = task_service

        # 唯一可变状态容器；允许外部注入以便测试 / 与其他组件共享。
        self.state: SourceServiceState = state or SourceServiceState()

        # 在途提交（key = task.seq）。完成回调要靠它确认 future 归属并回收；
        # 回调跑在 TaskService 的工作线程上，所以这里单独加锁 —— state 本身
        # 仍遵循"不加锁、调用方串行化"的约定。
        self._futures: Dict[int, Future[Any]] = {}
        self._futures_lock = threading.Lock()

        # 工作线程（done 回调）只往这里塞 seq，不碰 state；主线程在
        # ``_drain_settled`` 里统一回收，保证 observe 回调只在主线程触发。
        self._settled_seqs: Set[int] = set()
        self._settled_lock = threading.Lock()

        # 单 source 扫描完成信号（实例属性，实例独占）。
        # 契约：cb(sender: SourceExecutor, *, event: SourceScanFinished)
        self.scan_finished: Signal = Signal(name="SourceScanFinished")

        #
        self._closed: bool = False

    # ------------------------------------------------------------------ 日志

    def describe(self, *, verbose: bool = False) -> str:
        if verbose:
            return (
                f"SourceService(state={self.state:v}, "
                f"submitted={sorted(self.state.submitted_seqs)})"
            )
        return f"SourceService(state={self.state})"

    def __str__(self) -> str:
        return self.describe()

    def __format__(self, spec: str) -> str:
        if spec == "":
            return self.describe()
        if spec in ("v", "verbose"):
            return self.describe(verbose=True)
        raise ValueError(f"Unsupported format spec: {spec!r}")

    # ------------------------------------------------------------------ 视图
    # 只读便利属性；底层引用与 state 共享。

    @property
    def sources(self) -> Mapping[int, IngestSource]:
        return self.state.sources

    @property
    def tasks(self) -> List["SourceExecutor"]:
        return self.state.tasks

    # ==================================================================
    # 生命周期
    # ==================================================================

    def start(self) -> None:
        """首次启动：等价于一次完整 :meth:`update`。"""
        self.update()

    def shutdown(self) -> None:
        """关闭服务。

        * 幂等：多次调用无副作用；
        * 不清空 scan_finished 的订阅者（订阅生命周期归订阅方）；
        * 不通知 TaskService，不取消在途任务。

        关闭后：update / update_source / create / delete / update_source_config
        / enable_source / disable_source 全部 no-op（写操作返回 False 或 None，
        无返回值的直接 return）。
        读操作（list_sources / get_source / has_source / get_sync_candidate）
        继续可用 —— 它们只转发 repo，不涉及本服务状态。
        """
        if self._closed:
            return
        self._closed = True

        # 释放引用，便于 GC；也避免误用
        self.state.sources.clear()
        self.state.tasks_by_source.clear()
        self.state.submitted_seqs.clear()

        # 在途 future 句柄一并释放（done 回调可能在关闭后仍到达，
        # ``_on_task_done`` 对不存在的 seq 是 no-op）。
        with self._futures_lock:
            self._futures.clear()
        with self._settled_lock:
            self._settled_seqs.clear()

        _logger.debug("SourceService shut down")

    def update(self) -> None:
        """全量刷新：

        1. 从 repo 取当前所有有效源；
        2. 与 ``state.sources`` diff（新增 / 变更 / 删除）；
        3. 只为新增 / 扫描行为变更的源重建任务（纯展示字段只刷新快照）；
        4. 提交尚未提交的任务。

        与 :meth:`update_source` 语义对齐：改 ``title`` 之类的展示字段不会
        重建任务、``seq`` 保持不变；改 ``source_path`` 等行为字段才重建。
        """
        if self._closed:
            _logger.debug("update() ignored: SourceService is closed")
            return

        latest = self._fetch_latest_valid_sources()
        _logger.debug(
            "Refreshing sources: valid=%d current=%d",
            len(latest), len(self.state.sources),
        )
        _logger.trace("Latest valid sources: %s", pformat(latest))

        rebuild_ids = self._apply_full_snapshot(latest)
        self._rebuild_tasks(rebuild_ids)
        self._submit_new_tasks()

    def update_source(self, source_id: int) -> None:
        """单源刷新：拉取最新配置 → 评估有效性 → 视情况重建该源的任务。

        重建条件（与 :meth:`_source_changed` 对齐）：

        * 源是首次出现（``old is None``）；
        * 任一**扫描行为**字段变化：``target_path`` / ``mount_point`` /
          ``source_path`` / ``file_type_check`` / ``file_types`` /
          ``subfolder_recursion`` / ``subfolder_recursion_depth`` /
          ``auto_mount``。

        仅展示字段（``title`` / ``description`` / ``tags`` / ``trigger_config``）
        变化时：

        * 刷新 ``state.sources`` 快照，让后续 reader 看到新值；
        * **不**重建任务 —— 已在途 / 已提交的扫描任务保持原 seq 不动，
          避免"改个标题就重扫一遍"。

        源不存在 / 无效（含 ``disabled=True``） / 挂载失败时：
        从 state 移除其跟踪记录与任务。无论走哪条分支，最后都会尝试提交新任务。
        """
        if self._closed:
            _logger.debug(
                "update_source() ignored: SourceService is closed"
            )
            return

        config = self._fetch_source_config(source_id)

        if config is None or config.is_void():
            self._drop_source(source_id)
            self._submit_new_tasks()
            return

        old = self.state.sources.get(source_id)

        if old is None or self._source_changed(old, config):
            # 新增 / 扫描行为变更：需要（重新）挂载 + 重建任务
            if not self._try_mount(source_id, config):
                self._drop_source(source_id)
                self._submit_new_tasks()
                return
            self.state.upsert_source(source_id, config)
            self._rebuild_task_for_source(source_id)
        elif old != config:
            # 仅展示字段变化：刷新快照，保留原任务与 seq
            self.state.upsert_source(source_id, config)
        # else: 完全未变 —— 保留原对象引用，无操作

        self._submit_new_tasks()

    # ==================================================================
    # 写入 API
    # ==================================================================

    def create_source(self, source: IngestSourceCreate) -> Optional[int]:
        """创建导入源并立即调度一次扫描。

        Returns:
            新建 source 的 id；服务已关闭时返回 ``None``。
        """
        if self._closed:
            _logger.debug("write op ignored: SourceService is closed")
            return None

        new_id = self._source_repo.create(source)
        _logger.debug("Created source %d, scheduling initial scan", new_id)
        self.update_source(new_id)
        self.source_added.emit(self, source_id=new_id)
        return new_id

    def update_source_config(
            self, source_id: int, source: IngestSourceUpdate
    ) -> bool:
        """局部更新 + 让 worker 重新评估该源。

        Returns:
            ``True`` 表示确实写入了 DB（``False`` 表示 DTO 全是 ``UNSET``
            或 id 不存在，或服务已关闭）；写入成功才会触发重扫评估。
        """
        if self._closed:
            _logger.debug("write op ignored: SourceService is closed")
            return False

        ok = self._source_repo.update(source_id, source)
        if ok:
            self.update_source(source_id)
            self.source_updated.emit(self, source_id=source_id)
        return ok

    def enable_source(self, source_id: int) -> bool:
        """启用导入源（幂等）。

        走与 ``update_source_config`` 相同的链路：写库 → ``update_source``
        → 源变为 "非 void" → 走 "新增" 分支（重挂载 + 建任务）。
        """
        return self.update_source_config(
            source_id, IngestSourceUpdate(disabled=False)
        )

    def disable_source(self, source_id: int) -> bool:
        """禁用导入源（幂等）。

        禁用后 ``IngestSource.is_void()`` 短路返回 ``True``，
        ``update_source`` 走 ``_drop_source`` 把该源从跟踪集里剔除。
        DB 行保留 —— 禁用是逻辑状态，不是删除。
        """
        return self.update_source_config(
            source_id, IngestSourceUpdate(disabled=True)
        )

    def delete_source(self, source_id: int) -> bool:
        """删除导入源；删除成功则同步刷新 worker 状态。

        repo 侧负责拒绝虚拟根 / 有 assets 引用的情况，这里只做流程编排。
        """
        if self._closed:
            _logger.debug("write op ignored: SourceService is closed")
            return False

        # TODO impl service
        # deleted = self._source_repo.delete(source_id)
        # if deleted:
        #     _logger.debug("Deleted source %d, refreshing worker", source_id)
        #     self.update_source(source_id)
        #     self.source_removed.emit(self, source_id=source_id)
        # return deleted
        return False

    # alias
    create = create_source
    delete = delete_source

    # ==================================================================
    # 查询 API（纯查询，不触发 worker）
    # ==================================================================

    def list_sources(self) -> List[IngestSource]:
        """列出所有导入源（含虚拟根 / 手动导入源），按 id 升序。"""
        return self._source_repo.list_view_dtos()

    def get_source(self, source_id: int) -> Optional[IngestSource]:
        """查询单条导入源；不存在返回 ``None``。"""
        return self._source_repo.get_view_dto(source_id)

    def has_source(self, source_id: int) -> bool:
        """快捷判断导入源是否存在。"""
        return self._source_repo.exists(source_id)

    def get_sync_candidate(self, source_id: int) -> Optional[IngestSource]:
        """单源同步候选，避免 UI 走 ``get_source_candidates()`` 全量遍历。"""
        return self._source_repo.get_sync_candidate(source_id)

    # ==================================================================
    # 内部：源读取 / 变更判定
    # ==================================================================

    def _fetch_latest_valid_sources(self) -> Dict[int, IngestSource]:
        """从 repo 取所有有效源，返回 ``{id: IngestSource}``。

        ``is_void()`` 已把 ``disabled=True`` 判为不可用，因此这里天然过滤
        掉所有被禁用的源，不需要额外的集合。
        """
        candidates = self._source_repo.get_source_candidates()
        result = {
            c.id: c
            for c in candidates
            if c.id != MANUAL_SOURCE_ID and not c.is_void()
        }
        return result

    def _fetch_source_config(
            self, source_id: int
    ) -> Optional[IngestSource]:
        """单源配置读取；虚拟根显式返回 ``None``。"""
        if source_id == MANUAL_SOURCE_ID:
            _logger.warning("Source(id=0) is virtual source, can't enable.")
            return None
        return self._source_repo.get_sync_candidate(source_id)

    @staticmethod
    def _source_changed(old: IngestSource, new: IngestSource) -> bool:
        """判断需要触发重扫/重挂载的字段是否变化。

        比较一切**影响"扫哪些文件 / 扫到哪"的字段**：

        * ``source_path`` / ``target_path`` / ``mount_point`` —— 位置变更；
        * ``file_type_check`` / ``file_types``               —— 筛选规则变更；
        * ``subfolder_recursion`` / ``subfolder_recursion_depth`` —— 深度变更；
        * ``auto_mount``                                     —— 挂载策略变更。

        **筛选规则必须算进来**：``SourceExecutor`` 持有的是创建任务那一刻的
        ``IngestSource`` 快照，任务不重建就一直用旧 DTO 扫。漏掉的后果是
        「把 file_types 从 ``[".png"]`` 补成 ``[".png", ".jpg"]`` 再重扫，
        日志里仍是 ``File globs: ['*.png']``，永远匹配 0 个文件」——
        用户侧表现为"改了过滤条件也没用、库一直是空的"。

        不触发重建的字段：

        * ``title`` / ``description`` / ``tags`` —— 纯展示字段；
        * ``trigger_config``                    —— 调度层字段；
        * ``disabled``                          —— 由 ``is_void`` 短路在
          上游处理（禁用走 drop、启用走新增，都进不了这条分支）。
        """
        return (
                old.target_path != new.target_path
                or old.mount_point != new.mount_point
                or old.source_path != new.source_path
                or old.file_type_check != new.file_type_check
                or list(old.file_types or ()) != list(new.file_types or ())
                or bool(old.subfolder_recursion)
                != bool(new.subfolder_recursion)
                or old.subfolder_recursion_depth
                != new.subfolder_recursion_depth
                or bool(old.auto_mount) != bool(new.auto_mount)
        )

    # ==================================================================
    # 内部：状态迁移
    # ==================================================================

    def _apply_full_snapshot(
            self, latest: Dict[int, IngestSource]
    ) -> Set[int]:
        """把 ``latest`` 应用到 ``state.sources``（新增 / 变更 / 删除）。

        三类变更的语义：

        * **新增**：挂载成功则跟踪，失败则忽略；
        * **扫描行为变更**（``_source_changed`` 为真）：重挂载，挂载失败就
          把源从跟踪集剔除；
        * **仅展示字段变更**：只刷新快照，不动任务 —— 与
          :meth:`update_source` 保持一致。

        未变化的源保留原对象引用，避免下游缓存的 key 被无谓打断。

        Returns:
            需要（重新）建任务的 ``source_id`` 集合（新增 + 行为变更）。
        """
        current_ids = self.state.source_ids
        latest_ids = set(latest)
        rebuild_ids: Set[int] = set()

        for source_id, new_source in latest.items():
            old = self.state.sources.get(source_id)

            if old is not None and self._source_changed(old, new_source):
                # 行为字段变了：重挂载 + 重建任务
                if self._try_mount(source_id, new_source):
                    self.state.upsert_source(source_id, new_source)
                    rebuild_ids.add(source_id)
                    _logger.trace(
                        "Source %d changed, tracked: %s",
                        source_id,
                        pformat(new_source),
                    )
                else:
                    self.state.remove_source(source_id)
            elif old is not None and old != new_source:
                # 仅展示字段变了：刷新快照，不重建任务、保留原 seq
                self.state.upsert_source(source_id, new_source)
            elif old is None:
                # 新增
                if self._try_mount(source_id, new_source):
                    self.state.upsert_source(source_id, new_source)
                    rebuild_ids.add(source_id)
                    _logger.trace(
                        "Source %d tracked: %s",
                        source_id,
                        pformat(new_source)
                    )
            # else: 完全未变，保留原引用

        removed_ids = current_ids - latest_ids
        if removed_ids:
            _logger.debug(
                "Dropping %d stale source(s): %s",
                len(removed_ids), sorted(removed_ids),
            )
        for source_id in removed_ids:
            self.state.remove_source(source_id)

        return rebuild_ids

    def _drop_source(self, source_id: int) -> None:
        """把源从 state 中彻底移除（跟踪记录 + 任务）。"""
        self.state.drop_source(source_id)

    # ==================================================================
    # 内部：任务管理
    # ==================================================================

    def _rebuild_tasks(self, rebuild_ids: Set[int]) -> None:
        """按 diff 结果同步任务表：

        * 源已不在跟踪集 → 丢弃其任务（源被删除 / 挂载失败 / 被禁用）；
        * ``rebuild_ids`` 里的源 → 重建任务（新 seq）；
        * 其余源 → 保留原 ``SourceExecutor`` 与 ``seq`` 不动。
        """
        for source_id in list(self.state.tasks_by_source):
            if source_id not in self.state.sources:
                self.state.remove_task(source_id)
        for source_id in rebuild_ids:
            if source_id in self.state.sources:
                self._schedule_task_for_source(source_id)

    def _rebuild_task_for_source(self, source_id: int) -> None:
        """仅重建指定源的任务；源已被移除时删除其任务。"""
        if source_id not in self.state.sources:
            self.state.remove_task(source_id)
            return
        self._schedule_task_for_source(source_id)

    def _schedule_task_for_source(self, source_id: int) -> None:
        """为有效源生成新的扫描任务，覆盖旧任务（若有）。

        ``tasks_by_source`` 直接存 :class:`SourceExecutor`：它本身就是
        TaskService 需要的可调用对象，``seq`` 也是提交凭据
        （``correlation_id``），没必要再包一层 DTO。
        """
        source = self.state.require_source(source_id)
        seq = next(SourceService._seq_counter)

        executor = SourceExecutor(
            source=source,
            repo=self._import_repo,
            seq=seq,
            finished_signal=self.scan_finished,
            # 权威存活判断走 source 仓：任务真正执行时源可能已被删除
            # （回收站软删 / purge），executor 据此跳过而不是撞外键。
            is_source_active=self._source_repo.exists,
        )
        self.state.upsert_task(source_id, executor)
        _logger.debug(
            "Scheduled scan task: source=%d seq=%d path=%s",
            source_id, seq, source.source_path,
        )

    def _submit_new_tasks(self) -> None:
        """把 ``state.tasks_by_source`` 中尚未提交的任务提交给 TaskService。

        ``submitted_seqs`` 是**在途集合**，不是"提交过"集合：

        * 提交成功后加入；任务到终态时由 done 回调
          (:meth:`_on_task_done`) 移除 —— 否则扫完一轮后再也提交不出去；
        * ``task.seq`` 被重建替换（源配置变更）时留在集合里的旧 seq 由
          :meth:`SourceServiceState.prune_submitted` 清掉。

        两条失败通道都必须就地接住，**不能**让异常冒到调用方
        （``create_source`` / ``update_source`` 都是 UI 写路径）：

        * ``BackpressureError``（队列满，拒绝式背压）：记 warning、跳过该
          任务，本轮**不**写 ``submitted_seqs``，下次刷新会重试；
        * ``TaskServiceShutdownError``（服务已关闭 / 正在关闭）：记 error
          并**提前 return** —— 此时后面每个任务都必然失败，继续尝试只会
          刷日志。

        成功提交时把 ``task.seq`` 作为 ``correlation_id`` 交给 TaskService：
        背压回调里能据此反查是哪个 source（``seq`` 是服务内部序号，不可用于
        反查）。
        """
        # 先把工作线程攒下的已结算 seq 在主线程回收掉（见 _drain_settled），
        # 否则"扫完一轮后再提交"会被 submitted_seqs 里的残留挡住。
        self._drain_settled()

        for executor in self.state.tasks_by_source.values():
            seq = executor.seq
            if seq in self.state.submitted_seqs:
                continue

            try:
                future = self._task_service.submit(
                    executor,
                    executor=_SCAN_TASK_EXECUTOR,
                    priority=_SCAN_TASK_PRIORITY,
                    correlation_id=seq,
                )
            except BackpressureError as exc:
                _logger.warning(
                    "Scan task rejected by backpressure, will retry on next "
                    "refresh: seq=%d (%s)", seq, exc,
                )
                continue
            except TaskServiceShutdownError as exc:
                _logger.error(
                    "TaskService is shut down, stop submitting scan tasks "
                    "for this round: seq=%d (%s)", seq, exc,
                )
                return

            self.state.mark_submitted(seq)
            with self._futures_lock:
                self._futures[seq] = future
            future.add_done_callback(
                lambda _future, _seq=seq: self._on_task_done(_seq)
            )
            _logger.debug(
                "Submitted scan task: seq=%d priority=%d",
                seq, _SCAN_TASK_PRIORITY,
            )

        alive = {e.seq for e in self.state.tasks_by_source.values()}
        pruned = self.state.prune_submitted(alive)
        if pruned:
            _logger.trace(
                "Pruned %d stale submitted seq(s); alive=%d",
                pruned, len(alive),
            )

    def _on_task_done(self, seq: int) -> None:
        """TaskService 的 done 回调（**工作线程**）：登记待回收的 seq。

        任务无论成功 / 失败 / 被背压 / 被 shutdown 丢弃，都会走到这里：
        不回收的话 ``submitted_seqs`` 只增不减，源扫完一次就永远提交不出去。

        **这里只做线程安全的最小操作**：摘掉 future 句柄、把 seq 塞进
        ``_settled_seqs``。真正的 ``state.discard_submitted``（会 mark_dirty
        并触发 observe 回调）留给主线程的 :meth:`_drain_settled`，
        以维持 "state 不做跨线程同步、observe 只在主线程触发" 的契约。
        """
        with self._futures_lock:
            self._futures.pop(seq, None)
        with self._settled_lock:
            self._settled_seqs.add(seq)
        _logger.trace("Scan task settled, queued for release: seq=%d", seq)

    def _drain_settled(self) -> None:
        """在主线程回收 ``_on_task_done`` 攒下的 seq。

        只允许从主线程的写路径调用（``update`` / ``update_source`` /
        ``create_source`` … 最终都会走到 :meth:`_submit_new_tasks`）。
        """
        with self._settled_lock:
            pending, self._settled_seqs = self._settled_seqs, set()
        for seq in pending:
            self.state.discard_submitted(seq)

    # ==================================================================
    # 内部：挂载
    # ==================================================================

    def _try_mount(self, source_id: int, source: IngestSource) -> bool:
        """按需为 source 执行自动挂载。

        * ``auto_mount=False``：不挂载，视为成功；
        * 挂载异常：记录日志并返回 ``False``。
        """
        if not source.auto_mount:
            return True
        try:
            self._auto_mount(source)
        except Exception as e:
            _logger.error(
                "Auto-mount failed for source %d: %s", source_id, e
            )
            return False
        _logger.debug(
            "Mounted source %d: %s -> %s",
            source_id, source.mount_point, source.source_path,
        )
        return True

    @staticmethod
    def _auto_mount(source: IngestSource) -> None:
        """执行自动挂载（``auto_mount=True`` 时前置字段已由 ``is_void`` 校验）。"""
        assert source.mount_point
        assert source.target_path
        auto_mount(
            mount_point=Path(source.mount_point),
            mount_target=source.target_path,
            source_path=Path(source.source_path),
            logger=_logger,
        )
