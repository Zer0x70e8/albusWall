#
"""导入源表单：元信息 / 源路径 / 触发器三组控件（纯展示，不含业务）。

表单不做任何 DTO ↔ 控件映射，也不发请求；读取 / 回填由外部完成。

``tag_remove_requested`` / ``file_type_remove_requested`` 是预留信号，外部接线后按
Delete 才会真的移除；未接线时按 Delete 无反应，但已防止双击 / F2 误编辑。
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QStringListModel, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ...i18n import Retranslatable
from ...utils import auto_set_object_names

__all__ = ["IngestSourceDetailWidget"]


class IngestSourceDetailWidget(Retranslatable, QFrame):
    """导入源表单。"""

    # ---- 对外信号（预留）：只声明「想移除这一项」，移除由外部接线完成 ----
    tag_remove_requested = Signal(str)
    file_type_remove_requested = Signal(str)

    # ---- 子控件声明（类型注解） ----
    main_layout: QVBoxLayout

    # ---- 元信息 ----
    grp_meta: QGroupBox
    grp_meta_layout: QFormLayout

    title_layout: QHBoxLayout
    title_label: QLabel
    title_line_edit: QLineEdit

    description_layout: QHBoxLayout
    description_label: QLabel
    description_line_edit: QLineEdit

    tags_layout: QHBoxLayout
    tags_label: QLabel
    tags_view: QListView
    tags_line_edit: QLineEdit
    tags_button: QPushButton

    # ---- 摄入源 ----
    grp_source: QGroupBox
    grp_source_layout: QFormLayout

    source_path_line_edit: QLineEdit
    source_path_line_edit_layout: QHBoxLayout
    source_path_browser_button: QPushButton
    source_path_label: QLabel

    file_type_layout: QHBoxLayout
    file_type_label: QLabel
    file_type_list_view: QListView
    file_type_line_edit: QLineEdit
    file_type_add_button: QPushButton

    file_type_check_widget: QWidget
    file_type_check_layout: QHBoxLayout
    file_type_check_label: QLabel
    file_type_check_s_radio_button: QRadioButton
    file_type_check_mg_radio_button: QRadioButton

    subfolder_recursion_layout: QHBoxLayout
    subfolder_recursion_label: QLabel
    subfolder_recursion_check_box: QCheckBox

    subfolder_recursion_depth_layout: QHBoxLayout
    subfolder_recursion_depth_label: QLabel
    subfolder_recursion_depth_check_box: QCheckBox
    subfolder_recursion_depth_spin_box: QSpinBox

    # ---- 触发器 ----
    grp_triggers: QGroupBox
    grp_triggers_layout: QVBoxLayout

    grp_scheduled: QGroupBox
    grp_scheduled_layout: QVBoxLayout

    update_mode_layout: QHBoxLayout
    update_mode_label: QLabel
    update_mode_combo: QComboBox

    scheduled_time_layout: QHBoxLayout
    scheduled_time_label: QLabel
    scheduled_time_edit: QLineEdit

    interval_time_layout: QHBoxLayout
    interval_time_label: QLabel
    interval_time_edit: QLineEdit

    disable_timer_layout: QHBoxLayout
    disable_timer_label: QLabel
    disable_timer_checkbox: QCheckBox

    grp_device_insertion_trigger: QGroupBox
    grp_device_insertion_layout: QVBoxLayout

    enable_device_trigger_layout: QHBoxLayout
    enable_device_trigger_label: QLabel
    enable_device_trigger_checkbox: QCheckBox

    target_layout: QHBoxLayout
    target_label: QLabel
    target_combo: QComboBox

    auto_mount_layout: QHBoxLayout
    auto_mount_label: QLabel
    auto_mount_checkbox: QCheckBox

    mount_point_layout: QHBoxLayout
    mount_point_label: QLabel
    mount_point_edit: QLineEdit

    # ---- 内部 model ----
    _tags_model: QStringListModel
    _file_types_model: QStringListModel

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)

        self._tags_model = QStringListModel(self)
        self._file_types_model = QStringListModel(self)

        self._setup_ui()
        self.retranslate_ui()
        self._wire_internal_signals()
        auto_set_object_names(self)
        self._apply_default_state()

    # ------------------------------------------------------------------ #
    # 布局
    # ------------------------------------------------------------------ #
    def _setup_ui(self) -> None:
        """创建所有控件并设置布局。"""
        self.main_layout = QVBoxLayout(self)

        # ---- 元信息 ----
        self.grp_meta = QGroupBox()
        self.grp_meta_layout = QFormLayout(self.grp_meta)

        self.title_layout = QHBoxLayout()
        self.title_label = QLabel(self.grp_meta)
        self.title_line_edit = QLineEdit(self.grp_meta)

        self.title_layout.addWidget(self.title_line_edit)

        self.description_layout = QHBoxLayout()
        self.description_label = QLabel(self.grp_meta)
        self.description_line_edit = QLineEdit(self.grp_meta)

        self.description_layout.addWidget(self.description_line_edit)

        self.tags_layout = QHBoxLayout()
        self.tags_label = QLabel(self.grp_meta)
        self.tags_view = QListView(self.grp_meta)
        self.tags_line_edit = QLineEdit(self.grp_meta)
        self.tags_button = QPushButton(self.grp_meta)

        self.tags_view.setFlow(QListView.Flow.LeftToRight)
        self.tags_view.setWrapping(True)
        self.tags_view.setResizeMode(QListView.ResizeMode.Adjust)
        self.tags_view.setViewMode(QListView.ViewMode.IconMode)
        self.tags_view.setSpacing(4)
        self.tags_view.setFixedHeight(40)
        # 只读展示：禁掉双击 / F2 误编辑；Tab 可聚焦与选中高亮保持不变
        self.tags_view.setEditTriggers(QListView.EditTrigger.NoEditTriggers)
        self.tags_view.installEventFilter(self)

        self.tags_layout.addWidget(self.tags_view)
        self.tags_layout.addWidget(self.tags_line_edit)
        self.tags_layout.addWidget(self.tags_button)

        self.grp_meta_layout.addRow(self.title_label, self.title_layout)
        self.grp_meta_layout.addRow(
            self.description_label, self.description_layout)
        self.grp_meta_layout.addRow(self.tags_label, self.tags_layout)

        # ---- 摄入源 ----
        self.grp_source = QGroupBox()
        self.grp_source_layout = QFormLayout(self.grp_source)

        # 源路径
        self.source_path_line_edit = QLineEdit(self.grp_source)
        self.source_path_browser_button = QPushButton(self.grp_source)
        self.source_path_label = QLabel(self.grp_source)

        self.source_path_line_edit_layout = QHBoxLayout()
        self.source_path_line_edit_layout.addWidget(
            self.source_path_line_edit, 1)
        self.source_path_line_edit_layout.addWidget(
            self.source_path_browser_button)

        # 文件类型
        self.file_type_label = QLabel(self.grp_source)
        self.file_type_list_view = QListView(self.grp_source)
        self.file_type_line_edit = QLineEdit(self.grp_source)
        self.file_type_add_button = QPushButton(self.grp_source)

        self.file_type_list_view.setFlow(QListView.Flow.LeftToRight)
        self.file_type_list_view.setWrapping(True)
        self.file_type_list_view.setResizeMode(QListView.ResizeMode.Adjust)
        self.file_type_list_view.setViewMode(QListView.ViewMode.IconMode)
        self.file_type_list_view.setSpacing(4)
        self.file_type_list_view.setFixedHeight(40)
        # 只读展示：禁掉双击 / F2 误编辑；Tab 可聚焦与选中高亮保持不变
        self.file_type_list_view.setEditTriggers(
            QListView.EditTrigger.NoEditTriggers)
        self.file_type_list_view.installEventFilter(self)

        self.file_type_layout = QHBoxLayout()
        self.file_type_layout.addWidget(self.file_type_list_view, 2)
        self.file_type_layout.addWidget(self.file_type_line_edit, 1)
        self.file_type_layout.addWidget(self.file_type_add_button)

        # 文件类型校验方式
        self.file_type_check_widget = QWidget(self.grp_source)
        self.file_type_check_layout = QHBoxLayout(self.file_type_check_widget)
        self.file_type_check_label = QLabel(self.grp_source)
        self.file_type_check_s_radio_button = QRadioButton(self.grp_source)
        self.file_type_check_mg_radio_button = QRadioButton(self.grp_source)

        self.file_type_check_layout.setContentsMargins(0, 0, 0, 0)
        self.file_type_check_layout.addWidget(
            self.file_type_check_s_radio_button)
        self.file_type_check_layout.addWidget(
            self.file_type_check_mg_radio_button)

        # 子文件夹递归
        self.subfolder_recursion_label = QLabel(self.grp_source)
        self.subfolder_recursion_check_box = QCheckBox(self.grp_source)

        self.subfolder_recursion_layout = QHBoxLayout()
        self.subfolder_recursion_layout.addWidget(
            self.subfolder_recursion_check_box)
        self.subfolder_recursion_layout.addStretch(1)

        # 递归深度
        self.subfolder_recursion_depth_label = QLabel(self.grp_source)
        self.subfolder_recursion_depth_check_box = QCheckBox(self.grp_source)
        self.subfolder_recursion_depth_spin_box = QSpinBox(self.grp_source)

        self.subfolder_recursion_depth_spin_box.setRange(0, 16777215)

        self.subfolder_recursion_depth_layout = QHBoxLayout()
        self.subfolder_recursion_depth_layout.addWidget(
            self.subfolder_recursion_depth_check_box)
        self.subfolder_recursion_depth_layout.addWidget(
            self.subfolder_recursion_depth_spin_box)
        self.subfolder_recursion_depth_layout.addStretch(1)

        self.grp_source_layout.addRow(
            self.source_path_label, self.source_path_line_edit_layout)
        self.grp_source_layout.addRow(
            self.file_type_label, self.file_type_layout)
        self.grp_source_layout.addRow(
            self.file_type_check_label, self.file_type_check_widget)
        self.grp_source_layout.addRow(
            self.subfolder_recursion_label, self.subfolder_recursion_layout)
        self.grp_source_layout.addRow(
            self.subfolder_recursion_depth_label,
            self.subfolder_recursion_depth_layout)

        spacer = QWidget()
        spacer.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        self.grp_source_layout.addRow(spacer)

        # ---- 触发器 ----
        self.grp_triggers = QGroupBox(self)
        self.grp_triggers_layout = QVBoxLayout(self.grp_triggers)

        self.grp_scheduled = QGroupBox(self.grp_triggers)
        self.grp_scheduled.setCheckable(True)
        self.grp_scheduled_layout = QVBoxLayout(self.grp_scheduled)

        self.update_mode_layout = QHBoxLayout()
        self.update_mode_label = QLabel(self.grp_scheduled)
        self.update_mode_combo = QComboBox(self.grp_scheduled)
        self.update_mode_layout.addWidget(self.update_mode_label, 2)
        self.update_mode_layout.addWidget(self.update_mode_combo)

        self.scheduled_time_layout = QHBoxLayout()
        self.scheduled_time_label = QLabel(self.grp_scheduled)
        self.scheduled_time_edit = QLineEdit(self.grp_scheduled)

        self.scheduled_time_layout.addWidget(self.scheduled_time_label, 2)
        self.scheduled_time_layout.addWidget(self.scheduled_time_edit)

        self.interval_time_layout = QHBoxLayout()
        self.interval_time_label = QLabel(self.grp_scheduled)
        self.interval_time_edit = QLineEdit(self.grp_scheduled)

        self.interval_time_layout.addWidget(self.interval_time_label, 2)
        self.interval_time_layout.addWidget(self.interval_time_edit)

        self.grp_scheduled_layout.addLayout(self.update_mode_layout)
        self.grp_scheduled_layout.addLayout(self.scheduled_time_layout)
        self.grp_scheduled_layout.addLayout(self.interval_time_layout)

        # ---- 设备插入触发器 ----
        self.grp_device_insertion_trigger = QGroupBox(self.grp_triggers)
        self.grp_device_insertion_trigger.setCheckable(True)
        self.grp_device_insertion_layout = QVBoxLayout(
            self.grp_device_insertion_trigger)

        self.target_layout = QHBoxLayout()
        self.target_label = QLabel(self.grp_device_insertion_trigger)
        self.target_combo = QComboBox(self.grp_device_insertion_trigger)
        self.target_layout.addWidget(self.target_label, 2)
        self.target_layout.addWidget(self.target_combo)

        self.auto_mount_layout = QHBoxLayout()
        self.auto_mount_label = QLabel(self.grp_device_insertion_trigger)
        self.auto_mount_checkbox = QCheckBox(self.grp_device_insertion_trigger)
        self.auto_mount_layout.addWidget(self.auto_mount_label, 2)
        self.auto_mount_layout.addWidget(self.auto_mount_checkbox)

        self.mount_point_layout = QHBoxLayout()
        self.mount_point_label = QLabel(self.grp_device_insertion_trigger)
        self.mount_point_edit = QLineEdit(self.grp_device_insertion_trigger)

        self.mount_point_layout.addWidget(self.mount_point_label, 2)
        self.mount_point_layout.addWidget(self.mount_point_edit)

        self.grp_device_insertion_layout.addLayout(self.target_layout)
        self.grp_device_insertion_layout.addLayout(self.auto_mount_layout)
        self.grp_device_insertion_layout.addLayout(self.mount_point_layout)

        self.grp_triggers_layout.addWidget(self.grp_scheduled)
        self.grp_triggers_layout.addWidget(self.grp_device_insertion_trigger)

        # ---- 组装 ----
        self.main_layout.addWidget(self.grp_meta)
        self.main_layout.addWidget(self.grp_source)
        self.main_layout.addWidget(self.grp_triggers)
        self.main_layout.addStretch()

    # ------------------------------------------------------------------ #
    # 文案
    # ------------------------------------------------------------------ #
    def retranslate_ui(self) -> None:
        """重新应用全部文案 / 占位提示（语言切换时由 TranslationManager 调用）。

        注意：下拉框的条目要按索引改文案，否则会把用户的当前选择重置回第一项。
        """
        # ---- meta ----
        self.grp_meta.setTitle(self.tr("元信息"))
        self.title_label.setText(self.tr("标题"))
        self.title_line_edit.setPlaceholderText(self.tr("新建导入源"))
        self.description_label.setText(self.tr("描述"))
        self.description_line_edit.setPlaceholderText(self.tr("示例"))
        self.tags_label.setText(self.tr("标签："))
        self.tags_line_edit.setPlaceholderText(self.tr("标签1"))
        self.tags_button.setText(self.tr("添加"))

        # ---- source ----
        self.grp_source.setTitle(self.tr("导入源"))
        self.source_path_browser_button.setText(self.tr("浏览"))
        self.source_path_label.setText(self.tr("源路径（图库路径）"))
        self.file_type_label.setText(self.tr("文件类型"))
        self.file_type_add_button.setText(self.tr("添加"))
        self.file_type_check_label.setText(
            self.tr("文件类型校验方式"))
        self.file_type_check_s_radio_button.setText(self.tr("后缀检查"))
        self.file_type_check_mg_radio_button.setText(
            self.tr("魔数检查"))
        self.subfolder_recursion_label.setText(
            self.tr("子文件夹递归"))
        self.subfolder_recursion_depth_label.setText(
            self.tr("子文件夹递归深度"))
        self.subfolder_recursion_depth_check_box.setText(self.tr("不限"))

        # ---- triggers ----
        self.grp_triggers.setTitle(self.tr("触发器"))
        self.grp_scheduled.setTitle(self.tr("定时更新"))
        self.update_mode_label.setText(self.tr("模式"))
        self._retranslate_update_mode_combo()
        self.scheduled_time_label.setText(self.tr("定时时间"))
        self.scheduled_time_edit.setPlaceholderText("HH:MM")
        self.interval_time_label.setText(self.tr("间隔时间"))
        self.interval_time_edit.setPlaceholderText(self.tr("例如 30m 或 1h"))

        self.grp_device_insertion_trigger.setTitle(
            self.tr("设备插入触发器"))
        self.target_label.setText(self.tr("目标"))
        self.auto_mount_label.setText(
            self.tr("自动挂载（仅 Linux，需要权限）"))
        self.mount_point_label.setText(self.tr("挂载点"))
        self.mount_point_edit.setPlaceholderText(
            "/media/example/auto_import")

    def _retranslate_update_mode_combo(self) -> None:
        """重写「更新模式」两个条目，保留当前选择。"""
        labels = (self.tr("定时时间"), self.tr("间隔时间"))
        blocked = self.update_mode_combo.blockSignals(True)
        try:
            current = self.update_mode_combo.currentIndex()
            if self.update_mode_combo.count() == 0:
                for label in labels:
                    self.update_mode_combo.addItem(label)
                self.update_mode_combo.setCurrentIndex(0)
            else:
                for index, label in enumerate(labels):
                    if index < self.update_mode_combo.count():
                        self.update_mode_combo.setItemText(index, label)
                self.update_mode_combo.setCurrentIndex(current)
        finally:
            self.update_mode_combo.blockSignals(blocked)

    # ------------------------------------------------------------------ #
    # 内部连线
    # ------------------------------------------------------------------ #
    def _wire_internal_signals(self) -> None:
        self.tags_view.setModel(self._tags_model)
        self.file_type_list_view.setModel(self._file_types_model)

        self.tags_button.clicked.connect(self._on_add_tag)
        self.tags_line_edit.returnPressed.connect(self._on_add_tag)
        self.file_type_add_button.clicked.connect(self._on_add_file_type)
        self.file_type_line_edit.returnPressed.connect(self._on_add_file_type)

        self.source_path_browser_button.clicked.connect(
            self._on_browse_source_path)
        self.update_mode_combo.currentIndexChanged.connect(
            self._on_update_mode_changed)
        self.subfolder_recursion_depth_check_box.toggled.connect(
            lambda checked: self.subfolder_recursion_depth_spin_box.setEnabled(
                not checked))

    def _apply_default_state(self) -> None:
        self.file_type_check_s_radio_button.setChecked(True)
        self.subfolder_recursion_check_box.setChecked(False)
        self.subfolder_recursion_depth_check_box.setChecked(True)
        self.subfolder_recursion_depth_spin_box.setEnabled(False)
        self.update_mode_combo.setCurrentIndex(0)
        self._on_update_mode_changed(0)

    # ------------------------------------------------------------------ #
    # 小交互
    # ------------------------------------------------------------------ #
    def _on_add_tag(self) -> None:
        text = self.tags_line_edit.text().strip()
        if not text:
            return
        tags = self._tags_model.stringList()
        if text not in tags:
            tags.append(text)
            self._tags_model.setStringList(tags)
        self.tags_line_edit.clear()

    def _on_add_file_type(self) -> None:
        text = self.file_type_line_edit.text().strip()
        if not text:
            return
        if not text.startswith("."):
            text = "." + text
        types = self._file_types_model.stringList()
        if text not in types:
            types.append(text)
            self._file_types_model.setStringList(types)
        self.file_type_line_edit.clear()

    def _on_browse_source_path(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, self.tr("选择源目录"),
            self.source_path_line_edit.text() or "",
        )
        if path:
            self.source_path_line_edit.setText(path)

    def _on_update_mode_changed(self, index: int) -> None:
        is_interval = index == 1
        self.scheduled_time_edit.setVisible(not is_interval)
        self.scheduled_time_label.setVisible(not is_interval)
        self.interval_time_edit.setVisible(is_interval)
        self.interval_time_label.setVisible(is_interval)

    # ------------------------------------------------------------------ #
    # 键盘：两个只读列表的「移除」意图
    # ------------------------------------------------------------------ #
    def eventFilter(self, obj, event):  # noqa: N802 - Qt 命名
        """Delete / Backspace 在只读列表上抛移除意图（展示副本本身不动）。"""
        if (event.type() == QEvent.Type.KeyPress
                and event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace)):
            if obj is self.tags_view:
                self._emit_remove(self.tags_view, self.tag_remove_requested)
                event.accept()
                return True
            if obj is self.file_type_list_view:
                self._emit_remove(self.file_type_list_view,
                                  self.file_type_remove_requested)
                event.accept()
                return True
        return super().eventFilter(obj, event)

    @staticmethod
    def _emit_remove(view: QListView, signal) -> None:
        """把「想移除这一项」按 DisplayRole 文本抛给外部；无选中项时什么也不做。"""
        index = view.currentIndex()
        if not index.isValid():
            return
        signal.emit(index.data(Qt.ItemDataRole.DisplayRole))

    # ------------------------------------------------------------------ #
    # 纯视图侧的复位接口
    # ------------------------------------------------------------------ #
    def clear(self) -> None:
        """把表单复位到空白初始状态。"""
        self.title_line_edit.clear()
        self.description_line_edit.clear()
        self._tags_model.setStringList([])
        self.source_path_line_edit.clear()
        self._file_types_model.setStringList([])
        self.file_type_check_s_radio_button.setChecked(True)
        self.subfolder_recursion_check_box.setChecked(False)
        self.subfolder_recursion_depth_check_box.setChecked(True)
        self.subfolder_recursion_depth_spin_box.setEnabled(False)
        self.grp_scheduled.setChecked(False)
        self.update_mode_combo.setCurrentIndex(0)
        self.scheduled_time_edit.clear()
        self.interval_time_edit.clear()
        self.grp_device_insertion_trigger.setChecked(False)
        self.auto_mount_checkbox.setChecked(False)
        self.mount_point_edit.clear()
        self._on_update_mode_changed(self.update_mode_combo.currentIndex())


if __name__ == "__main__":
    def _main() -> int:  # pragma: no cover - 手动预览入口
        import sys

        from PySide6.QtWidgets import QApplication

        app = QApplication(sys.argv)

        window = QWidget()
        widget = IngestSourceDetailWidget(window)
        layout = QVBoxLayout(window)
        layout.addWidget(widget)
        layout.addStretch()
        window.resize(600, 800)
        window.setStyleSheet("""
        QGroupBox {
            font-weight: bold;
            margin-top: 12px;
            padding-top: 10px;
        }
        QGroupBox::title {
            subcontrol-origin: margin;
            left: 10px;
            padding: 0 5px;
        }
        #IngestSourceDetailWidget QPushButton{
            max-width: 4em
        }
        QGroupBox#IngestSourceDetailWidgetGrpScheduled,
        QGroupBox#IngestSourceDetailWidgetGrpDeviceInsertionTrigger {
            margin-top: 12px !important;
            padding-top: 20px !important;
        }
        QGroupBox#IngestSourceDetailWidgetGrpScheduled::title,
        QGroupBox#IngestSourceDetailWidgetGrpDeviceInsertionTrigger::title {
            subcontrol-origin: margin !important;
            subcontrol-position: top left !important;
            left: 10px !important;
            padding: 0 4px !important;
        }
        """)
        window.show()
        return app.exec()


    raise SystemExit(_main())
