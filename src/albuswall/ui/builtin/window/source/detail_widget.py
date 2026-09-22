#
""""""

from PySide6.QtCore import QObject
from PySide6.QtWidgets import (
    QFrame, QVBoxLayout, QHBoxLayout, QFormLayout,
    QGroupBox, QLabel, QLineEdit, QPushButton,
    QCheckBox, QSpinBox, QRadioButton, QComboBox,
    QWidget, QApplication, QListView, QSizePolicy
)


class IngestSourceDetailWidget(QFrame):
    main_layout: QVBoxLayout

    #
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

    #
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

    #
    grp_triggers: QGroupBox
    grp_triggers_layout: QVBoxLayout

    #
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

    #
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

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setup_ui()
        self.setup_object_names()

    def setup_ui(self):
        """创建所有控件并设置布局"""
        self.main_layout = QVBoxLayout(self)

        # ====
        self.grp_meta = QGroupBox(self.tr("meta info"))
        self.grp_meta_layout = QFormLayout(self.grp_meta)

        #
        self.title_layout = QHBoxLayout()
        self.title_label = QLabel(self.tr("title"), self.grp_meta)
        self.title_line_edit = QLineEdit(self.tr("new ingest source"), self.grp_meta)

        self.title_layout.addWidget(self.title_line_edit)

        self.description_layout = QHBoxLayout()
        self.description_label = QLabel(self.tr("description"), self.grp_meta)
        self.description_line_edit = QLineEdit(self.tr("example"), self.grp_meta)

        self.description_layout.addWidget(self.description_line_edit)

        self.tags_layout = QHBoxLayout()
        self.tags_label = QLabel(self.tr("tag: "), self.grp_meta)
        self.tags_view = QListView(self.grp_meta)
        self.tags_line_edit = QLineEdit(self.tr("tag1"), self.grp_meta)
        self.tags_button = QPushButton(self.tr("add"), self.grp_meta)

        self.tags_view.setFlow(QListView.Flow.LeftToRight)
        self.tags_view.setWrapping(True)
        self.tags_view.setResizeMode(QListView.ResizeMode.Adjust)
        self.tags_view.setViewMode(QListView.ViewMode.IconMode)
        self.tags_view.setSpacing(4)
        self.tags_view.setFixedHeight(40)

        self.tags_layout.addWidget(self.tags_view)
        self.tags_layout.addWidget(self.tags_line_edit)
        self.tags_layout.addWidget(self.tags_button)

        self.grp_meta_layout.addRow(self.title_label, self.title_layout)
        self.grp_meta_layout.addRow(self.description_label, self.description_layout)
        self.grp_meta_layout.addRow(self.tags_label, self.tags_layout)


        # ========== 摄入源分组 ==========
        self.grp_source = QGroupBox(self.tr("Ingest Source"))
        self.grp_source_layout = QFormLayout(self.grp_source)

        # 源路径
        self.source_path_line_edit = QLineEdit(self.grp_source)
        self.source_path_browser_button = QPushButton(self.tr("browser"), self.grp_source)
        self.source_path_label = QLabel(self.tr("source path (library path)"), self.grp_source)

        self.source_path_line_edit_layout = QHBoxLayout()
        self.source_path_line_edit_layout.addWidget(self.source_path_line_edit, 1)
        self.source_path_line_edit_layout.addWidget(self.source_path_browser_button)

        # 文件类型
        self.file_type_label = QLabel(self.tr("file type(s)"), self.grp_source)
        self.file_type_list_view = QListView(self.grp_source)
        self.file_type_line_edit = QLineEdit(self.grp_source)
        self.file_type_add_button = QPushButton(self.tr("add"), self.grp_source)

        self.file_type_list_view.setFlow(QListView.Flow.LeftToRight)
        self.file_type_list_view.setWrapping(True)
        self.file_type_list_view.setResizeMode(QListView.ResizeMode.Adjust)
        self.file_type_list_view.setViewMode(QListView.ViewMode.IconMode)
        self.file_type_list_view.setSpacing(4)
        self.file_type_list_view.setFixedHeight(40)

        self.file_type_layout = QHBoxLayout()
        self.file_type_layout.addWidget(self.file_type_list_view, 2)
        self.file_type_layout.addWidget(self.file_type_line_edit, 1)
        self.file_type_layout.addWidget(self.file_type_add_button)

        # 文件类型校验方式
        self.file_type_check_widget = QWidget(self.grp_source)
        self.file_type_check_layout = QHBoxLayout(self.file_type_check_widget)
        self.file_type_check_label = QLabel(self.tr("File type verification method"), self.grp_source)
        self.file_type_check_s_radio_button = QRadioButton(self.tr("Suffix Check"), self.grp_source)
        self.file_type_check_mg_radio_button = QRadioButton(self.tr("Magic Number Check"), self.grp_source)

        self.file_type_check_layout.setContentsMargins(0, 0, 0, 0)
        self.file_type_check_layout.addWidget(self.file_type_check_s_radio_button)
        self.file_type_check_layout.addWidget(self.file_type_check_mg_radio_button)

        # 子文件夹递归
        self.subfolder_recursion_label = QLabel(self.tr("Subfolder recursion"), self.grp_source)
        self.subfolder_recursion_check_box = QCheckBox(self.grp_source)

        self.subfolder_recursion_layout = QHBoxLayout()
        self.subfolder_recursion_layout.addWidget(self.subfolder_recursion_check_box)
        self.subfolder_recursion_layout.addStretch(1)

        # 递归深度
        self.subfolder_recursion_depth_label = QLabel(self.tr("Subfolder recursion depth"), self.grp_source)
        self.subfolder_recursion_depth_check_box = QCheckBox(self.tr("no limit"), self.grp_source)
        self.subfolder_recursion_depth_spin_box = QSpinBox(self.grp_source)

        self.subfolder_recursion_depth_spin_box.setRange(0, 16777215)

        self.subfolder_recursion_depth_layout = QHBoxLayout()
        self.subfolder_recursion_depth_layout.addWidget(self.subfolder_recursion_depth_check_box)
        self.subfolder_recursion_depth_layout.addWidget(self.subfolder_recursion_depth_spin_box)
        self.subfolder_recursion_depth_layout.addStretch(1)

        self.grp_source_layout.addRow(self.source_path_label, self.source_path_line_edit_layout)
        self.grp_source_layout.addRow(self.file_type_label, self.file_type_layout)
        self.grp_source_layout.addRow(self.file_type_check_label, self.file_type_check_widget)
        self.grp_source_layout.addRow(self.subfolder_recursion_label, self.subfolder_recursion_layout)
        self.grp_source_layout.addRow(self.subfolder_recursion_depth_label, self.subfolder_recursion_depth_layout)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        self.grp_source_layout.addRow(spacer)

        # ====
        self.grp_triggers = QGroupBox(self.tr("Triggers"), self)
        self.grp_triggers_layout = QVBoxLayout(self.grp_triggers)

        self.grp_scheduled = QGroupBox(self.tr("Scheduled Update"), self.grp_triggers)
        self.grp_scheduled.setCheckable(True)
        self.grp_scheduled_layout = QVBoxLayout(self.grp_scheduled)

        self.update_mode_layout = QHBoxLayout()
        self.update_mode_label = QLabel(self.tr("mode"), self.grp_scheduled)
        self.update_mode_combo = QComboBox(self.grp_scheduled)
        self.update_mode_combo.addItem(self.tr("Scheduled Time"))
        self.update_mode_combo.addItem(self.tr("Interval Time"))
        self.update_mode_layout.addWidget(self.update_mode_label, 2)
        self.update_mode_layout.addWidget(self.update_mode_combo)

        self.scheduled_time_layout = QHBoxLayout()
        self.scheduled_time_label = QLabel(self.tr("Scheduled Time"), self.grp_scheduled)
        self.scheduled_time_edit = QLineEdit(self.grp_scheduled)
        self.scheduled_time_edit.setPlaceholderText("HH:MM")
        self.scheduled_time_layout.addWidget(self.scheduled_time_label, 2)
        self.scheduled_time_layout.addWidget(self.scheduled_time_edit)

        self.interval_time_layout = QHBoxLayout()
        self.interval_time_label = QLabel(self.tr("Interval Time"), self.grp_scheduled)
        self.interval_time_edit = QLineEdit(self.grp_scheduled)
        self.interval_time_edit.setPlaceholderText(self.tr("e.g., 30m or 1h"))
        self.interval_time_layout.addWidget(self.interval_time_label, 2)
        self.interval_time_layout.addWidget(self.interval_time_edit)

        self.grp_scheduled_layout.addLayout(self.update_mode_layout)
        self.grp_scheduled_layout.addLayout(self.scheduled_time_layout)
        self.grp_scheduled_layout.addLayout(self.interval_time_layout)

        # ----- 设备插入触发器 -----
        self.grp_device_insertion_trigger = QGroupBox(self.tr("Device Insertion Trigger"), self.grp_triggers)
        self.grp_device_insertion_trigger.setCheckable(True)
        self.grp_device_insertion_layout = QVBoxLayout(self.grp_device_insertion_trigger)

        self.target_layout = QHBoxLayout()
        self.target_label = QLabel(self.tr("Target"), self.grp_device_insertion_trigger)
        self.target_combo = QComboBox(self.grp_device_insertion_trigger)
        self.target_layout.addWidget(self.target_label, 2)
        self.target_layout.addWidget(self.target_combo)

        self.auto_mount_layout = QHBoxLayout()
        self.auto_mount_label = QLabel(self.tr("Auto-mount (Linux only, requires permissions)"),
                                       self.grp_device_insertion_trigger)
        self.auto_mount_checkbox = QCheckBox(self.grp_device_insertion_trigger)
        self.auto_mount_layout.addWidget(self.auto_mount_label, 2)
        self.auto_mount_layout.addWidget(self.auto_mount_checkbox)

        self.mount_point_layout = QHBoxLayout()
        self.mount_point_label = QLabel(self.tr("Mount point"), self.grp_device_insertion_trigger)
        self.mount_point_edit = QLineEdit(self.grp_device_insertion_trigger)
        self.mount_point_edit.setPlaceholderText("/media/example/auto_import")
        self.mount_point_layout.addWidget(self.mount_point_label, 2)
        self.mount_point_layout.addWidget(self.mount_point_edit)

        self.grp_device_insertion_layout.addLayout(self.target_layout)
        self.grp_device_insertion_layout.addLayout(self.auto_mount_layout)
        self.grp_device_insertion_layout.addLayout(self.mount_point_layout)

        self.grp_triggers_layout.addWidget(self.grp_scheduled)
        self.grp_triggers_layout.addWidget(self.grp_device_insertion_trigger)

        self.main_layout.addWidget(self.grp_meta)
        self.main_layout.addWidget(self.grp_source)
        self.main_layout.addWidget(self.grp_triggers)
        self.main_layout.addStretch()

    def setup_object_names(self):
        """为自身及所有绑定在 self 上的控件/布局设置唯一 objectName"""
        prefix = type(self).__name__
        self.setObjectName(prefix)

        for attr_name, value in self.__dict__.items():
            if isinstance(value, QObject) and not attr_name.startswith('_'):
                camel_name = ''.join(part.capitalize() for part in attr_name.split('_'))
                obj_name = f"{prefix}{camel_name}"
                value.setObjectName(obj_name)


if __name__ == "__main__":
    import sys
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
    sys.exit(app.exec())