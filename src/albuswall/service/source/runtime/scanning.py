#
"""扫描的纯函数与常量（零 IO、零状态）。

本模块是 ``service/source`` 里唯一"不算服务"的一层：只放字符串处理、
分批工具和调参常量，供 :mod:`~albuswall.service.source.runtime.executor`
在扫描线程上调用。它**不** import 仓储 / DTO / 信号，也不持有任何状态，
因此可以被任意子包安全 import 而不引入环。

搬自旧 ``service/source.py`` 顶部，语义逐字保留（含 ``file_types`` 的
glob 归一规则与右锚定匹配语义）。
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Iterable, List, Sequence, TypeVar

from albuswall.common.enums import ExecutorType
from albuswall.repositories.import_ import MAX_PATHS_PER_QUERY

__all__ = [
    "MAX_PATHS_PER_QUERY",
    "GlobMatcher",
    "chunked",
    "normalize_file_globs",
    "SCAN_BATCH_SIZE",
    "SCAN_TASK_EXECUTOR",
    "SCAN_TASK_PRIORITY",
]

_T = TypeVar("_T")

SCAN_BATCH_SIZE = 500

# 批次上限必须落在 SQLite 单条查询的变量上限之内：
# ``ImportRepository.find_missing_paths`` 的 SQL 固定占用 1 个变量
# （source_id），因此单批最多能带 ``MAX_PATHS_PER_QUERY`` 个 path 占位符。
# 调大 ``SCAN_BATCH_SIZE`` 前先看这里，否则会在运行期抛 ValueError。
assert SCAN_BATCH_SIZE <= MAX_PATHS_PER_QUERY, (
    f"SCAN_BATCH_SIZE ({SCAN_BATCH_SIZE}) must be <= "
    f"MAX_PATHS_PER_QUERY ({MAX_PATHS_PER_QUERY})"
)

# 扫描任务的固定调度参数（``Task`` DTO 删除后集中到这里，避免魔法数散落）。
SCAN_TASK_PRIORITY = 8
SCAN_TASK_EXECUTOR = ExecutorType.THREAD

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


class GlobMatcher:
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


def chunked(seq: Sequence[_T], size: int) -> Iterable[Sequence[_T]]:
    """把序列切成固定大小的批次（最后一批可能更短）。"""
    if size <= 0:
        raise ValueError(f"chunk size must be positive, got {size}")
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


# 旧实现内部名为 ``_GlobMatcher`` / ``_chunked`` / ``_SCAN_*``；本包把它们
# 导出为公开名，同时保留旧名别名，便于逐字对照迁移前的调用点。
_GlobMatcher = GlobMatcher
_chunked = chunked
_SCAN_BATCH_SIZE = SCAN_BATCH_SIZE
_SCAN_TASK_PRIORITY = SCAN_TASK_PRIORITY
_SCAN_TASK_EXECUTOR = SCAN_TASK_EXECUTOR
