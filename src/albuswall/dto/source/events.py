#
"""Ingest source 域 —— 事件与结果 DTO（事件与结果类）。

归属见 ``dto/CONVENTIONS.md`` · DTO 归属与命名：事件 / 结果 DTO 由**命令侧产出**，
供订阅方或调用方消费（信号回调、service 编排），既不是 repo 的浏览视图，
也不是 repo 的写模型，因此单独成类、不判 Command / Query。

* 命名：``*Event``（信号 payload）/ ``*Report``（命令返回的报告）；
* 不得被查询路径当作列表项 / 详情返回类型使用。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional
from uuid import UUID

__all__ = [
    "SourcePurgeReport",
    "SourceScanFinished",
]


@dataclass(frozen=True, slots=True)
class SourceScanFinished:
    """单个 source 扫描/持久化任务完成事件（``*Event``）。

    ``skipped=True`` 表示任务被**主动跳过**（扫描开始前发现源已被删除 /
    禁用），既不是成功也不是失败：扫描没有发生，``ok`` 仍为 ``True``，
    但 ``inserted_count`` 等计数全部为 0，订阅方不应据此判定"无新内容"。
    """

    source_id: int
    task_seq: int
    scanned_file_count: int
    new_file_count: int
    has_new_content: bool
    inserted_count: int = 0
    error: Optional[str] = None
    skipped: bool = False

    @property
    def ok(self) -> bool:
        """本次扫描无异常。"""
        return self.error is None

    @property
    def is_noop(self) -> bool:
        """扫描成功但无新内容（被跳过的事件不算 noop）。"""
        return self.ok and not self.skipped and not self.has_new_content


@dataclass(frozen=True, slots=True)
class SourcePurgeReport:
    """``IngestSourceRepository.purge`` 的返回（``*Report``）。

    * ``purged=False``  → source 不存在（幂等，不是错误）
    * ``purged=True``   → source 已删，``asset_ids`` / ``asset_uuids``
                          列出**同时被物理删除**的资产，供 service 层
                          清理缩略图目录。
    * ``asset_ids`` / ``asset_uuids`` 在 ``purged=True`` 且 source 下
      无资产时为空列表——不是 None，调用方可以无条件迭代。

    契约：
    * ``asset_uuids`` 对外统一为 ``UUID``；repo 构造本报告时负责
      ``str→UUID``（与 ``from_row`` 同一口径，见 ``CONVENTIONS.md`` ·
      UUID 统一）。
    * 全改 dataclass：原 TypedDict 形式已撤销。
    """
    source_id: int
    purged: bool
    asset_ids: List[int]
    asset_uuids: List[UUID]
