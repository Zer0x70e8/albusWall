#
""""""

try:
    from .basic import Setting as _Setting
    from .about import About
except ImportError:
    from albuswall.ui.builtin.setting.basic import Setting as _Setting
    from albuswall.ui.builtin.setting.about import About

JUMO_BUTTON_OBJ_NAME_SUFFIX = "JumpButton"


class Setting(_Setting):
    about: About

    def __init__(self, parent=None, auto_setup=True):
        super().__init__(parent, auto_setup=auto_setup)

        self.setup_ui()

    def setup_ui(self):
        self.about = About("about", self)

        self.content_layout.addWidget(self.about)
        self.add_jump_button(
            self.about,
            self.tr("about"),
            f"SettingAbout{JUMO_BUTTON_OBJ_NAME_SUFFIX}"
        )

        self.content_layout.addStretch()
        self.nav_layout.addStretch()


if __name__ == "__main__":
    import sys
    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    window = Setting()
    window.setStyleSheet("""
        CollapsibleGroupBox {
            border: 1px solid #aaa;
            border-radius: 4px;
            margin-top: 1.2em;
            font-weight: bold;
            qproperty-simble_on_extend: "↓";
            qproperty-simble_on_collapsible: "→";
        }
        CollapsibleGroupBox::title {
            subcontrol-origin: margin;
            left: 20px;
            padding: 0 5px;
            top: 0.8em;
        }
        CollapsibleGroupBox[collapsed="true"] {
            border: none;
            border-top: 1px solid #aaa;
            background: transparent;
        }
        CollapsibleGroupBox[collapsed="true"]::title {
            background: palette(window);
        }
        """)

    window.show()
    sys.exit(app.exec())
