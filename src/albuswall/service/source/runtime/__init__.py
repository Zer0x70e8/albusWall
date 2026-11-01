#
"""常驻任务层：扫描任务的持有、替换与提交。

只导出三件东西：

* :class:`SourceRuntime` —— 接收 state 层的对账计划；
* :class:`SourceExecutor` —— 单个源的扫描执行体（任务本体）；
* :class:`ScanTaskSnapshot` —— 每源扫描任务的只读快照（``SourceRuntime.status``）；
* :func:`normalize_file_globs` / :class:`GlobMatcher` / :func:`chunked`
  等扫描侧纯函数（见 :mod:`.scanning`）。

本层**不** import ``state`` / ``command`` / ``trigger`` 子包，
依赖方向永远是 ``state → runtime``。
"""

from .executor import SourceExecutor
from .scanning import (
    MAX_PATHS_PER_QUERY,
    GlobMatcher,
    chunked,
    normalize_file_globs,
)
from .service import ScanTaskSnapshot, SourceRuntime

__all__ = [
    "MAX_PATHS_PER_QUERY",
    "GlobMatcher",
    "ScanTaskSnapshot",
    "SourceExecutor",
    "SourceRuntime",
    "chunked",
    "normalize_file_globs",
]
