#
"""Ingest source 域 —— 查询侧 DTO（Query）。

归属见 ``dto/CONVENTIONS.md`` · DTO 归属与命名：

* 本模块只有读模型 ``IngestSource`` 与域内公开常量 ``MANUAL_SOURCE_ID``；
* 写模型在 ``dto/source/command.py``，事件与结果在 ``dto/source/events.py``；
* 本模块**不** import 上述两者（查询路径禁止引用 Command DTO）；
* 本模块只承载数据与纯函数式语义：不做 IO、不调用仓库、不做表单桥接
  （表单桥接在命令侧 ``dto/source/command.py``，见 ``form_from_source``）。

命令侧用法
----------
命令侧（导入 worker、``SourceService`` 跟踪集）会把同一个读模型当作**只读配置
快照**使用。为避免"看起来像读模型被写路径拥有"的误解，命令侧请用别名
:data:`IngestSourceSnapshot` 标注语义；二者是同一个类（别名，不是子类），
不产生第二套字段与 ``from_row``。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any, Dict, Final, List, Optional, Self

from albuswall.common.enums import FileTypeCheckMode
from albuswall.utils.path import is_valid_mount_point
from albuswall.utils.decode_json import json_loads_dict, json_loads_list

from albuswall.dto.trigger import TriggerConfig
from albuswall.dto.utils import row_get, coerce_enum

MANUAL_SOURCE_ID: Final[int] = 0
"""虚拟根 / 手动导入源的固定主键。禁止删除、禁止自动同步。"""

__all__ = [
    "IngestSource",
    "IngestSourceSnapshot",
    "MANUAL_SOURCE_ID",
]


@dataclass
class IngestSource:
    """导入源的统一读模型（Query）。

    设计取舍
    --------
    原实现拆分为 ``IngestSourceSyncCandidate``（worker 用）与
    ``IngestSourceViewDTO``（UI 用）两个类，字段 80% 重叠，代价是：

    * 两份 ``from_row`` 必须手动保持同步；
    * 一条 ``ViewDTO.to_sync_candidate()`` 的降级转换路径；
    * 调用方必须清楚"我拿到的这一份带不带 title"。

    Python 的 dataclass 多带几个不读的字段零成本；合并为单一类型后
    ``worker._sources``、``repo.get_source_candidates()``、
    UI 的 ``list_sources()`` 看到的是同一种形状。

    归属说明
    --------
    * 查询路径：UI 列表 / 详情（``list_sources`` / ``get_source``）；
    * 命令路径："写前读"的只读配置快照（``get_sync_candidate`` 及其在
      ``SourceService`` 跟踪集里的副本），命令侧用 :data:`IngestSourceSnapshot`
      标注该用途。

    字段来源对应
    ------------
    * ``target_path``  = 原 SyncCandidate.target / ViewDTO.target_path
      （DB 列名 ``target_path``，此处统一采用 DB 命名）；
    * ``title / description / tags / created_at / modified_at``
      = 原 ViewDTO 的展示字段；
    * 语义方法（``is_void`` / ``get_allowed_extensions``）来自 SyncCandidate；
    * 展示方法（``display_name`` / ``is_manual``）来自 ViewDTO。
    """

    # ---- identity ----
    id: int

    # ---- source 配置（同步逻辑需要）----
    source_path: str = ""
    target_path: Optional[str] = None
    mount_point: Optional[str] = None
    auto_mount: bool = False
    file_type_check: FileTypeCheckMode = FileTypeCheckMode.SUFFIX
    file_types: List[str] = field(default_factory=list)
    subfolder_recursion: bool = False
    subfolder_recursion_depth: Optional[int] = None
    trigger_config: Optional[Dict[str, Any]] = None

    # ---- 用户显式禁用 ----
    # 与 ``is_void`` 的"环境不成立"正交：
    #   * disabled=True  → 用户主动禁用，语义上不可用；
    #   * is_void()      → 目录不存在 / 挂载失效等环境原因，语义上不可用。
    # 二者任一为真，SourceService 都会把该源从跟踪集里剔除。
    disabled: bool = False

    # 删除标记，会在之后后台清理而不是立即完成
    is_deleted: bool = False

    # ---- 展示字段（UI 需要；轻查询时为空）----
    title: str = ""
    description: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    deleted_at: Optional[str] = None
    created_at: Optional[str] = None
    modified_at: Optional[str] = None

    # ------------------------------------------------------------------ 构造
    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Self:
        """从 ``sqlite3.Row`` 构造。

        契约：``from_row`` 固定接受 ``sqlite3.Row``，不兼容 ``Mapping``。
        缺失的列按空值处理（支持轻查询只 select 部分列）。
        """
        return cls(
            id=int(row["id"]),
            title=row_get(row, "title") or "",
            description=row_get(row, "description"),
            source_path=row_get(row, "source_path") or "",
            target_path=row_get(row, "target_path"),
            mount_point=row_get(row, "mount_point"),
            auto_mount=bool(row_get(row, "auto_mount", 0)),
            file_type_check=coerce_enum(
                row_get(row, "file_type_check"),
                FileTypeCheckMode,
                FileTypeCheckMode.SUFFIX,
            ),
            file_types=json_loads_list(row_get(row, "file_types")),
            tags=json_loads_list(row_get(row, "tags")),
            subfolder_recursion=bool(row_get(row, "subfolder_recursion", 0)),
            subfolder_recursion_depth=row_get(row, "subfolder_recursion_depth"),
            trigger_config=json_loads_dict(row_get(row, "trigger_config")),
            disabled=bool(row_get(row, "disabled", 0)),
            is_deleted=bool(row_get(row, "is_deleted", 0)),

            deleted_at=row_get(row, "deleted_at"),
            created_at=row_get(row, "created_at"),
            modified_at=row_get(row, "modified_at"),
        )

    # ------------------------------------------------------------------ 语义
    def is_void(self) -> bool:
        """该源是否不可用（用户显式禁用 / 缺少必要信息 / 环境不成立）。

        ``disabled`` 排在最前：用户主动禁用的源，无论环境是否成立都视为
        不可用，从跟踪集里剔除。
        """
        if self.disabled:
            return True
        if self.target_path is not None and (
                not self.source_path.strip()
                or not Path(self.source_path).is_dir()
        ):
            return True
        if self.file_type_check is FileTypeCheckMode.NONE:
            # 排除哨兵：NONE 是 Python 侧哨兵（schema CHECK 只允许
            # suffix / magic / off / magic_lax，且读路径经 coerce_enum
            # 后拿不到 NONE）。用"排除哨兵"而非白名单，保证新增成员
            # （off / magic_lax 以及未来的成员）默认放行。
            # 见 common/CONVENTIONS.md · 5.1 · FileTypeCheckMode.NONE。
            return True
        if (
                self.auto_mount
                and self.target_path is not None
                and self.mount_point is not None
                and not is_valid_mount_point(self.mount_point)
        ):
            return True
        return False

    def is_valid(self) -> bool:
        """``is_void()`` 的语义取反，便于调用侧阅读。"""
        return not self.is_void()

    def get_allowed_extensions(self) -> set[str]:
        """从源配置获取允许的扩展名集合（统一为小写、带点）。"""
        if not self.file_types:
            return set()
        allowed: set[str] = set()
        for ext in self.file_types:
            s = str(ext).strip().lower()
            if not s:
                continue
            if not s.startswith("."):
                s = "." + s.lstrip()
            allowed.add(s)
        return allowed

    # ------------------------------------------------------------------ 展示
    @property
    def display_name(self) -> str:
        """卡片标题兜底：``title`` 为空时退化到目录名。

        ``title`` 与 ``source_path`` 都为空时返回空串 —— DTO 只承载数据，
        本地化的「未命名」兜底由 UI 层提供（见
        ``IngestSourcePresenter._dto_to_card_item``），这样切换语言时
        卡片标题才能跟着变。
        """
        if self.title:
            return self.title
        if not self.source_path:
            return ""
        return (
            self.source_path.rstrip("/\\")
            .rsplit("/", 1)[-1]
            .rsplit("\\", 1)[-1]
        )

    @property
    def is_manual(self) -> bool:
        """是否为虚拟根 / 手动导入源。"""
        return self.id == MANUAL_SOURCE_ID

    # ------------------------------------------------------------------ 兼容
    @property
    def target(self) -> Optional[str]:
        """``target_path`` 的只读兼容别名（deprecated）。

        新代码请直接使用 ``target_path``。此别名保留的唯一目的，
        是不破坏既有调用方（``service/source.py`` 里的 ``source.target``）。
        """
        return self.target_path

    # ------------------------------------------------------------------ 触发配置
    # noinspection broad-exception
    @cached_property
    def trigger(self) -> Optional[TriggerConfig]:
        """强类型 ``TriggerConfig`` 视图；解析失败返回 ``None``。

        与 ``trigger_config`` 的关系：
        * ``trigger_config`` —— 存储表示（原始 JSON dict，零成本序列化）；
        * ``trigger``        —— 语义表示（强类型，供调度 / 门面使用）。

        解析失败**不抛异常**：单条脏数据不应阻塞源读取。严格语义请在
        repo 侧走 ``get_trigger_config``。
        """
        if self.trigger_config is None:
            return None
        try:
            cfg = TriggerConfig.from_json(
                json.dumps(self.trigger_config, ensure_ascii=False)
            )
        except Exception:
            return None
        if cfg is None:
            return None
        try:
            cfg.id = self.id
        except Exception:
            # TriggerConfig 可能是 frozen；忽略注入失败。
            pass
        return cfg


# 命令侧只读配置快照的语义别名：与 ``IngestSource`` 是**同一个类**，
# 不新增字段、不新增 from_row。用途只有"在命令路径的签名上标注这份读模型
# 是写前读的快照"（见 ``dto/CONVENTIONS.md`` · 交叉引用与写前读白名单）。
IngestSourceSnapshot = IngestSource
