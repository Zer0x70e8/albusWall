#
""""""

from typing import Final, cast

from PySide6.QtWidgets import QVBoxLayout, QLabel

try:
    from ..widgets.collapsible_group_box import CollapsibleGroupBox
    from ..anims.collapse_animation import HeightSlideAnimation
except ImportError:
    from albuswall.ui.builtin.widgets.collapsible_group_box import CollapsibleGroupBox
    from albuswall.ui.builtin.anims.collapse_animation import HeightSlideAnimation

from albuswall.__about__ import (
    __title__, __version__, __author__,
    __doc__ as __about_doc__
)


DISABLE_ANIM: Final = False
ANIM_TIME: Final = 300


class About(CollapsibleGroupBox):
    def __init__(self, title, parent=None, enable_anim=not DISABLE_ANIM):
        if enable_anim:
            super().__init__(title, parent=parent, animation=HeightSlideAnimation(ANIM_TIME))
        else:
            super().__init__(title, parent=parent)
        self.setObjectName("SettingAbout")

        self.content_text_label = None

        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout(self.contentWidget())

        self.content_text_label = QLabel(self)
        self.content_text_label.setText(f"""
            \t{'\n\t'.join(
            cast(str, __about_doc__).split("\n")
        )}
            \ttitle: {__title__}
            \tauthor: {__author__}
            \tversion: {__version__}
            \tThanks for using it.
            """)

        layout.setSpacing(0)
        layout.setContentsMargins(0 ,0 ,0 ,0)

        layout.addWidget(self.content_text_label)
        self.contentWidget().setLayout(layout)
