#
"""导入源表单 ⇄ DTO 的桥。

``IngestSourceDetailWidget`` 是「哑」视图：只负责控件的读写，既不认识
DTO，也不发请求。控件与字段的对应关系、``trigger_config`` 的嵌套结构、
以及「表单快照 → Create / Update」的装配全部收敛在这里 —— 与
``dto/source/command.py`` 的 ``IngestSourceForm`` / ``form_from_source``
（DTO → 表单）严格对称。

为什么放在 presenter 层而不是 DTO 层：反方向（表单 → 写模型）耦合的是
**控件**（QLineEdit / QComboBox / QListView），DTO 层不该 import Qt。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from albuswall.common.enums import FileTypeCheckMode, UpdateMode
from albuswall.dto.source.command import (
    IngestSourceCreate,
    IngestSourceForm,
    IngestSourceUpdate,
)

if TYPE_CHECKING:
    from albuswall.ui.window.source.detail_widget import (
        IngestSourceDetailWidget,
    )

__all__ = [
    "apply_form",
    "close_form",
    "read_form",
    "form_to_create",
    "form_to_update",
]

logger = logging.getLogger(__package__)

#: 「更新模式」下拉框的索引 → 枚举（与 ``_retranslate_update_mode_combo`` 同序）。
_UPDATE_MODES: tuple[UpdateMode, ...] = (
    UpdateMode.SCHEDULED_TIME,
    UpdateMode.INTERVAL_TIME,
)


# ===========================================================================
# 读取：控件 → 表单快照
# ===========================================================================

def read_form(widget: IngestSourceDetailWidget) -> IngestSourceForm:
    """把详情表单读成一份 ``IngestSourceForm``。"""
    return IngestSourceForm(
        title=widget.title_line_edit.text().strip(),
        description=widget.description_line_edit.text().strip(),
        tags=list(widget._tags_model.stringList()),
        source_path=widget.source_path_line_edit.text().strip(),
        target_path=_combo_data(widget.target_combo),
        file_types=list(widget._file_types_model.stringList()),
        file_type_check=(
            FileTypeCheckMode.MAGIC
            if widget.file_type_check_mg_radio_button.isChecked()
            else FileTypeCheckMode.SUFFIX
        ),
        subfolder_recursion=widget.subfolder_recursion_check_box.isChecked(),
        subfolder_recursion_depth=(
            None
            if widget.subfolder_recursion_depth_check_box.isChecked()
            else int(widget.subfolder_recursion_depth_spin_box.value())
        ),
        scheduled_enabled=widget.grp_scheduled.isChecked(),
        update_mode=_UPDATE_MODES[_combo_index(widget.update_mode_combo)],
        scheduled_time=widget.scheduled_time_edit.text().strip() or None,
        scheduled_interval=widget.interval_time_edit.text().strip() or None,
        device_trigger_enabled=(
            widget.grp_device_insertion_trigger.isChecked()),
        auto_mount=widget.auto_mount_checkbox.isChecked(),
        mount_point=widget.mount_point_edit.text().strip() or None,
    )


# ===========================================================================
# 回填：表单快照 → 控件
# ===========================================================================

def apply_form(
        widget: IngestSourceDetailWidget,
        form: IngestSourceForm,
) -> None:
    """把一份表单快照写回控件（不改用户的选择语义，只做展示同步）。"""
    widget.title_line_edit.setText(form.title or "")
    widget.description_line_edit.setText(form.description or "")
    widget._tags_model.setStringList([str(tag) for tag in form.tags])

    widget.source_path_line_edit.setText(form.source_path or "")
    widget._file_types_model.setStringList(
        [str(suffix) for suffix in form.file_types])

    is_magic = form.file_type_check is FileTypeCheckMode.MAGIC
    widget.file_type_check_mg_radio_button.setChecked(is_magic)
    widget.file_type_check_s_radio_button.setChecked(not is_magic)

    widget.subfolder_recursion_check_box.setChecked(
        bool(form.subfolder_recursion))
    unlimited = form.subfolder_recursion_depth is None
    widget.subfolder_recursion_depth_check_box.setChecked(unlimited)
    widget.subfolder_recursion_depth_spin_box.setEnabled(not unlimited)
    if form.subfolder_recursion_depth is not None:
        widget.subfolder_recursion_depth_spin_box.setValue(
            int(form.subfolder_recursion_depth))

    widget.grp_scheduled.setChecked(bool(form.scheduled_enabled))
    index = _UPDATE_MODES.index(
        UpdateMode.INTERVAL_TIME
        if form.update_mode is UpdateMode.INTERVAL_TIME
        else UpdateMode.SCHEDULED_TIME
    )
    widget.update_mode_combo.setCurrentIndex(index)
    widget.scheduled_time_edit.setText(form.scheduled_time or "")
    widget.interval_time_edit.setText(form.scheduled_interval or "")

    widget.grp_device_insertion_trigger.setChecked(
        bool(form.device_trigger_enabled))
    widget.auto_mount_checkbox.setChecked(bool(form.auto_mount))
    widget.mount_point_edit.setText(form.mount_point or "")

    # 下拉框里没有对应条目时保持不动（视图目前不填充目标列表）
    if form.target_path:
        target_index = widget.target_combo.findData(form.target_path)
        if target_index >= 0:
            widget.target_combo.setCurrentIndex(target_index)


def close_form(widget: IngestSourceDetailWidget) -> None:
    """把表单复位到空白初始状态（关闭详情时用）。"""
    widget.clear()


# ===========================================================================
# 表单快照 → 写模型
# ===========================================================================

def form_to_create(
        form: IngestSourceForm,
        fallback_title: str,
) -> IngestSourceCreate:
    """表单快照 → 新建 DTO。"""
    return IngestSourceCreate(
        title=form.title or fallback_title,
        description=form.description or None,
        source_path=form.source_path or "",
        target_path=form.target_path,
        mount_point=form.mount_point,
        auto_mount=form.auto_mount,
        file_type_check=form.file_type_check,
        file_types=list(form.file_types),
        tags=list(form.tags),
        subfolder_recursion=form.subfolder_recursion,
        subfolder_recursion_depth=form.subfolder_recursion_depth,
        trigger_config=form.to_trigger_config(),
    )


def form_to_update(form: IngestSourceForm) -> IngestSourceUpdate:
    """表单快照 → PATCH DTO（全字段显式设置）。

    ``disabled`` 不在此列：它是列表级的开关（右键菜单），不是详情页的
    配置字段，走 ``SourceService.enable`` / ``disable``。
    """
    return IngestSourceUpdate(
        title=form.title or "",
        description=form.description or None,
        source_path=form.source_path or "",
        target_path=form.target_path,
        mount_point=form.mount_point,
        auto_mount=form.auto_mount,
        file_type_check=form.file_type_check,
        file_types=list(form.file_types),
        tags=list(form.tags),
        subfolder_recursion=form.subfolder_recursion,
        subfolder_recursion_depth=form.subfolder_recursion_depth,
        trigger_config=form.to_trigger_config(),
    )


# ===========================================================================
# 小工具
# ===========================================================================

def _combo_index(combo: Any) -> int:
    """当前索引钳到合法范围（视图缺条目时按第 0 项处理）。"""
    index = int(combo.currentIndex())
    return index if 0 <= index < len(_UPDATE_MODES) else 0


def _combo_data(combo: Any) -> str | None:
    """当前项的 ``userData``；没有时退回文本（空串 → ``None``）。"""
    data = combo.currentData()
    if data is None:
        data = combo.currentText()
    text = str(data).strip() if data is not None else ""
    return text or None
