#
"""Ingest source 域 —— 命令侧 DTO（Command）。

归属见 ``dto/CONVENTIONS.md`` · DTO 归属与命名：

* 本模块只放**写模型与表单写入桥**，只由命令路径（UI 提交 / service 写方法 /
  repo 写方法）产生与消费；
* 查询路径禁止 import 本模块；
* 本模块允许 import ``dto/source/query.py`` 的读模型，用于"读模型 → 表单快照"
  这一条桥：``form_from_source``（单向，不得反向）。

``trigger_config`` 的两个方向都收敛在本模块：

* 写：``IngestSourceForm.to_trigger_config()``（存储形态唯一出口）；
* 读：``_apply_trigger_config_to_form``（存储形态 → 扁平表单字段）。
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any, Dict, List, Mapping, Optional

from albuswall.common.enums import FileTypeCheckMode, UpdateMode
from albuswall.utils.decode_json import json_loads_dict

from albuswall.dto.sentinel import UNSET, PatchField
from albuswall.dto.utils import coerce_enum

from .query import IngestSource

__all__ = [
    "IngestSourceCreate",
    "IngestSourceForm",
    "IngestSourceUpdate",
    "form_from_source",
]


# noinspection broad-exception
def _safe_call(fn: Any) -> Any:
    try:
        return fn()
    except Exception:
        return None


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


# =============================================================================
# 写入 DTO —— 创建
# =============================================================================

@dataclass
class IngestSourceCreate:
    """用于创建导入源的写模型（Command）。

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
    """用于更新导入源的写模型（Command，PATCH 语义）。

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
# 表单快照 DTO —— UI 与业务 DTO 之间的中间层（命令侧）
# =============================================================================

@dataclass
class IngestSourceForm:
    """UI 表单快照（Command），字段与 ``IngestSourceDetailWidget`` 控件一一对应。

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
        form: IngestSourceForm,
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


def form_from_source(source: IngestSource) -> IngestSourceForm:
    """读模型 → 表单快照（``form_from_source``，命令侧单向桥）。

    原先这是读模型上的 ``IngestSource.to_form_data()`` 方法，属"Query DTO
    反向构造 Command DTO"的倒挂；按 ``dto/CONVENTIONS.md`` · 交叉引用与
    写前读白名单 移到命令侧，读模型不再知道自己有表单。

    桥接放在 DTO 层（而非 presenter）是刻意的：form 的字段与读模型字段
    一一对应，是纯数据变换；反向（form → Create/Update）的桥接同样在此，
    由 presenter 调用。
    """
    form = IngestSourceForm(
        title=source.title,
        description=source.description or "",
        tags=list(source.tags),
        source_path=source.source_path,
        target_path=source.target_path,
        file_types=list(source.file_types),
        file_type_check=source.file_type_check,
        subfolder_recursion=source.subfolder_recursion,
        subfolder_recursion_depth=source.subfolder_recursion_depth,
        auto_mount=source.auto_mount,
        mount_point=source.mount_point,
    )
    _apply_trigger_config_to_form(source.trigger_config, form)
    return form
