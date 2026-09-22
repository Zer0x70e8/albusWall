#
""""""

from typing import Final

from PySide6.QtWidgets import QVBoxLayout

try:
    from ..widgets.collapsible_group_box import CollapsibleGroupBox
    from ..anims.collapse_animation import HeightSlideAnimation
except ImportError:
    from albuswall.ui.builtin.widgets.collapsible_group_box import CollapsibleGroupBox
    from albuswall.ui.builtin.anims.collapse_animation import HeightSlideAnimation


DISABLE_ANIM: Final = False
ANIM_TIME: Final = 300


class Example(CollapsibleGroupBox):
    def __init__(self, title, parent=None, anim=DISABLE_ANIM):
        if anim:
            super().__init__(title, parent=parent)
        else:
            super().__init__(title, parent=parent, animation=HeightSlideAnimation(ANIM_TIME))

    def setup_ui(self):
        layout = QVBoxLayout(self.contentWidget())

        layout.setSpacing(0)
        layout.setContentsMargins(0 ,0 ,0 ,0)

        self.contentWidget().setLayout(layout)
