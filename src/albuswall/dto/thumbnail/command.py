#
"""Thumbnail 域 —— 命令侧 DTO（Command）。

归属见 ``dto/CONVENTIONS.md`` · DTO 归属与命名：

* ``ThumbnailTaskInput`` —— 命令侧"写前读"的输入投影（``get_task_input*`` →
  构造任务 → 写 ``thumb_*`` 列），读是为了写，故归命令侧；
* ``ThumbnailResult``    —— 命令侧产出的结果（``_work_one`` 返回值，驱动重试队列），
  属"事件与结果类"，命名用 ``*Result``。

本模块不提供 ``from_row`` 之外的读模型；查询侧的行 / 统计在
``dto/thumbnail/query.py``，路径值对象在 ``dto/thumbnail/paths.py``。
"""

from __future__ import annotations

from sqlite3 import Row
from dataclasses import dataclass
from typing import Optional
from uuid import UUID

from albuswall.dto.utils import row_get, to_uuid

__all__ = [
    "ThumbnailResult",
    "ThumbnailTaskInput",
]


@dataclass(frozen=True, slots=True)
class ThumbnailTaskInput:
    """单个缩略图任务所需的全部上下文（命令侧写前读）。

    由 ``ThumbnailRepository.get_task_input(asset_id)``
    或 ``ThumbnailRepository.get_task_input_by_uuid(uuid)`` 构造。

    契约：``uuid`` 对外统一为 ``UUID``；``from_row`` 负责 ``str→UUID``。
    """
    asset_id: int
    uuid: UUID
    source_id: Optional[int]
    source_path: Optional[str]  # sources 表里的根目录/基础路径
    file_path: str  # assets 表里的相对路径

    @classmethod
    def from_row(cls, row: Row) -> "ThumbnailTaskInput":
        """契约：``from_row`` 固定接受 ``sqlite3.Row``，不兼容 ``Mapping``。

        ``asset_id`` 优先取 ``id`` 列，缺失时回退到 ``asset_uuid``
        （历史调用点兼容，待调用方统一后收紧）。
        """
        return cls(
            asset_id=row_get(row, "id") or row_get(row, "asset_uuid"),
            uuid=to_uuid(row["uuid"]),
            source_id=row_get(row, "source_id"),
            source_path=row_get(row, "source_path"),
            file_path=row_get(row, "file_path"),
        )


@dataclass(frozen=True, slots=True)
class ThumbnailResult:
    """``_work_one()`` 的执行结果（命令侧产出 / ``*Result``）。

    ok          —— 是否成功
    asset_uuid  —— 对应资产 uuid（契约：``UUID``，与 ``Asset.uuid`` 同口径）
    error       —— 失败标记，形如 "render:UnidentifiedImageError"
    duration_ms —— 成功耗时（毫秒）
    retryable   —— False 表示重试没有意义（资产已删、源丢失、非法图片）；
                   True 表示临时故障（IO、OOM、写盘失败），可有限重试。
                   TaskService 应以此决定是否入重试队列。
    """
    ok: bool
    asset_uuid: UUID
    error: Optional[str] = None
    duration_ms: Optional[int] = None
    retryable: bool = False
