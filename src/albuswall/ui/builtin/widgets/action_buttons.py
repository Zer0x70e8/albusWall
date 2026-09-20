#
"""Action buttons for window controls.

This models provides a set of customizable buttons for common window actions:
close, minimize, maximize, pin (stay on top), and fold (collapse/expand).
All buttons are derived from QPushButton and emit signals or perform actions
directly on the parent window.
"""

from PySide6.QtCore import Qt, Signal, QEvent, QTimer, QRect
from PySide6.QtWidgets import QPushButton

__all__ = [
    "CloseButton",
    "MinimizeButton",
    "MaximizeButton",
    "PinButton",
    "FoldButton",
]


class CloseButton(QPushButton):
    """A button that closes the parent window when clicked."""

    def __init__(self, parent=None):
        """Initialize the close button with a cross cover."""
        super().__init__(parent)
        self.setObjectName(type(self).__name__)

        self.clicked.connect(lambda: self.window().close())
        self.setText("\u2715")  # multiplication sign (✕)



class MaximizeButton(QPushButton):
    """
    Maximize/Restore button.
    Even if the window starts from a maximized state,
    it can correctly remember the normal geometry size.

    The button toggles between two states:
        - Maximize   (cover: "🗗")
        - Restore    (cover: "🗖")
    The cover updates automatically when the window state changes externally.
    """

    def __init__(self, parent=None, title_bar_height=30, margin=8):
        """
        Initialize the maximize/restore button.

        :param parent: Optional parent widgets.
        :param title_bar_height: Height of the title bar (used for geometry consistency).
        :param margin: Margin around the button (currently unused, kept for compatibility).
        """
        super().__init__(parent)
        self.setObjectName(type(self).__name__)

        # Icons: (maximize, restore) using Unicode symbols
        self.icons = ("\U0001F5D7", "\U0001F5D6")   # 🗗 (maximize), 🗖 (restore)
        self.setText(self.icons[1])                 # Initially show restore cover
        self._normal_geometry = None                # Saved normal geometry (QRect)
        self._title_bar_height = title_bar_height   # Matches WindowResizer
        self._margin = margin
        self._in_toggle = False                     # Prevents reentrant toggling

        self.clicked.connect(self.toggle_maximize)

        win = self.window()
        if win:
            win.installEventFilter(self)
            # Delay saving normal geometry – ensures window is fully initialized.
            QTimer.singleShot(0, self._save_normal_geometry_if_needed)
            QTimer.singleShot(0, self.sync_icon)

    def sync_icon(self):
        """Update the button cover to reflect the current window maximized state."""
        if self._in_toggle:
            return
        is_max = self.window().isMaximized()
        new_text = self.icons[0] if is_max else self.icons[1]
        if self.text() != new_text:
            self.setText(new_text)

    def toggle_maximize(self):
        """
        Toggle between maximized and normal window states.

        The logic mirrors the behavior of WindowResizer's drag‑restore:
            - On restore: calls showNormal(), then forces the saved normal geometry
              (if available) to guarantee correct positioning.
            - On maximize: saves current geometry, then sets the window to fill the
              current screen's available geometry.
        """
        win = self.window()
        if self._in_toggle:
            return
        self._in_toggle = True
        try:
            if win.isMaximized():
                # ----- Restore from maximized state -----
                # 1. Let Qt attempt to restore its internal cached state.
                win.showNormal()
                # 2. Override with manually saved normal geometry (if any)
                if self._normal_geometry is not None:
                    screen_geom = win.screen().availableGeometry()
                    target_geom = self._normal_geometry

                    # If the saved geometry exactly matches the screen's available
                    # area (i.e., the window was previously maximized on this screen),
                    # shift it downward by 8 pixels to avoid covering the entire screen.
                    # This may push the window partly off‑screen, which is intended.
                    if target_geom.size() == screen_geom.size():
                        target_geom = QRect(
                            target_geom.x(),
                            target_geom.y() + 8,
                            target_geom.width(),
                            target_geom.height()
                        )

                    if win.geometry() != target_geom:
                        win.setGeometry(target_geom)
                    win.update()

                # 3. Clear the maximized window state flag.
                win.setWindowState(win.windowState() & ~Qt.WindowState.WindowMaximized)

            else:
                # ----- Maximize -----
                # Save current normal geometry.
                self._normal_geometry = win.geometry()
                # Maximize to the available geometry of the current screen.
                screen_geom = win.screen().availableGeometry()
                win.setGeometry(screen_geom)
                win.setWindowState(win.windowState() | Qt.WindowState.WindowMaximized)

        finally:
            self._in_toggle = False
            self.sync_icon()

    def _save_normal_geometry_if_needed(self):
        """
        Save the window's current geometry as the normal geometry if the window
        is neither maximized nor minimized.
        """
        win = self.window()
        if win and not win.isMaximized() and not win.isMinimized():
            self._normal_geometry = win.geometry()

    def eventFilter(self, obj, event):
        """
        Monitor the window for state changes (e.g., external maximize/restore)
        and update the button cover and normal geometry accordingly.
        """
        win = self.window()
        if obj is self.window() and event.type() == QEvent.Type.WindowStateChange:
            if not self._in_toggle:
                self.sync_icon()
                # If exiting maximized state but current geometry differs from saved,
                # force restore the saved normal geometry.
                if not win.isMaximized() and self._normal_geometry is not None:
                    if win.geometry() != self._normal_geometry:
                        win.setGeometry(self._normal_geometry)
                # If entering maximized state and no normal geometry was saved,
                # try to save it asynchronously (window may still be transitioning).
                if win.isMaximized() and self._normal_geometry is None:
                    QTimer.singleShot(0, self._save_normal_geometry_if_needed)
        return super().eventFilter(obj, event)


class MinimizeButton(QPushButton):
    """A button that minimizes the parent window when clicked."""

    def __init__(self, parent=None):
        """Initialize the minimize button with a dash cover."""
        super().__init__(parent)
        self.setObjectName(type(self).__name__)

        self.clicked.connect(lambda: self.window().showMinimized())
        self.setText("\U0001F5D5")  # minimize symbol


class PinButton(QPushButton):
    """A button that toggles the 'stay on top' (always on top) flag of the parent window."""

    def __init__(self, parent=None):
        """Initialize the pin button with two states: unpinned and pinned."""
        super().__init__(parent)
        self.setObjectName(type(self).__name__)

        self.texts = ("\U0001F4CC\uFE0E", "\U0001F4CC\uFE0E\u20E0")  # (unpinned, pinned)
        self.setText(self.texts[0])
        self.clicked.connect(self.toggle_stay)

    def toggle_stay(self):
        """Toggle the window's 'stay on top' flag and update the button cover."""
        w = self.window()
        # check if already on top
        is_on_top = bool(w.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
        w.setWindowFlag(
            Qt.WindowType.WindowStaysOnTopHint,
            not bool(w.windowFlags() & Qt.WindowType.WindowStaysOnTopHint),
        )
        w.show()

        if is_on_top:
            self.setText(self.texts[0])
        else:
            self.setText(self.texts[1])


class FoldButton(QPushButton):
    """A button that emits a fold/unfold signal. Does not change the window itself."""

    fold_signal: Signal = Signal(bool)

    def __init__(self, parent=None):
        """Initialize the fold button with two states: folded and unfolded."""
        super().__init__(parent)
        self.setObjectName(type(self).__name__)

        self.texts = ("\u23F7", "\u23F5")  # (unfolded cover, folded cover)
        self.is_folded = False
        self.setText(self.texts[0])
        self.clicked.connect(self.toggle_stay)

    def toggle_stay(self):
        """Toggle the folded state and emit fold_signal with the new state."""
        self.is_folded = not self.is_folded
        if self.is_folded:
            self.setText(self.texts[1])
            self.fold_signal.emit(self.is_folded)
        else:
            self.setText(self.texts[0])
            self.fold_signal.emit(self.is_folded)

