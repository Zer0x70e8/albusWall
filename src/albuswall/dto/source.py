#
""""""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from functools import cached_property
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Final, Self

from albuswall.common.enums import FileTypeCheckMode
from albuswall.utils.path import is_valid_mount_point

from . import UnsetType, UNSET
from .trigger import TriggerConfig

MANUAL_SOURCE_ID: Final[int] = 0
"""虚拟根 / 手动导入源的固定主键。禁止删除、禁止自动同步。"""

__all__ = [
    "MANUAL_SOURCE_ID",
    "UpdateMode",
    "IngestSourceSyncCandidate",
    "IngestSourceCreate",
    "IngestSourceUpdate",
    "IngestSourceViewDTO",
    "IngestSourceFormData",
    "SourceScanFinished",
]


class UpdateMode(StrEnum):
    """trigger_config.update_mode 的合法取值。"""
    MANUAL = "manual"
    SCHEDULED_TIME = "scheduled_time"
    INTERVAL_TIME = "interval_time"
    DEVICE_TRIGGER = "device_trigger"


# =============================================================================
# JSON / 枚举规范化辅助
# =============================================================================

def _json_loads_list(raw: Any) -> List[str]:
    """把 DB 中的 TEXT JSON 数组解析为 ``list[str]``，任何异常回退到 ``[]``。"""
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw]
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return []
    if not isinstance(raw, str):
        return []
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(x) for x in parsed]


def _json_loads_dict(raw: Any) -> Optional[Dict[str, Any]]:
    """把 DB 中的 TEXT JSON 对象解析为 ``dict``，失败返回 ``None``。"""
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(raw, str):
        return None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _coerce_file_type_check(value: Any) -> Optional[FileTypeCheckMode]:
    """把 str / enum 统一为 ``FileTypeCheckMode``，非法值返回 ``None``。"""
    if value is None:
        return None
    if isinstance(value, FileTypeCheckMode):
        return value
    try:
        return FileTypeCheckMode(value)
    except (ValueError, TypeError):
        return None


def _normalize_trigger_config(value: Any) -> Optional[Dict[str, Any]]:
    """把不同形态的 trigger_config 规范化为 dict。

    接受：None / dict / JSON 字符串 / 带 ``to_json`` 或 ``to_dict`` 的对象
    （例如 :class:`TriggerConfig`）。
    """
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return _json_loads_dict(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            result = to_dict()
            return result if isinstance(result, dict) else None
        except Exception:
            return None
    to_json = getattr(value, "to_json", None)
    if callable(to_json):
        try:
            return _json_loads_dict(to_json())
        except Exception:
            return None
    return None


# =============================================================================
# 同步候选 DTO —— worker 内部使用
# =============================================================================

@dataclass
class IngestSourceSyncCandidate:
    """同步候选 DTO，表示一个可自动同步的导入源。

    这是 ``SourceServiceWorker`` 视角的最小字段集合：**不含**
    ``title / description / tags``，因为这些字段对同步逻辑毫无价值。
    """

    id: int
    source_path: str
    target: Optional[str]
    mount_point: Optional[str]
    auto_mount: bool
    file_type_check: FileTypeCheckMode
    file_types: List[str] = field(default_factory=list)
    subfolder_recursion: bool = False
    subfolder_recursion_depth: Optional[int] = None
    trigger_config: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------ 构造
    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> Self:
        return cls(
            id=int(row["id"]),
            source_path=row["source_path"] or "",
            target=row["target_path"],
            mount_point=row["mount_point"],
            auto_mount=bool(row["auto_mount"]),
            file_type_check=(
                    _coerce_file_type_check(row["file_type_check"])
                    or FileTypeCheckMode.SUFFIX
            ),
            file_types=_json_loads_list(row["file_types"]),
            subfolder_recursion=bool(row["subfolder_recursion"]),
            subfolder_recursion_depth=row["subfolder_recursion_depth"],
            trigger_config=_json_loads_dict(row["trigger_config"]),
        )

    # ------------------------------------------------------------------ 语义
    def is_void(self) -> bool:
        """判断该同步候选是否不可用（即缺少必要信息）。"""
        if self.target is not None and \
                (not self.source_path.strip() or
                 not Path(self.source_path).is_dir()):
            return True
        if self.file_type_check is FileTypeCheckMode.NONE:
            return True
        if self.auto_mount and \
                self.target is not None and \
                self.mount_point is not None and \
                not is_valid_mount_point(self.mount_point):
            return True
        if self.id is None:
            return True
        return False

    def get_allowed_extensions(self) -> set[str]:
        """从源配置获取允许的扩展名集合（统一为小写、带点）"""
        if not self.file_types:
            return set()
        allowed = set()
        for ext in self.file_types:
            ext = str(ext).strip().lower()
            if not ext.startswith('.'):
                ext = '.' + ext
            allowed.add(ext)
        return allowed

    # alias
    def is_valid(self) -> bool:
        """``is_void()`` 的语义取反，便于调用侧阅读。"""
        return not self.is_void()


# =============================================================================
# 写入 DTO —— 创建
# =============================================================================

@dataclass
class IngestSourceCreate:
    """用于创建导入源的 DTO。"""

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

    def __post_init__(self) -> None:
        mode = _coerce_file_type_check(self.file_type_check)
        self.file_type_check = mode or FileTypeCheckMode.SUFFIX
        self.file_types = list(self.file_types or [])
        self.tags = list(self.tags or [])
        self.trigger_config = _normalize_trigger_config(self.trigger_config)

    def to_row_params(self) -> tuple:
        """返回与 ``IngestSourceRepository.create`` 的 INSERT 占位符一一对应的元组。"""
        return (
            self.title,
            self.description,
            self.source_path,
            self.target_path,
            self.mount_point,
            int(self.auto_mount),
            self.file_type_check.value,
            json.dumps(self.file_types, ensure_ascii=False),
            json.dumps(self.tags, ensure_ascii=False),
            int(self.subfolder_recursion),
            self.subfolder_recursion_depth,
            json.dumps(self.trigger_config, ensure_ascii=False)
            if self.trigger_config is not None else None,
        )


# =============================================================================
# 写入 DTO —— 更新（PATCH 语义）
# =============================================================================

@dataclass
class IngestSourceUpdate:
    """用于更新导入源的 DTO，所有字段可选，仅更新传入的非 None 字段。"""

    title: Optional[str] = None
    description: Optional[str] = None
    source_path: Optional[str] = None
    target_path: Optional[str] = None
    mount_point: Optional[str] = None
    auto_mount: Optional[bool] = None
    file_type_check: Optional[FileTypeCheckMode] = None
    file_types: Optional[List[str]] = None
    tags: Optional[List[str]] = None
    subfolder_recursion: Optional[bool] = None
    subfolder_recursion_depth: Optional[int] | UnsetType = UNSET
    trigger_config: Optional[Dict[str, Any]] = None

    def __post_init__(self) -> None:
        if self.file_type_check is not None:
            mode = _coerce_file_type_check(self.file_type_check)
            if mode is None:
                raise ValueError(
                    f"Invalid file_type_check: {self.file_type_check!r}"
                )
            self.file_type_check = mode
        if self.file_types is not None:
            self.file_types = list(self.file_types)
        if self.tags is not None:
            self.tags = list(self.tags)
        if self.trigger_config is not None:
            self.trigger_config = _normalize_trigger_config(self.trigger_config)

    def is_empty(self) -> bool:
        """所有字段均为 None，无需发 UPDATE。"""
        return all(
            getattr(self, f) is None for f in self.__dataclass_fields__
        )

    # ---------------------------------------------------------------- 表单构造
    @classmethod
    def from_form_data(cls, form: "IngestSourceFormData") -> "IngestSourceUpdate":
        """从 UI 表单快照构造一个"全字段" Update。

        语义：把当前表单的所有值一次性写回仓库（不是 PATCH 部分字段）。
        ``trigger_config`` 由表单字段合成，保证 ``update_mode`` 的
        单值约束与 ``scheduled`` / ``device_trigger`` 的开关一致。
        """
        return cls(
            title=form.title,
            description=form.description,
            source_path=form.source_path,
            target_path=form.target_path,  # 可能是 None → 真正写 NULL
            mount_point=form.mount_point,
            auto_mount=form.auto_mount,
            file_type_check=form.file_type_check,
            file_types=list(form.file_types),
            tags=list(form.tags),
            subfolder_recursion=form.subfolder_recursion,
            subfolder_recursion_depth=form.subfolder_recursion_depth,
            trigger_config=form.to_trigger_config(),
        )


# =============================================================================
# 视图 DTO —— UI 展示使用
# =============================================================================

@dataclass
class IngestSourceViewDTO:
    """完整 ingest_source 记录，面向 UI 展示。

    与 :class:`IngestSourceSyncCandidate` 的区别：
    * 含 ``title / description / tags``（卡片展示必需）；
    * 含 ``created_at / modified_at``（详情页元信息）；
    * 与同步候选无继承关系 —— 两类消费者解耦，互不影响。
    """

    id: int
    title: str
    description: Optional[str]
    source_path: str
    target_path: Optional[str]
    mount_point: Optional[str]

    auto_mount: bool
    file_type_check: FileTypeCheckMode
    file_types: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)

    subfolder_recursion: bool = False
    subfolder_recursion_depth: Optional[int] = None

    trigger_config: Optional[Dict[str, Any]] = None

    created_at: Optional[str] = None
    modified_at: Optional[str] = None

    # ------------------------------------------------------------------ 构造
    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "IngestSourceViewDTO":
        return cls(
            id=int(row["id"]),
            title=row["title"] or "",
            description=row["description"],
            source_path=row["source_path"] or "",
            target_path=row["target_path"],
            mount_point=row["mount_point"],
            auto_mount=bool(row["auto_mount"]),
            file_type_check=(
                    _coerce_file_type_check(row["file_type_check"])
                    or FileTypeCheckMode.SUFFIX
            ),
            file_types=_json_loads_list(row["file_types"]),
            tags=_json_loads_list(row["tags"]),
            subfolder_recursion=bool(row["subfolder_recursion"]),
            subfolder_recursion_depth=row["subfolder_recursion_depth"],
            trigger_config=_json_loads_dict(row["trigger_config"]),
            created_at=row["created_at"],
            modified_at=row["modified_at"],
        )

    # ------------------------------------------------------------------ 辅助
    def to_sync_candidate(self) -> IngestSourceSyncCandidate:
        """降级为同步候选，供 worker 侧复用（丢弃 UI 字段）。"""
        return IngestSourceSyncCandidate(
            id=self.id,
            source_path=self.source_path,
            target=self.target_path,
            mount_point=self.mount_point,
            auto_mount=self.auto_mount,
            file_type_check=self.file_type_check,
            file_types=list(self.file_types),
            subfolder_recursion=self.subfolder_recursion,
            subfolder_recursion_depth=self.subfolder_recursion_depth,
            trigger_config=dict(self.trigger_config)
            if self.trigger_config is not None else None,
        )

    @cached_property
    def trigger(self) -> Optional[TriggerConfig]:
        """返回强类型 TriggerConfig，解析失败返回 None。"""
        if self.trigger_config is None:
            return None
        try:
            return TriggerConfig.from_json(
                json.dumps(self.trigger_config, ensure_ascii=False)
            )
        except Exception:
            return None

    def to_form_data(self) -> "IngestSourceFormData":
        """展平为 detail_widget 可以直接消费的表单快照。"""
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

    @property
    def display_name(self) -> str:
        """卡片标题兜底：``title`` 为空时退化到目录名。"""
        if self.title:
            return self.title
        if not self.source_path:
            return "(未命名)"
        return self.source_path.rstrip("/\\").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]

    @property
    def is_manual(self) -> bool:
        """是否为虚拟根 / 手动导入源。"""
        return self.id == MANUAL_SOURCE_ID


# =============================================================================
# 表单快照 DTO —— UI 与业务 DTO 之间的中间层
# =============================================================================

@dataclass
class IngestSourceFormData:
    """UI 表单快照，字段与 ``IngestSourceDetailWidget`` 控件一一对应。

    这一层存在的原因：

    * detail_widget 是"哑"的 —— 只关心控件的读写，不关心 ``trigger_config``
      的嵌套结构；
    * 业务 DTO 需要的是规范的 ``trigger_config`` JSON 结构；
    * 把两者的差异全部收敛在这里，Presenter 只需要：

          form = view_dto.to_form_data()       # DTO → UI
          detail_widget.set_form_data(form)

          form = detail_widget.get_form_data()  # UI → DTO
          update = IngestSourceUpdate.from_form_data(form)

    控件对应关系（详见行内注释）::

        title_line_edit                       → title
        description_line_edit                 → description
        tags_view (+tags_line_edit/+tags_button) → tags
        source_path_line_edit (+browser)      → source_path
        file_type_list_view (+line_edit/add)  → file_types
        file_type_check_s/mg_radio_button     → file_type_check
        subfolder_recursion_check_box         → subfolder_recursion
        subfolder_recursion_depth_check_box
            ("no limit") + spin_box           → subfolder_recursion_depth (None=不限)
        grp_scheduled.checked                 → scheduled_enabled
        update_mode_combo                     → update_mode
        scheduled_time_edit                   → scheduled_time
        interval_time_edit                    → scheduled_interval
        grp_device_insertion_trigger.checked  → device_trigger_enabled
        target_combo                          → target_path
        auto_mount_checkbox                   → auto_mount
        mount_point_edit                      → mount_point
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

    # ---- trigger（扁平化） ----
    scheduled_enabled: bool = False
    update_mode: str = "scheduled_time"  # "scheduled_time" | "interval_time"
    scheduled_time: Optional[str] = None  # "HH:MM"
    scheduled_interval: Optional[str] = None  # "30m" / "1h"
    device_trigger_enabled: bool = False

    # ---- mount ----
    auto_mount: bool = False
    mount_point: Optional[str] = None

    # ------------------------------------------------------------------ 规范化
    def __post_init__(self) -> None:
        mode = _coerce_file_type_check(self.file_type_check)
        self.file_type_check = mode or FileTypeCheckMode.SUFFIX
        self.file_types = list(self.file_types or [])
        self.tags = list(self.tags or [])
        # if self.update_mode not in ("scheduled_time", "interval_time"):
        #     self.update_mode = "scheduled_time"
        if self.update_mode not in (UpdateMode.SCHEDULED_TIME, UpdateMode.INTERVAL_TIME):
            self.update_mode = UpdateMode.SCHEDULED_TIME

    # ------------------------------------------------------------------ 转换
    def to_trigger_config(self) -> Dict[str, Any]:
        """把扁平 trigger 字段组装成 trigger_config JSON 结构。

        映射规则（``update_mode`` 为单值，需要综合两个开关得到）:

        * 两者都未启用             → ``update_mode = "manual"``
        * 仅 scheduled             → ``update_mode = form.update_mode``
        * 仅 device_trigger        → ``update_mode = "device_trigger"``
        * 都启用                   → ``update_mode = "device_trigger"``
          （设备接入为主触发，scheduled 作为兜底扫描保留其 enabled 与时间配置）

        未被选中模式的 ``time`` / ``interval`` 会显式置 None，
        避免旧值残留。
        """
        if not self.scheduled_enabled and not self.device_trigger_enabled:
            return {"update_mode": UpdateMode.MANUAL}

        if self.device_trigger_enabled:
            update_mode = UpdateMode.DEVICE_TRIGGER
        else:
            update_mode = UpdateMode(self.update_mode)

        return {
            "update_mode": update_mode,
            "device_trigger": {"enabled": bool(self.device_trigger_enabled)},
            "scheduled": {
                "enabled": bool(self.scheduled_enabled),
                "time": self.scheduled_time
                if update_mode is UpdateMode.SCHEDULED_TIME else None,
                "interval": self.scheduled_interval
                if update_mode is UpdateMode.INTERVAL_TIME else None,
            },
        }


def _apply_trigger_config_to_form(
        tc: Optional[Mapping[str, Any]],
        form: IngestSourceFormData,
) -> None:
    """把 trigger_config dict 反解回 form 的扁平字段（原地写入）。"""
    if not tc:
        return

    # ★ 兼容历史脏数据：update_mode 可能是 str / list / tuple
    raw_mode = tc.get("update_mode") or "manual"
    if isinstance(raw_mode, (list, tuple)):
        raw_mode = raw_mode[0] if raw_mode else "manual"
    update_mode = str(raw_mode)

    scheduled = tc.get("scheduled") or {}
    device = tc.get("device_trigger") or {}

    form.scheduled_enabled = bool(scheduled.get("enabled", False))
    form.device_trigger_enabled = bool(device.get("enabled", False))

    # form 只区分 scheduled_time / interval_time 两种；
    # device_trigger / manual 一律落回 scheduled_time 分支（表单保持可编辑）
    if update_mode == UpdateMode.INTERVAL_TIME:
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
    """单个 source 扫描/持久化任务完成事件。"""

    source_id: int
    task_seq: int
    scanned_file_count: int
    new_file_count: int
    has_new_content: bool
    inserted_count: int = 0
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        """本次扫描无异常。"""
        return self.error is None

    @property
    def is_noop(self) -> bool:
        """扫描成功但无新内容。"""
        return self.ok and not self.has_new_content
