#
"""Ingest source 的数据传输对象。

本模块**只**承载数据与纯函数式语义：

* 不做 IO、不调用仓库、不做表单桥接；
* ``trigger_config`` 的解析兼容逻辑收敛到一处（``_normalize_trigger_dict``），
  读路径统一走 :meth:`IngestSource.trigger` cached_property；
* ``UpdateMode`` 从 ``albuswall.common.enums`` 导入，本模块**不再**声明平行枚举。

DTO 列表
--------
* :class:`IngestSource`            —— 统一读模型（原 SyncCandidate + ViewDTO 合并）
* :class:`IngestSourceCreate`      —— 创建
* :class:`IngestSourceUpdate`      —— PATCH 更新
* :class:`IngestSourceFormData`    —— UI 表单快照
* :class:`SourceScanFinished`      —— 扫描完成事件
* :class:`SourcePurgeReport`       —— purge 返回报告
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field, fields
from functools import cached_property
from pathlib import Path
from typing import Any, Dict, Final, List, Mapping, Optional, Self
from uuid import UUID

from albuswall.common.enums import FileTypeCheckMode, UpdateMode
from albuswall.utils.path import is_valid_mount_point
from albuswall.utils.decode_json import json_loads_dict, json_loads_list

from .sentinel import UNSET, PatchField
from .trigger import TriggerConfig
from .utils import row_get, coerce_enum

MANUAL_SOURCE_ID: Final[int] = 0
"""虚拟根 / 手动导入源的固定主键。禁止删除、禁止自动同步。"""

__all__ = [
    "MANUAL_SOURCE_ID",
    "IngestSource",
    "IngestSourceCreate",
    "IngestSourceUpdate",
    "IngestSourceFormData",
    "SourceScanFinished",
    "SourcePurgeReport",
]


# noinspection broad-exception
def _normalize_trigger_dict(value: Any) -> Optional[Dict[str, Any]]:
    """把不同形态的 trigger_config 归一化为 dict（写入路径用）。

    接受：``None`` / ``dict`` / JSON 字符串 / 带 ``to_json`` 或
    ``to_dict`` 的对象（例如 :class:`TriggerConfig`）。

    优先级：``to_json`` 优先于 ``to_dict``。原因：
    :meth:`TriggerConfig.to_dict` 按契约保留 ``Enum`` 原样（运行时视图），
    直接落库在 ``json.dumps`` 时会炸；:meth:`TriggerConfig.to_json` 有
    ``default=`` 兜底把 ``Enum`` 转 ``value``，输出是 JSON-able 的
    **存储形态**。存储视图与运行时视图职责分离。
    """
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return json_loads_dict(value)

    to_json = getattr(value, "to_json", None)
    if callable(to_json):
        return json_loads_dict(_safe_call(to_json))

    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        result = _safe_call(to_dict)
        return result if isinstance(result, dict) else None

    return None


# noinspection broad-exception
def _safe_call(fn: Any) -> Any:
    try:
        return fn()
    except Exception:
        return None


# =============================================================================
# 统一读模型
# =============================================================================

@dataclass
class IngestSource:
    """导入源的统一 DTO。

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

    # ------------------------------------------------------------------ 表单桥
    def to_form_data(self) -> "IngestSourceFormData":
        """展平为 UI 表单快照。

        桥接逻辑放在 DTO 上（而非 presenter）是刻意的：form 的字段
        与 DTO 的字段一一对应，桥接是纯数据变换，不引入额外依赖。
        反向（form -> Update）的桥接则留给 presenter。
        """
        form = IngestSourceFormData(
            title=self.title,
            description=self.description or "",
            tags=list(self.tags),
            source_path=self.source_path,
            target_path=self.target_path,
            file_types=list(self.file_types),
            file_type_check=self.file_type_check,
            subfolder_recursion=self.subfolder_recursion,
            subfolder_recursion_depth=self.subfolder_recursion_depth,
            auto_mount=self.auto_mount,
            mount_point=self.mount_point,
        )
        _apply_trigger_config_to_form(self.trigger_config, form)
        return form


# =============================================================================
# 写入 DTO —— 创建
# =============================================================================

@dataclass
class IngestSourceCreate:
    """用于创建导入源的 DTO。

    职责边界：**只描述"用户想创建什么"**。
    不提供 ``to_row_params()``——SQL 占位符顺序属于 repo，改动 INSERT
    不应牵连 DTO。``trigger_config`` 为 JSON-able dict（DB 存储形态）；
    从 ``TriggerConfig`` 构造时先过 ``_normalize_trigger_dict``。
    """

    title: str
    source_path: str
    description: Optional[str] = None
    target_path: Optional[str] = None
    mount_point: Optional[str] = None
    auto_mount: bool = False
    file_type_check: FileTypeCheckMode = FileTypeCheckMode.SUFFIX
    file_types: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    subfolder_recursion: bool = False
    subfolder_recursion_depth: Optional[int] = None
    trigger_config: Optional[Dict[str, Any]] = None
    disabled: bool = False

    def __post_init__(self) -> None:
        # 写路径遇非法枚举值抛 ValueError，不静默降级
        # （CONVENTIONS.md · 枚举容错：读路径降级、写路径抛错）
        mode = coerce_enum(self.file_type_check, FileTypeCheckMode)
        if mode is None:
            raise ValueError(
                f"Invalid file_type_check: {self.file_type_check!r}"
            )
        self.file_type_check = mode
        self.file_types = list(self.file_types or [])
        self.tags = list(self.tags or [])
        if self.trigger_config is not None:
            self.trigger_config = _normalize_trigger_dict(self.trigger_config)


# =============================================================================
# 写入 DTO —— 更新（PATCH 语义）
# =============================================================================

@dataclass
class IngestSourceUpdate:
    """用于更新导入源的 DTO（PATCH 语义）。

    语义
    ----
    * 字段默认值为 :data:`UNSET` —— 表示"不改这个字段"；
    * 显式传 ``None``            —— 表示"将该字段置为 NULL"。

    这样"字段未设置"和"字段设置为 NULL"在类型层面就区分开来，
    不再依赖调用方逐字段判断。
    """

    title: PatchField[str] = UNSET
    description: PatchField[str] = UNSET
    source_path: PatchField[str] = UNSET
    target_path: PatchField[str] = UNSET
    mount_point: PatchField[str] = UNSET
    auto_mount: PatchField[bool] = UNSET
    file_type_check: PatchField[FileTypeCheckMode] = UNSET
    file_types: PatchField[List[str]] = UNSET
    tags: PatchField[List[str]] = UNSET
    subfolder_recursion: PatchField[bool] = UNSET
    subfolder_recursion_depth: PatchField[int] = UNSET
    trigger_config: PatchField[Dict[str, Any]] = UNSET
    disabled: PatchField[bool] = UNSET

    def __post_init__(self) -> None:
        if self.file_type_check is not UNSET and self.file_type_check is not None:
            mode = coerce_enum(self.file_type_check, FileTypeCheckMode)
            if mode is None:
                raise ValueError(
                    f"Invalid file_type_check: {self.file_type_check!r}"
                )
            self.file_type_check = mode
        if self.file_types is not UNSET and self.file_types is not None:
            # noinspection bad-argument-type
            self.file_types = list(self.file_types)
        if self.tags is not UNSET and self.tags is not None:
            # noinspection bad-argument-type
            self.tags = list(self.tags)
        if self.trigger_config is not UNSET and self.trigger_config is not None:
            self.trigger_config = _normalize_trigger_dict(self.trigger_config)

    def is_empty(self) -> bool:
        """所有字段都是 ``UNSET`` —— 没有任何变更意图。"""
        return all(
            getattr(self, f.name) is UNSET
            for f in fields(self)
        )


# =============================================================================
# 表单快照 DTO —— UI 与业务 DTO 之间的中间层
# =============================================================================

@dataclass
class IngestSourceFormData:
    """UI 表单快照，字段与 ``IngestSourceDetailWidget`` 控件一一对应。

    这一层存在的原因：detail_widget 是“哑”的 —— 只关心控件的读写，
    不关心 ``trigger_config`` 的嵌套结构。所有差异收敛在这里。

    控件对应关系（与旧版一致）
    --------------------------
    title_line_edit                       -> title
    description_line_edit                 -> description
    tags_view (+tags_line_edit/+tags_button) -> tags
    source_path_line_edit (+browser)      -> source_path
    file_type_list_view (+line_edit/add)  -> file_types
    file_type_check_s/mg_radio_button     -> file_type_check
    subfolder_recursion_check_box         -> subfolder_recursion
    subfolder_recursion_depth_check_box
        ("no limit") + spin_box           -> subfolder_recursion_depth (None=不限)
    grp_scheduled.checked                 -> scheduled_enabled
    update_mode_combo                     -> update_mode
    scheduled_time_edit                   -> scheduled_time
    interval_time_edit                    -> scheduled_interval
    grp_device_insertion_trigger.checked  -> device_trigger_enabled
    target_combo                          -> target_path
    auto_mount_checkbox                   -> auto_mount
    mount_point_edit                      -> mount_point

    注意：``disabled`` 是配置层开关，不与 detail widget 上的任何控件对应，
    因此本层**不**包含该字段。启用 / 禁用走 ``SourceService`` 的
    ``enable_source`` / ``disable_source``，不经表单快照。
    """

    # ---- meta ----
    title: str = ""
    description: str = ""
    tags: List[str] = field(default_factory=list)

    # ---- source ----
    source_path: str = ""
    target_path: Optional[str] = None
    file_types: List[str] = field(default_factory=list)
    file_type_check: FileTypeCheckMode = FileTypeCheckMode.SUFFIX
    subfolder_recursion: bool = False
    subfolder_recursion_depth: Optional[int] = None

    # ---- trigger（扁平化）----
    scheduled_enabled: bool = False
    update_mode: UpdateMode = UpdateMode.SCHEDULED_TIME
    scheduled_time: Optional[str] = None  # "HH:MM"
    scheduled_interval: Optional[str] = None  # "30m" / "1h"
    device_trigger_enabled: bool = False

    # ---- mount ----
    auto_mount: bool = False
    mount_point: Optional[str] = None

    # ------------------------------------------------------------------ 规范化
    def __post_init__(self) -> None:
        self.file_type_check = coerce_enum(
            self.file_type_check,
            FileTypeCheckMode,
            FileTypeCheckMode.SUFFIX,
        )
        self.file_types = list(self.file_types or [])
        self.tags = list(self.tags or [])

        normalized = coerce_enum(
            self.update_mode,
            UpdateMode,
            UpdateMode.SCHEDULED_TIME,
        )
        if normalized not in (UpdateMode.SCHEDULED_TIME, UpdateMode.INTERVAL_TIME):
            normalized = UpdateMode.SCHEDULED_TIME
        self.update_mode = normalized

    # ------------------------------------------------------------------ 转换
    def to_trigger_config(self) -> Dict[str, Any]:
        """把扁平 trigger 字段组装成 JSON-able ``trigger_config`` dict。

        映射规则（``update_mode`` 为单值，需要综合两个开关得到）:

        * 两者都未启用       -> ``update_mode = "manual"``
        * 仅 scheduled       -> ``update_mode = self.update_mode``
        * 仅 device_trigger  -> ``update_mode = "device_trigger"``
        * 都启用             -> ``update_mode = "device_trigger"``
          （设备接入为主触发，scheduled 作为兜底扫描保留其 enabled 与时间配置）

        未被选中模式的 ``time`` / ``interval`` 会显式置 None，
        避免旧值残留。

        返回 dict（而非 ``TriggerConfig``）是有意为之：写入路径
        （``IngestSourceCreate`` / ``IngestSourceUpdate``）的
        ``trigger_config`` 字段就是 dict，JSON 序列化零成本；
        强类型视图由 :meth:`IngestSource.trigger` 按需提供。

        契约（``CONVENTIONS.md`` · TriggerConfig.to_dict）：这是**存储形态**
        的唯一出口，``TriggerConfig.to_dict`` 仅作运行时视图，不进存储。

        形态：``update_mode`` 恒为**单值**，与 ``TriggerConfig.update_mode``
        （单值 ``UpdateMode``）以及 ``media_library_schema.sql`` 的注释一致。
        """
        if not self.scheduled_enabled and not self.device_trigger_enabled:
            return {"update_mode": UpdateMode.MANUAL.value}

        if self.device_trigger_enabled:
            update_mode = UpdateMode.DEVICE_TRIGGER
        else:
            update_mode = self.update_mode

        return {
            "update_mode": update_mode.value,
            "device_trigger": {"enabled": bool(self.device_trigger_enabled)},
            "scheduled": {
                "enabled": bool(self.scheduled_enabled),
                "time": (
                    self.scheduled_time
                    if update_mode is UpdateMode.SCHEDULED_TIME
                    else None
                ),
                "interval": (
                    self.scheduled_interval
                    if update_mode is UpdateMode.INTERVAL_TIME
                    else None
                ),
            },
        }


def _apply_trigger_config_to_form(
        tc: Optional[Mapping[str, Any]],
        form: IngestSourceFormData,
) -> None:
    """把 trigger_config dict 反解回 form 的扁平字段（原地写入）。

    ``update_mode`` 是单值字符串，未知 / NULL 降级为 ``MANUAL``
    （读路径容错，见 ``CONVENTIONS.md`` · 枚举容错）。
    """
    if not tc:
        return

    update_mode = coerce_enum(
        tc.get("update_mode"),
        UpdateMode,
        UpdateMode.MANUAL,
    )

    scheduled = tc.get("scheduled") or {}
    device = tc.get("device_trigger") or {}

    form.scheduled_enabled = bool(scheduled.get("enabled", False))
    form.device_trigger_enabled = bool(device.get("enabled", False))

    # form 只区分 scheduled_time / interval_time 两种；
    # device_trigger / manual 一律落回 scheduled_time 分支（表单保持可编辑）。
    if update_mode is UpdateMode.INTERVAL_TIME:
        form.update_mode = UpdateMode.INTERVAL_TIME
    else:
        form.update_mode = UpdateMode.SCHEDULED_TIME

    form.scheduled_time = scheduled.get("time")
    form.scheduled_interval = scheduled.get("interval")


# =============================================================================
# 扫描完成事件 DTO
# =============================================================================

@dataclass(frozen=True, slots=True)
class SourceScanFinished:
    """单个 source 扫描/持久化任务完成事件。

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
    """``IngestSourceRepository.purge`` 的返回。

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
