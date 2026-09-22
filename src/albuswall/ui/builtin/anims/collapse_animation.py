#
""""""

from PySide6.QtCore import QPropertyAnimation, QEasingCurve


class CollapseAnimation:
    """
    Base class / interface for collapse animation.

    Provides a default no-animation implementation: directly toggle the widgets's visible state
    and instantly set its maximum height. Subclasses can override these methods to add animation
    effects (e.g., HeightSlideAnimation).
    """

    def stop(self):
        """Stop the current animation (if any)."""
        pass

    def animate_expand(self,
                       content_widget,
                       group_box,
                       on_finished=None
                       ):
        """
        Perform expand operation.

        Default behavior: immediately remove
        the maximum height restriction (set to a very large value),
        then show the widgets, and call the optional callback on_finished when done.

        Parameters:
            content_widget: The content widgets to expand (usually a QWidget).
            group_box: The container that holds content_widget (not used directly by this base class,
                       available for subclasses).
            on_finished: Callback function (no arguments) called after the expand operation completes.
        """
        content_widget.setMaximumHeight(16777215)
        content_widget.show()
        if on_finished:
            on_finished()

    def animate_collapse(self,
                         content_widget,
                         group_box,
                         on_finished=None
                         ):
        """
        Perform collapse operation.

        Default behavior: hide the widgets and immediately set its maximum height to 0,
        then call on_finished.

        Note:
            Using hide() makes the widgets invisible, while setMaximumHeight(0) ensures the layout
            does not reserve space for it. If only hide() is used without restricting height,
            some layouts may still allocate space.
        """
        content_widget.hide()
        content_widget.setMaximumHeight(0)
        if on_finished:
            on_finished()


class HeightSlideAnimation(CollapseAnimation):
    """
    Sliding animation based on maximum height gradient.
    """

    def __init__(self, duration=250):
        """
        Parameters:
            duration: Animation duration in milliseconds. Default is 250 ms.
        """
        super().__init__()
        self._animation = None  # Current running QPropertyAnimation instance
        self._duration = duration  # Animation duration

    def stop(self):
        if self._animation and self._animation.state() == QPropertyAnimation.State.Running:
            self._animation.stop()

    def animate_expand(self,
                       content_widget,
                       group_box, on_finished=None
                       ):
        self.stop()

        # Show the widgets (ensure it is not hidden,
        #   otherwise the height animation cannot work)
        content_widget.show()

        # Temporarily remove the height limit
        #   so the layout can calculate the full content height
        content_widget.setMaximumHeight(16777215)
        full_height = content_widget.sizeHint().height()

        # Determine the starting height for the animation
        # If the widgets is not visible (theoretically collapsed), start from 0;
        # otherwise start from the current visible height
        #   (supports interrupted continuous animation)
        start_h = 0 if not content_widget.isVisible() else content_widget.height()

        # Immediately restrict the height to the starting value to prepare
        # the starting frame for the animation
        content_widget.setMaximumHeight(start_h)

        # Start animation: transition from current restricted height to the full height
        self._start_animation(content_widget, start_h, full_height, on_finished)

    def animate_collapse(self,
                         content_widget,
                         group_box,
                         on_finished=None
                         ):
        self.stop()

        start_h = content_widget.height()
        # Lock the current height to avoid an immediate jump
        content_widget.setMaximumHeight(start_h)

        # Animate to 0
        self._start_animation(content_widget, start_h, 0, on_finished)

    def _start_animation(self, target, start_value, end_value, on_finished):
        self._animation = QPropertyAnimation(target, b"maximumHeight")
        self._animation.setDuration(self._duration)
        self._animation.setStartValue(start_value)
        self._animation.setEndValue(end_value)
        self._animation.setEasingCurve(QEasingCurve.Type.InOutQuad)  # easing curve

        if on_finished:
            # Note: the finished signal is only emitted when the animation finishes naturally;
            # if the animation is stopped via stop(), it will not be emitted.
            # This avoids erroneously calling the completion callback on interruption.
            self._animation.finished.connect(on_finished)

        self._animation.start()
