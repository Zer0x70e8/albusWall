#
"""Thumbnail 域 DTO 包（Command / Query / 共享值对象 分包）。

按 ``dto/CONVENTIONS.md`` · DTO 归属与命名 分包：

* ``dto.thumbnail.paths``   —— 共享值对象 ``ThumbnailPaths``（两侧都允许 import）；
* ``dto.thumbnail.query``   —— 读路径返回的行 / 统计
  （``MissingThumbnailRow`` / ``ThumbnailHashRow`` / ``ThumbnailStats``）；
* ``dto.thumbnail.command`` —— 命令侧输入与结果
  （``ThumbnailTaskInput`` / ``ThumbnailResult``）。

本 ``__init__`` 只做**显式**再导出（无通配、无新增语义）。新代码建议直接从
所属子模块 import，以便在 import 行上就能看出读写侧。
"""

from .command import ThumbnailResult, ThumbnailTaskInput
from .paths import ThumbnailPaths
from .query import MissingThumbnailRow, ThumbnailHashRow, ThumbnailStats

__all__ = [
    "MissingThumbnailRow",
    "ThumbnailHashRow",
    "ThumbnailPaths",
    "ThumbnailResult",
    "ThumbnailStats",
    "ThumbnailTaskInput",
]
