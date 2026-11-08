#
"""自绘边框窗口的交互管理器（``albuswall.ui.platform.WindowResizer``）。

覆盖三件事：

* 主窗口构造时**真的装上了**管理器（否则无边框窗口既不能拖动也不能缩放）；
* 缩放方向的边距判定（纯函数，不碰 Qt 事件循环）；
* 交互控件上的左键按下**不会**被当成「拖窗口」，标题栏空白区的左键按下
  会（后者用 ``is_dragging`` 观测，不依赖平台真的开始系统拖拽）。

Qt 需要 offscreen 平台才能在无显示环境跑；必须在 import PySide6 之前设好。
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")


# ==================== fixture ====================

@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app
    app.setStyleSheet("")


@pytest.fixture()
def window(qapp):
    """真实主窗口：显示出来，布局跑完（标题栏高度才有值）。"""
    from albuswall.ui.window import Window

    w = Window()
    w.resize(640, 480)
    w.show()
    qapp.processEvents()
    yield w
    w.hide()
    w.deleteLater()
    qapp.processEvents()


def _press(window, x: int, y: int):
    """在窗口内坐标 (x, y) 处按一次左键（走应用级事件过滤器）。"""
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication

    global_pos = QPointF(window.mapToGlobal(window.rect().topLeft())) + QPointF(x, y)
    local = window.mapFromGlobal(global_pos.toPoint())
    child = window.childAt(local) or window
    event = QMouseEvent(
        QMouseEvent.Type.MouseButtonPress,
        QPointF(child.mapFrom(window, local)),
        global_pos,
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(child, event)
    QApplication.processEvents()
    return child, event


# ==================== 装配 ====================

def test_window_installs_resizer(window):
    """主窗口构造时就把管理器装上，并绑定到窗口本身。"""
    from albuswall.ui.platform import WindowResizer

    resizer = window.window_resizer
    assert isinstance(resizer, WindowResizer)
    assert resizer.win is window
    assert resizer.margin > 0


def test_title_bar_region_is_the_action_bar(window):
    """拖拽区 = 操作栏实测矩形：操作栏里算，工具栏的空白不算。"""
    from PySide6.QtCore import QPoint

    resizer = window.window_resizer
    action_bar = window.title_bar.action_bar
    assert action_bar.isVisible()

    top_left = action_bar.mapTo(window, action_bar.rect().topLeft())
    bottom = action_bar.mapTo(window, action_bar.rect().bottomLeft()).y()

    # 操作栏内（窗口标题与按钮之间的空白）
    inside = QPoint(top_left.x() + action_bar.width() // 2,
                    top_left.y() + action_bar.height() // 2)
    assert resizer._in_title_bar(inside)

    # 操作栏下沿之下的工具栏，已不算标题栏
    assert not resizer._in_title_bar(QPoint(inside.x(), bottom + 20))


def test_installed_resizer_updates_cursor(window):
    """鼠标贴在左下角时给对角线缩放光标（不真的开始拖拽）。"""
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication

    local = QPointF(1, window.height() - 1)
    global_pos = QPointF(window.mapToGlobal(local.toPoint()))
    event = QMouseEvent(
        QMouseEvent.Type.MouseMove,
        local,
        global_pos,
        Qt.MouseButton.NoButton,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(window, event)
    QApplication.processEvents()
    assert window.window_resizer.last_direction != 0


# ==================== 方向判定 ====================

@pytest.mark.parametrize(
    ("x", "y", "expected"),
    [
        (100, 100, 0),
        (1, 100, 4),  # LEFT
        (199, 100, 8),  # RIGHT（w=200）
        (100, 1, 1),  # UP
        (100, 199, 2),  # DOWN（h=200）
        (1, 1, 5),  # TOP_LEFT
        (199, 199, 10),  # BOTTOM_RIGHT
    ],
)
def test_direction_from_mouse_pos(x, y, expected):
    from albuswall.ui.platform import Direction

    assert Direction.from_mouse_pos(x, y, 200, 200, 12) == expected


# ==================== 交互控件不被当成拖拽 ====================

def test_button_press_is_not_a_window_drag(window):
    """最大化按钮上的左键按下交给按钮自己，不触发拖窗口。"""
    button = window.title_bar.maximize_button
    center = button.rect().center()
    local = button.mapTo(window, center)
    _press(window, local.x(), local.y())
    assert window.window_resizer.is_dragging is False


def test_title_bar_blank_press_starts_drag(window):
    """操作栏空白处（窗口标题右侧）的左键按下 = 拖窗口。"""
    y = max(1, window.title_bar.action_bar.height() // 2)
    # 靠窗口左边缘的 x 会先命中缩放边距（8 < margin 12），取中间一点的空白
    x = 200
    _press(window, x, y)
    try:
        assert window.window_resizer.is_dragging is True
    finally:
        window.window_resizer.is_dragging = False
