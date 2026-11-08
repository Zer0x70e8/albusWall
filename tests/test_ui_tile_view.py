#
"""``TileView``（内容层的网格视图）：列数 → 格子尺寸、可见范围、光标。

内容层 ``Content`` 是它的子类，主内容网格的全部滚动 / 命中 / 键盘行为都由
这里保证。用例只依赖 offscreen 平台，不需要真实窗口。
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtCore import QSize, Qt  # noqa: E402
from PySide6.QtGui import QColor, QPixmap  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QStyle,
    QStyleOptionViewItem,
    QStyledItemDelegate,
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def pixmap(qapp):
    pixmap = QPixmap(64, 64)
    pixmap.fill(QColor("red"))
    return pixmap


def _model(count: int = 0, pixmap: QPixmap | None = None):
    from albuswall.ui.model import AssetGridModel

    model = AssetGridModel()
    for row in range(count):
        model.append_asset(f"a{row}", f"t{row}", pixmap)
    return model


def _view(count: int = 0, *, columns: int = 5, size=(500, 400), pixmap=None):
    """建一个显示出来的 ``TileView``。

    * 模型挂在视图上（``_test_model``），防止 Python 侧被回收；
    * 视图放进一个有固定尺寸的宿主里：``TileView`` 是顶层窗口时，
      ``QListView`` 会按内容 ``sizeHint`` 自己调尺寸，测不出真实布局。
    """
    from PySide6.QtWidgets import QWidget

    from albuswall.ui.widgets import TileView

    model = _model(count, pixmap)
    host = QWidget()
    host.resize(*size)
    view = TileView(host, column_count=columns)
    view.setGeometry(0, 0, *size)
    view.setModel(model)
    view._test_model = model
    view._test_host = host
    host.show()
    QApplication.processEvents()
    return view


# ==================== 列数 → 格子尺寸 ====================

def test_cell_size_is_derived_from_column_count(qapp):
    """列数是唯一真值：格子 = (可用宽度 - 1) // 列数，且始终是正方形。"""
    view = _view(20, columns=5, size=(500, 400))
    try:
        expected = max(1, (view._layout_width() - 1) // 5)
        assert view.cell_size == expected
        assert view.gridSize() == QSize(expected, expected)
        # (cell * columns) 严格小于可用宽度 —— 不会因为 1px 误差掉列
        assert view.cell_size * 5 < view._layout_width()
    finally:
        view.deleteLater()


def test_column_count_is_clamped(qapp):
    view = _view(20, columns=5)
    try:
        view.set_column_count(99)
        assert view.column_count == 12
        view.set_column_count(-3)
        assert view.column_count == 1
    finally:
        view.deleteLater()


def test_ctrl_wheel_changes_column_count(qapp):
    """``Ctrl`` + 滚轮 = 缩放（列数 ±1），普通滚轮不改列数。"""
    from PySide6.QtCore import QPoint, QPointF
    from PySide6.QtGui import QWheelEvent

    view = _view(40, columns=5)
    try:
        def wheel(dy: int, modifier):
            event = QWheelEvent(
                QPointF(10, 10), QPointF(10, 10),
                QPoint(0, 0), QPoint(0, dy),
                Qt.MouseButton.NoButton, modifier,
                Qt.ScrollPhase.NoScrollPhase, False,
            )
            QApplication.sendEvent(view.viewport(), event)

        wheel(120, Qt.KeyboardModifier.ControlModifier)
        assert view.column_count == 4
        wheel(-120, Qt.KeyboardModifier.ControlModifier)
        assert view.column_count == 5
        wheel(-120, Qt.KeyboardModifier.NoModifier)
        assert view.column_count == 5
    finally:
        view.deleteLater()


def test_cell_size_ignores_scrollbar_visibility(qapp):
    """滚动条出现 / 消失不许改变格子尺寸（否则会在临界尺寸上反复抖动）。"""
    short = _view(3, columns=5, size=(500, 400))
    long = _view(200, columns=5, size=(500, 400))
    try:
        assert short.verticalScrollBar().isVisible() is False
        assert long.verticalScrollBar().isVisible() is True
        assert short.cell_size == long.cell_size
    finally:
        short.deleteLater()
        long.deleteLater()


# ==================== delegate 尺寸强制 ====================

def test_delegate_size_hint_is_forced_to_cell_size(qapp):
    """委托写什么 ``sizeHint`` 都没用：视图一律强制成 ``cell_size``。"""
    from PySide6.QtWidgets import QListView

    class _Big(QStyledItemDelegate):
        def sizeHint(self, option, index):  # noqa: N802 - Qt 命名
            return QSize(1000, 17)

    view = _view(10, columns=4, size=(480, 400))
    try:
        big = _Big(view)
        view.setItemDelegate(big)
        # 内层仍是调用方塞的那份（``itemDelegate()`` 故意返回它）
        assert view.itemDelegate() is big
        option = QStyleOptionViewItem()
        index = view.model().index(0, 0)
        assert big.sizeHint(option, index) == QSize(1000, 17)

        # 外层是强制尺寸的包装：QListView 自己看到的就是 cell_size
        wrapper = QListView.itemDelegate(view)
        assert wrapper is not big
        assert wrapper.sizeHint(option, index) == QSize(
            view.cell_size, view.cell_size)
    finally:
        view.deleteLater()


# ==================== 可见范围 ====================

def test_visible_range_covers_whole_rows(qapp):
    from PySide6.QtTest import QTest

    view = _view(100, columns=5, size=(500, 400))
    ranges: list[tuple[int, int]] = []
    view.visible_range_changed.connect(
        lambda start, end, seq: ranges.append((start, end)))
    try:
        view.refresh_visible_range()
        assert ranges
        start, end = ranges[-1]
        assert start % 5 == 0            # 从整行开始
        assert end % 5 == 4 or end == 99  # 到整行结束（或数据末尾）

        ranges.clear()
        QTest.keyClick(view, Qt.Key.Key_End)  # 跳到最后一项 → 滚到底
        QApplication.processEvents()
        view.refresh_visible_range()
        assert ranges
        new_start, new_end = ranges[-1]
        assert new_start > start       # 可见范围整体下移
        assert new_end == 99           # 末行完整覆盖到数据末尾
    finally:
        view.deleteLater()


# ==================== 光标 ====================

def test_cursor_moves_by_row_and_clamps(qapp):
    from PySide6.QtTest import QTest

    view = _view(30, columns=5)
    try:
        view.setFocus()
        view.set_cursor_index(0)
        assert view.cursor_index() == 0

        QTest.keyClick(view, Qt.Key.Key_Down)
        assert view.cursor_index() == 5      # 下一行
        QTest.keyClick(view, Qt.Key.Key_Right)
        assert view.cursor_index() == 6
        QTest.keyClick(view, Qt.Key.Key_Left)
        QTest.keyClick(view, Qt.Key.Key_Up)
        assert view.cursor_index() == 0

        view.set_cursor_index(999)
        assert view.cursor_index() == 29     # 收敛到末项
        view.set_cursor_index(-5)
        assert view.cursor_index() == 0
    finally:
        view.deleteLater()


def test_cursor_survives_shrinking_model(qapp):
    """模型换源：光标收敛到合法行；模型清空：光标变 -1。"""
    view = _view(10, columns=5)
    try:
        view.set_cursor_index(9)
        assert view.cursor_index() == 9

        view.model().clear()
        QApplication.processEvents()
        assert view.cursor_index() == -1

        view.model().append_asset("x", "x")
        QApplication.processEvents()
        assert view.cursor_index() == 0
    finally:
        view.deleteLater()


# ==================== 滚动 ====================

def test_scroll_y_and_scroll_row_round_trip(qapp):
    view = _view(200, columns=5, size=(500, 400))
    bar = view.verticalScrollBar()
    try:
        view.set_scroll_y(0)
        assert view.scroll_y == 0
        view.set_scroll_row(3)
        assert view.scroll_row == 3
        assert view.scroll_y == 3 * view.cell_size
        assert view.overscroll == 0
        # 越界请求被「过滚上限」夹紧，不会抛
        view.set_scroll_y(10 ** 9)
        assert view.scroll_y == bar.maximum() + view.bottom_overscroll
        assert view.overscroll == view.bottom_overscroll
        view.set_scroll_y(-10 ** 9)
        assert view.scroll_y == -view.overscroll_top
        assert view.overscroll == -view.overscroll_top
    finally:
        view.deleteLater()


# ==================== 过滚 ====================

def _wheel(
        view, dy: int, modifier=Qt.KeyboardModifier.NoModifier, pos=None,
        *, pixels: bool = False,
):
    """给 viewport 投一个滚轮事件，返回它是否被消费。

    ``pixels=True`` 时把 ``dy`` 当作触控板的 ``pixelDelta``（高分滚动）。
    """
    from PySide6.QtCore import QPoint, QPointF
    from PySide6.QtGui import QWheelEvent

    point = QPointF(QPoint(10, 10) if pos is None else pos)
    angle = QPoint(0, 0) if pixels else QPoint(0, dy)
    pixel_delta = QPoint(0, dy) if pixels else QPoint(0, 0)
    event = QWheelEvent(
        point, point,
        pixel_delta, angle,
        Qt.MouseButton.NoButton, modifier,
        Qt.ScrollPhase.NoScrollPhase, False,
    )
    QApplication.sendEvent(view.viewport(), event)
    return event.isAccepted()


def test_wheel_past_top_overscrolls_without_bounce(qapp):
    """顶部继续上滚：内容被推开（最多 128px），停在那里不回弹。"""
    view = _view(200, columns=5, size=(500, 400))
    try:
        view.set_scroll_y(0)
        assert view.scroll_y == 0

        assert _wheel(view, 120) is True
        assert view.scroll_y == -30          # 一格 30px
        assert view.overscroll == -30
        assert view.verticalScrollBar().value() == 0
        # 内容整体下移（viewport 被挪开），没有回弹计时器在跑
        assert view.viewport().y() == 30

        # 连滚到底：夹在 -overscroll_top，且事件让给父层
        for _ in range(20):
            _wheel(view, 120)
        assert view.scroll_y == -view.overscroll_top
        assert view.viewport().y() == view.overscroll_top
        assert _wheel(view, 120) is False    # 已到极限 → 传下去

        # 往回滚先收回过滚，再动滚动条（不会跳）
        _wheel(view, -120)
        assert view.scroll_y == -view.overscroll_top + 30
        view.set_scroll_y(-60)
        _wheel(view, -120)                   # -60 → -30
        assert view.scroll_y == -30
        assert view.verticalScrollBar().value() == 0
        _wheel(view, -120)                   # -30 → 0（回到内容顶端）
        assert view.scroll_y == 0
        assert view.overscroll == 0
        assert view.viewport().y() == 0
        _wheel(view, -120)                   # 之后才开始滚滚动条
        assert view.scroll_y == 30
        assert view.overscroll == 0
    finally:
        view.deleteLater()


def test_wheel_past_bottom_overscrolls(qapp):
    """底部继续下滚：最多越过底部 256px。"""
    view = _view(200, columns=5, size=(500, 400))
    bar = view.verticalScrollBar()
    try:
        bar.setValue(bar.maximum())
        view.reset_overscroll()
        assert view.overscroll == 0

        assert _wheel(view, -120) is True
        assert view.scroll_y == bar.maximum() + 30
        assert view.overscroll == 30
        assert view.viewport().y() == -30    # 内容被顶上去

        for _ in range(20):
            _wheel(view, -120)
        assert view.scroll_y == bar.maximum() + view.bottom_overscroll
        assert bar.value() == bar.maximum()
        assert _wheel(view, -120) is False
    finally:
        view.deleteLater()


def test_normal_scroll_still_moves_the_bar(qapp):
    view = _view(200, columns=5, size=(500, 400))
    try:
        view.set_scroll_y(0)
        _wheel(view, -120)
        assert view.overscroll == 0
        assert view.scroll_y == 30
    finally:
        view.deleteLater()


def test_selection_does_not_bounce_overscroll(qapp):
    """选中 / 挪光标到**视野内**的格子：过滚不许回弹。"""
    from PySide6.QtTest import QTest

    view = _view(200, columns=5, size=(500, 400))
    try:
        view.setFocus()
        view.set_cursor_index(0)
        view.set_scroll_y(-100)
        assert view.overscroll == -100

        # 光标换到同一屏里的第 2 格：位置一点不动
        view.set_cursor_index(2)
        assert view.overscroll == -100
        assert view.viewport().y() == 100

        QTest.keyClick(view, Qt.Key.Key_Right)
        assert view.cursor_index() == 3
        assert view.overscroll == -100

        # 跳到很远的格子：真的滚了，过滚随之失效（位置连续，不打架）
        view.set_cursor_index(150)
        assert view.overscroll == 0
        assert view.scroll_y > 0
    finally:
        view.deleteLater()


def test_model_reset_keeps_scroll_and_overscroll(qapp):
    """相册刷新 / 过滤（模型 reset）不许把位置拉回顶部。"""
    view = _view(200, columns=5, size=(500, 400))
    model = view.model()
    try:
        view.set_scroll_y(500)
        view.resize(view.width(), view.height())  # 触发一次几何重算
        QApplication.processEvents()
        assert view.scroll_y == 500

        model.set_entries(
            [__import__("albuswall.ui.model", fromlist=["AssetEntry"]).AssetEntry(
                uuid=f"b{i}", title=f"b{i}") for i in range(200)])
        QApplication.processEvents()
        assert view.scroll_y == 500, "刷新后回到了别的位置"
        assert view.cursor_index() == 0

        # 过滚态下刷新：偏移也保住
        view.set_scroll_y(-100)
        model.set_entries(
            [__import__("albuswall.ui.model", fromlist=["AssetEntry"]).AssetEntry(
                uuid=f"c{i}", title=f"c{i}") for i in range(200)])
        QApplication.processEvents()
        assert view.overscroll == -100
        assert view.viewport().y() == 100
    finally:
        view.deleteLater()


def test_zoom_keeps_the_top_left_cell_in_place(qapp):
    """缩放（改列数）锚定左上角那一格：同一张图、同样的顶端偏移，不回弹。"""
    from PySide6.QtCore import QPoint

    view = _view(200, columns=5, size=(500, 400))
    model = view.model()
    try:
        cell = view.cell_size
        view.set_scroll_y(7 * cell + 40)      # 第 7 行，行内偏移 40px
        start_scroll = view.scroll_y

        # 手势锚点 = 左上角那一格（索引 + 它在视口里的 y）
        anchor = view.indexAt(QPoint(0, 0)).row()
        anchor_y = view.visualRect(model.index(anchor, 0)).y()

        for cols in (6, 7, 8, 7, 6, 5):
            view.set_column_count(cols)
            moved = view.visualRect(model.index(anchor, 0)).y()
            assert abs(moved - anchor_y) <= 1, (
                f"{cols} 列：锚点格子位移 {moved - anchor_y}px")

        # 来回缩放回到原位（同一格、同样的顶端偏移 → 位置一分不差）
        assert abs(view.scroll_y - start_scroll) <= 1
    finally:
        view.deleteLater()


def test_ctrl_wheel_zoom_anchors_at_pointer(qapp):
    """``Ctrl`` + 滚轮：缩放围着鼠标那一格，位置不跳。"""
    from PySide6.QtCore import QPoint

    view = _view(200, columns=5, size=(500, 400))
    model = view.model()
    try:
        view.set_scroll_y(6 * view.cell_size + 30)
        point = QPoint(60, 120)
        anchor = view.indexAt(point).row()
        anchor_y = view.visualRect(model.index(anchor, 0)).y()
        start_scroll = view.scroll_y

        assert _wheel(
            view, 120, Qt.KeyboardModifier.ControlModifier, point) is True
        assert view.column_count == 4
        assert abs(
            view.visualRect(model.index(anchor, 0)).y() - anchor_y) <= 1
        assert view.scroll_y != 0

        # 缩放回来：回到原来的位置（同一格锚定 → 不漂）
        assert _wheel(
            view, -120, Qt.KeyboardModifier.ControlModifier, point) is True
        assert view.column_count == 5
        assert abs(view.scroll_y - start_scroll) <= 1
    finally:
        view.deleteLater()


def test_zoom_keeps_overscroll(qapp):
    """过滚态下缩放：边缘那点位移不被顺手收掉。"""
    from PySide6.QtCore import QPoint

    view = _view(200, columns=5, size=(500, 400))
    try:
        view.set_scroll_y(-view.overscroll_top)
        assert view.overscroll == -view.overscroll_top
        view.set_column_count(6)
        assert view.overscroll == -view.overscroll_top
        assert view.viewport().y() == view.overscroll_top

        # Ctrl + 滚轮（带指针位置）也一样：过滚不许被缩放吃掉
        view._clear_zoom_anchor()
        assert _wheel(
            view, 120, Qt.KeyboardModifier.ControlModifier,
            QPoint(100, 120)) is True
        assert view.column_count == 5          # 放大 = 列数 -1
        assert view.overscroll == -view.overscroll_top
        assert view.viewport().y() == view.overscroll_top
    finally:
        view.deleteLater()


def test_external_scrollbar_change_cancels_overscroll(qapp):
    """拖滑块 / 键盘翻页改了滚动条：过滚立刻失效（位置不能拼出断层）。"""
    view = _view(200, columns=5, size=(500, 400))
    bar = view.verticalScrollBar()
    try:
        view.set_scroll_y(-view.overscroll_top)
        assert view.overscroll == -view.overscroll_top

        bar.setValue(300)                # 外部改动 = 用户拖滑块
        assert view.overscroll == 0
        assert view.scroll_y == 300
        assert view.viewport().y() == 0
    finally:
        view.deleteLater()


def test_zoom_debounce_accumulates_touchpad_steps(qapp):
    """缩放防抖：触控板的小碎步攒够一档（120）才动一列。"""
    view = _view(200, columns=5, size=(500, 400))
    try:
        # 7 × 15 = 105 < 120：一列都不动
        for _ in range(7):
            assert _wheel(view, 15, Qt.KeyboardModifier.ControlModifier) is True
            assert view.column_count == 5
        # 第 8 次攒满 120 → 缩一档（放大 = 列数 -1）
        _wheel(view, 15, Qt.KeyboardModifier.ControlModifier)
        assert view.column_count == 4
        # 余量清零：再来 7 次小碎步仍然不动
        for _ in range(7):
            _wheel(view, 15, Qt.KeyboardModifier.ControlModifier)
        assert view.column_count == 4
        _wheel(view, 15, Qt.KeyboardModifier.ControlModifier)
        assert view.column_count == 3
    finally:
        view.deleteLater()


def test_zoom_debounce_pixel_delta_trackpad(qapp):
    """触控板的 ``pixelDelta``：30px ≈ 一格滚轮，攒满才缩一档。"""
    view = _view(200, columns=5, size=(500, 400))
    try:
        # 5px × 4 = 20 滚轮量/次 → 5 次 = 100（不到 120），一列都不动
        for _ in range(5):
            _wheel(view, 5, Qt.KeyboardModifier.ControlModifier, pixels=True)
            assert view.column_count == 5
        _wheel(view, 5, Qt.KeyboardModifier.ControlModifier, pixels=True)
        assert view.column_count == 4          # 第 6 次攒满 120
    finally:
        view.deleteLater()


def test_zoom_debounce_reverse_direction_resets_residue(qapp):
    """反向往回滚：先清掉上个方向的余量，不会「卡住」。"""
    view = _view(200, columns=5, size=(500, 400))
    try:
        for _ in range(7):                      # +105 攒着
            _wheel(view, 15, Qt.KeyboardModifier.ControlModifier)
        assert view.column_count == 5

        _wheel(view, -15, Qt.KeyboardModifier.ControlModifier)   # 反向：清 +105，从 -15 起算
        for _ in range(6):                      # -90 → 共 -105，还差一点
            _wheel(view, -15, Qt.KeyboardModifier.ControlModifier)
        assert view.column_count == 5
        _wheel(view, -15, Qt.KeyboardModifier.ControlModifier)   # -120 → 缩出一档
        assert view.column_count == 6
    finally:
        view.deleteLater()


def test_zoom_debounce_does_not_bank_at_limit(qapp):
    """到列数极限后余量清零：反向时不会一次跳好几档。"""
    view = _view(200, columns=12, size=(500, 400))
    try:
        for _ in range(8):                      # 到顶 +120
            _wheel(view, -15, Qt.KeyboardModifier.ControlModifier)
        assert view.column_count == 12          # 已经最大，不动
        _wheel(view, 120, Qt.KeyboardModifier.ControlModifier)
        assert view.column_count == 11          # 没有攒着的余量，只缩一档
    finally:
        view.deleteLater()


def test_normal_wheel_ends_zoom_gesture(qapp):
    """普通滚轮 = 缩放手势结束：攒着的余量作废。"""
    view = _view(200, columns=5, size=(500, 400))
    try:
        for _ in range(7):                      # +105 攒着
            _wheel(view, 15, Qt.KeyboardModifier.ControlModifier)
        _wheel(view, -120)                      # 普通滚轮
        _wheel(view, 15, Qt.KeyboardModifier.ControlModifier)
        assert view.column_count == 5           # 余量已清，不会凑满一档
    finally:
        view.deleteLater()


def test_overscroll_survives_zoom_and_later_layout(qapp):
    """过滚态缩放：Qt 在后续布局里把 viewport 摆回原位后要补回来。"""
    from PySide6.QtCore import QPoint

    view = _view(200, columns=5, size=(500, 400))
    try:
        view.set_scroll_y(-60)
        assert view.overscroll == -60
        view.set_column_count(6)
        # 连跑几轮事件循环：QListView 的延迟布局会把 viewport 摆回 (0,0)
        for _ in range(5):
            QApplication.processEvents()
        assert view.overscroll == -60
        assert view.viewport().y() == 60
        _wheel(view, -120, pos=QPoint(10, 10))   # 往下滚 → 吃掉过滚
        assert view.overscroll == -30
        assert view.viewport().y() == 30
    finally:
        view.deleteLater()


def test_overscroll_emits_total_scroll_position(qapp):
    """``scroll_changed`` / ``scroll_y`` 的口径是「含过滚的总位置」。"""
    view = _view(200, columns=5, size=(500, 400))
    seen: list[int] = []
    view.scroll_changed.connect(seen.append)
    try:
        view.set_scroll_y(0)
        seen.clear()
        _wheel(view, 120)          # 进入顶部过滚（滚动条值不动）
        assert seen == [-30]
    finally:
        view.deleteLater()


# ==================== 委托绘制：焦点描边 ====================

def test_focused_cell_draws_highlight_ring(qapp, pixmap):
    """当前格（视图有焦点）画一圈蓝色描边 —— 键盘光标的可见性在这里。"""
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QPainter

    from albuswall.ui.delegate import InfoCellDelegate, InfoMode

    canvas = QPixmap(48, 48)
    canvas.fill(QColor(0, 0, 0))

    delegate = InfoCellDelegate(mode=InfoMode.NONE)
    option = QStyleOptionViewItem()
    option.rect = QRect(0, 0, 48, 48)
    option.state = QStyle.StateFlag.State_HasFocus

    model = _model(1)  # 没有缩略图：画面上只有背景 + 焦点描边
    painter = QPainter(canvas)
    try:
        delegate.paint(painter, option, model.index(0, 0))
    finally:
        painter.end()

    image = canvas.toImage()
    edge = [image.pixelColor(x, 24) for x in range(0, 4)]
    assert any(
        c.blue() > c.red() + 25 and c.blue() > c.green() + 15 for c in edge
    ), "焦点格没有画出高亮描边"
