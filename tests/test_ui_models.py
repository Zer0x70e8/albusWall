#
"""UI 展示模型 + 委托契约：模型产出 role，委托按 role 取数据。

这里只测**纯逻辑**（不需要真实窗口）：role 是否对得上、装载 / 增量修改是否
发对信号、鸭子类型入参（dataclass / dict / DTO）是否被正确归一化。
Qt 需要 offscreen 平台才能在无显示环境跑；必须在 import PySide6 之前设好。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QColor, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def pixmap(qapp):
    pixmap = QPixmap(32, 16)
    pixmap.fill(QColor("red"))
    return pixmap


# ==================== 相册 / 资产模型 ====================

def test_album_model_exposes_item_roles(pixmap):
    from albuswall.ui.model import AlbumModel, ItemRole

    model = AlbumModel()
    model.append_album("a1", "相册一", "描述", pixmap, asset_count=7)

    index = model.index(0, 0)
    assert model.data(index, ItemRole.Uuid) == "a1"
    assert model.data(index, ItemRole.Title) == "相册一"
    assert model.data(index, ItemRole.Description) == "描述"
    assert model.data(index, ItemRole.AssetCount) == 7
    # 装饰与显示 role 也要对上，否则默认委托画不出来
    assert model.data(index, ItemRole.Pixmap) is pixmap
    assert model.data(index, Qt.ItemDataRole.DecorationRole) is pixmap
    assert model.data(index, Qt.ItemDataRole.DisplayRole) == "相册一"


def test_album_model_accepts_plain_objects():
    from albuswall.ui.model import AlbumModel, ItemRole

    @dataclass
    class AlbumLike:
        uuid: str
        title: str
        description: str = ""

    model = AlbumModel()
    model.set_albums([AlbumLike("x", "标题"), {"uuid": "y", "title": "字典"}])

    assert model.rowCount() == 2
    assert model.data(model.index(0, 0), ItemRole.Uuid) == "x"
    assert model.data(model.index(1, 0), ItemRole.Title) == "字典"
    assert model.row_of("y") == 1


def test_album_model_carries_row_icon():
    """``ItemRole.Icon`` 是回收站单行列表的行首图标（当前是 emoji 文本）。

    回归背景：``TrashAlbumDelegate`` 只从 role 取数据，模型不给这一格，
    底部那行就只剩名称、左边空着。
    """
    from albuswall.ui.model import AlbumEntry, AlbumModel, ItemRole

    model = AlbumModel()
    model.set_albums([AlbumEntry(uuid="t", title="Trash")], icon="🗑️")

    index = model.index(0, 0)
    assert model.data(index, ItemRole.Icon) == "🗑️"
    assert model.icon_at(0) == "🗑️"
    assert model.data(index, ItemRole.Title) == "Trash"
    # 顶部网格的条目没有图标概念，缺省是空串而不是 None
    model.set_albums([AlbumEntry(uuid="a", title="A")])
    assert model.data(model.index(0, 0), ItemRole.Icon) == ""

    # 已经是 AlbumEntry 的条目在被统一覆盖时不能被原地改坏
    entry = AlbumEntry(uuid="t", title="Trash")
    built = AlbumModel.to_entries([entry], icon="🗑️")
    assert built[0].icon == "🗑️"
    assert entry.icon == ""


def test_album_entry_from_object_reads_icon_attribute():
    from albuswall.ui.model import AlbumEntry

    @dataclass
    class AlbumLike:
        uuid: str
        title: str
        icon: str = "📁"

    assert AlbumEntry.from_object(AlbumLike("x", "标题")).icon == "📁"
    assert AlbumEntry.from_object(AlbumLike("x", "标题"), icon="🗑️").icon == "🗑️"


def test_album_model_set_pixmap_only_touches_that_row(pixmap):
    from albuswall.ui.model import AlbumModel, ItemRole

    model = AlbumModel()
    model.append_album("a1", "一")
    model.append_album("a2", "二")

    changed: list[int] = []
    model.dataChanged.connect(
        lambda top_left, *_rest: changed.append(top_left.row()))

    model.set_pixmap(1, pixmap)

    assert changed == [1]
    assert model.pixmap_at(1) is pixmap
    assert model.pixmap_at(0) is None
    assert model.data(model.index(1, 0), ItemRole.Pixmap) is pixmap


def test_asset_model_keeps_unknown_state_distinct_from_false(pixmap):
    from albuswall.ui.model import AssetGridModel, ItemRole

    model = AssetGridModel()
    model.append_asset("a1", "图一", pixmap, favorite=None, days_left=None)
    model.append_asset("a2", "图二", favorite=False, days_left=3)

    # None = 未知，委托据此不画角标；不能悄悄变成 False
    assert model.data(model.index(0, 0), ItemRole.Favorite) is None
    assert model.data(model.index(0, 0), ItemRole.DaysLeft) is None
    assert model.data(model.index(1, 0), ItemRole.Favorite) is False
    assert model.data(model.index(1, 0), ItemRole.DaysLeft) == 3
    assert model.uuids() == ["a1", "a2"]


def test_asset_model_reads_dto_style_attribute_names():
    from albuswall.ui.model import AssetGridModel, ItemRole

    @dataclass
    class AssetLike:
        uuid: str
        file_name: str
        is_favorite: bool
        is_deleted: bool
        days_remaining: int | None = None

    model = AssetGridModel()
    model.set_assets([AssetLike("a1", "x.jpg", True, False, 2)])

    index = model.index(0, 0)
    assert model.data(index, ItemRole.Title) == "x.jpg"
    assert model.data(index, ItemRole.Favorite) is True
    assert model.data(index, ItemRole.Deleted) is False
    assert model.data(index, ItemRole.DaysLeft) == 2


# ==================== 信息面板模型 ====================

def test_metadata_model_formats_and_orders_fields():
    from albuswall.ui.model import MetadataModel, MetadataRole

    model = MetadataModel({"width": 1920, "file_size": 1536, "title": "x.jpg"})

    keys = [model.data(model.index(r, 0), MetadataRole.Key)
            for r in range(model.rowCount())]
    values = [model.data(model.index(r, 0), MetadataRole.Value)
              for r in range(model.rowCount())]

    # 顺序由字段规格决定，不是 dict 顺序
    assert keys == ["标题", "宽度", "文件大小"]
    assert values == ["x.jpg", "1920 px", "1.5 KB"]
    # 原始值要留给外部做排序 / 复制
    assert model.data(model.index(1, 0), MetadataRole.RawValue) == 1920


def test_metadata_model_accepts_dataclass_and_skips_blank():
    from albuswall.ui.model import MetadataModel, MetadataRole

    @dataclass
    class AssetLike:
        title: str = "a.jpg"
        description: str = ""
        is_favorite: bool = True

    model = MetadataModel(AssetLike())
    labels = [model.data(model.index(r, 0), MetadataRole.Key)
              for r in range(model.rowCount())]

    assert labels == ["标题", "已收藏"]
    # 布尔值文案的源文是英文（``self.tr("Yes")``），测试环境不装翻译器
    assert model.data(model.index(1, 0), MetadataRole.Value) in ("Yes", "True")


def test_metadata_model_clear_and_retranslate_are_safe():
    from albuswall.ui.model import MetadataModel

    model = MetadataModel({"title": "x"})
    model.retranslate()  # 没有数据时也不能炸
    model.clear()
    assert model.rowCount() == 0
    model.retranslate()
    assert model.rowCount() == 0


# ==================== 导航条模型 ====================

def test_navigation_model_lazy_provider_is_asked_once(pixmap):
    from albuswall.ui.model import NavigationModel
    from albuswall.ui.model.roles import NavigationRole

    asked: list[str] = []

    def provider(nav_id: str):
        asked.append(nav_id)
        return pixmap

    model = NavigationModel(lazy_provider=provider)
    model.set_items([("n1", None)])

    index = model.index(0, 0)
    assert model.data(index, NavigationRole.Pixmap) is pixmap
    # 第二次取已缓存，不再问 provider
    assert model.data(index, NavigationRole.Pixmap) is pixmap
    assert asked == ["n1"]

    model.retry_lazy()
    model.set_pixmap(0, None)
    model.data(index, NavigationRole.Pixmap)
    assert asked == ["n1", "n1"]


def test_navigation_model_rows_and_lookup(pixmap):
    from albuswall.ui.model import NavigationModel

    model = NavigationModel()
    model.set_items([("n1", pixmap), ("n2", None)])

    assert model.rowCount() == 2
    assert model.row_of("n2") == 1
    assert model.row_of("nope") == -1
    assert model.id_at(0) == "n1"


# ==================== 导入源卡片模型 ====================

def test_source_card_model_upsert_aligns_by_uuid():
    from albuswall.ui.model import SourceCard, SourceCardModel
    from albuswall.ui.model.roles import SourceCardRole

    model = SourceCardModel()
    row = model.upsert(SourceCard(uuid="s1", title="旧"))
    assert row == 0

    changed: list[int] = []
    model.dataChanged.connect(
        lambda top_left, *_rest: changed.append(top_left.row()))

    # 同 uuid → 更新而不是追加
    assert model.upsert(SourceCard(uuid="s1", title="新")) == 0
    assert model.rowCount() == 1
    assert changed == [0]
    assert model.data(model.index(0, 0), SourceCardRole.Title) == "新"
    assert model.remove_by_uuid("s1") is True
    assert model.rowCount() == 0


# ==================== 标签模型 ====================

class _Tag:
    def __init__(self, uuid: str, name: str, children=()):
        self.uuid = uuid
        self.name = name
        self.description = ""
        self.children = list(children)


class _Node:
    def __init__(self, tag, children=()):
        self.tag = tag
        self.children = list(children)


def test_tag_chip_model_dedupes_by_uuid():
    from albuswall.ui.model import TagChipModel
    from albuswall.ui.model.roles import TagRole

    root = _Tag("t1", "根标签")
    model = TagChipModel()
    model.set_tags([root, root, _Tag("t2", "子标签")])

    assert model.rowCount() == 2
    assert model.data(model.index(0, 0), Qt.ItemDataRole.DisplayRole) == "根标签"
    assert model.data(model.index(0, 0), TagRole.Tag) is root
    assert model.index_of("t2").row() == 1
    assert model.remove_at(1) is not None
    assert model.rowCount() == 1


def test_tag_tree_model_walks_domain_nodes():
    from albuswall.ui.model import TagTreeModel
    from albuswall.ui.model.roles import TagRole

    root = _Tag("t1", "根", [_Tag("t1-1", "子")])
    model = TagTreeModel()
    model.set_roots([_Node(root, [_Node(root.children[0])])])

    assert model.rowCount() == 1
    child = model.index_of("t1-1")
    assert child.isValid()
    assert model.uuid_at(child) == "t1-1"
    assert model.path_of("t1-1") == ["t1", "t1-1"]
    assert model.data(child, Qt.ItemDataRole.DisplayRole) == "子"
    assert model.data(child, TagRole.Tag) is root.children[0]


def test_tag_tree_model_accepts_homemade_nodes():
    from albuswall.ui.model import TagNode, TagTreeModel

    model = TagTreeModel([TagNode("r", "R", children=[TagNode("c", "C")])])
    assert model.index_of("c").isValid()
    assert model.path_of("c") == ["r", "c"]


# ==================== 委托 ====================

def test_album_cover_delegate_reads_title_and_pixmap(qapp, pixmap):
    from PySide6.QtGui import QPainter
    from PySide6.QtWidgets import QStyleOptionViewItem

    from albuswall.ui.delegate import AlbumCoverDelegate
    from albuswall.ui.model import AlbumModel

    model = AlbumModel()
    model.append_album("a1", "相册", "描述", pixmap, asset_count=2)

    option = QStyleOptionViewItem()
    option.rect = pixmap.rect()
    option.font = qapp.font()

    canvas = QPixmap(pixmap.size())
    painter = QPainter(canvas)
    try:
        # 不抛异常即可：绘制细节由截图用例（人工）覆盖
        AlbumCoverDelegate().paint(painter, option, model.index(0, 0))
    finally:
        painter.end()


def test_asset_thumb_delegate_handles_rows_without_pixmap(qapp):
    from PySide6.QtGui import QPainter
    from PySide6.QtWidgets import QStyleOptionViewItem

    from albuswall.ui.delegate import AssetThumbDelegate
    from albuswall.ui.model import AssetGridModel

    model = AssetGridModel()
    model.append_asset("a1", "没有缩略图的格子", favorite=True, days_left=1)

    option = QStyleOptionViewItem()
    option.rect = QPixmap(40, 40).rect()
    option.font = qapp.font()

    canvas = QPixmap(40, 40)
    painter = QPainter(canvas)
    try:
        AssetThumbDelegate().paint(painter, option, model.index(0, 0))
    finally:
        painter.end()


def test_album_cover_delegate_draws_selection_as_outline(qapp, pixmap):
    """选中态画成贴着封面的描边，而不是铺满整格的半透明蓝底。

    回归背景：相册格子没有文字行，样式表的 item 高亮仍会铺满整格，
    于是封面下方多出一条蓝边（「超出 item 的蓝条」）。现在底色被压掉，
    选中视觉是封面内沿的一圈圆角描边。
    """
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QPainter
    from PySide6.QtWidgets import QStyle, QStyleOptionViewItem

    from albuswall.ui.delegate import AlbumCoverDelegate
    from albuswall.ui.model import AlbumModel

    model = AlbumModel()
    model.append_album("a1", "相册", "", pixmap)

    delegate = AlbumCoverDelegate(padding=4, radius=8)
    assert delegate.selection_background_visible() is False

    option = QStyleOptionViewItem()
    option.rect = QRect(0, 0, 60, 60)
    option.font = qapp.font()
    option.state |= QStyle.StateFlag.State_Selected

    canvas = QPixmap(60, 60)
    canvas.fill(QColor("black"))
    painter = QPainter(canvas)
    try:
        delegate.paint(painter, option, model.index(0, 0))
    finally:
        painter.end()

    image = canvas.toImage()

    def is_blue(x: int, y: int) -> bool:
        color = image.pixelColor(x, y)
        return (
            color.blue() > color.red() + 25
            and color.blue() > color.green() + 15
        )

    # 描边落在封面（rect 去掉 padding=4）的内沿
    assert is_blue(5, 30), "左侧描边缺失"
    assert is_blue(30, 5), "顶部描边缺失"
    # 封面内部没有被蓝色覆盖
    assert not is_blue(30, 30), "封面被蓝底盖住了"
    # 格子底部（旧实现里溢出蓝边的位置）不再有蓝色
    assert not is_blue(30, 58), "格子底部仍有多余蓝边"


def _view_option(rect):
    """构造一份交给 ``QStyledItemDelegate.paint`` 的选项（只关心 rect）。"""
    from PySide6.QtWidgets import QStyleOptionViewItem

    option = QStyleOptionViewItem()
    option.rect = rect
    return option


def test_info_cell_delegate_from_model_reads_item_roles(qapp, pixmap):
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QPainter

    from albuswall.ui.delegate import InfoCellDelegate, InfoMode
    from albuswall.ui.model import AssetGridModel

    model = AssetGridModel()
    model.append_asset("a1", "图", pixmap, favorite=True, days_left=3)

    delegate = InfoCellDelegate.from_model(model, mode=InfoMode.FAVORITE)
    canvas = QPixmap(48, 48)
    painter = QPainter(canvas)
    try:
        delegate.paint(
            painter, _view_option(QRect(0, 0, 48, 48)), model.index(0, 0))
    finally:
        painter.end()


def test_info_cell_delegate_paints_roles_straight_from_index(qapp, pixmap):
    """不注入 getter 也能画：``paint`` 拿到的 ``QModelIndex`` 本身就能读 role。"""
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QPainter

    from albuswall.ui.delegate import InfoCellDelegate, InfoMode
    from albuswall.ui.model import AssetGridModel

    model = AssetGridModel()
    model.append_asset("a1", "图", pixmap, favorite=True, days_left=None)

    delegate = InfoCellDelegate(mode=InfoMode.AUTO)
    assert delegate.mode_for(model.index(0, 0)) is InfoMode.FAVORITE

    canvas = QPixmap(48, 48)
    painter = QPainter(canvas)
    try:
        delegate.paint(
            painter, _view_option(QRect(0, 0, 48, 48)), model.index(0, 0))
    finally:
        painter.end()


def test_info_cell_delegate_auto_mode_shows_days_before_favorite(qapp, pixmap):
    """``InfoMode.AUTO``：回收站格显示剩余天数，其余格显示收藏状态。

    回归背景：``InfoMode.FAVORITE`` / ``DAYS`` 是整块互斥的，内容网格里既
    有普通资产又有（回收站视图里的）已删资产，必须逐格判断。
    """
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QPainter

    from albuswall.ui.delegate import InfoCellDelegate, InfoMode
    from albuswall.ui.model import AssetGridModel

    model = AssetGridModel()
    model.append_asset("fav", "收藏", pixmap, favorite=True, days_left=None)
    model.append_asset("trash", "回收站", pixmap, favorite=True, days_left=3)
    model.append_asset("plain", "普通", pixmap, favorite=False, days_left=None)

    delegate = InfoCellDelegate.from_model(model, mode=InfoMode.AUTO)

    assert delegate.mode is InfoMode.AUTO
    assert delegate.mode_for(0) is InfoMode.FAVORITE
    # 天数优先：回收站里的格子即使也是收藏的，也只显示剩余天数
    assert delegate.mode_for(1) is InfoMode.DAYS
    assert delegate.mode_for(2) is InfoMode.NONE

    canvas = QPixmap(48, 48)
    painter = QPainter(canvas)
    try:
        for row in range(3):
            delegate.paint(
                painter, _view_option(QRect(0, 0, 48, 48)),
                model.index(row, 0))
    finally:
        painter.end()


def test_info_cell_delegate_days_text_uses_english_source(qapp):
    """倒计时文案的源文是英文（中文由 ``InfoCellDelegate`` 上下文翻译）。"""
    from albuswall.ui.delegate import InfoCellDelegate, InfoMode
    from albuswall.ui.model import AssetGridModel

    model = AssetGridModel()
    model.append_asset("a1", "图", days_left=3)

    delegate = InfoCellDelegate.from_model(model, mode=InfoMode.AUTO)
    assert delegate.format_days(3) == "3 days left"
    assert delegate.format_days(0) == "Deleted today"
    assert delegate.format_days(-1) == "Expired"


def test_trash_album_delegate_paints_icon_left_and_title_right(qapp):
    """回收站单行：左图标 / 中间弹簧（留白）/ 右名称。

    回归背景：底部视图原先复用正方形的封面委托，一行 trash 会占掉一个
    96px 的格子。现在换成单行委托：行高由 ``sizeHint`` 决定，图标贴左、
    名称贴右，中间的空白就是「弹簧」。
    """
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QPainter
    from PySide6.QtWidgets import QStyle, QStyleOptionViewItem

    from albuswall.ui.delegate import TrashAlbumDelegate
    from albuswall.ui.model import AlbumEntry, AlbumModel

    model = AlbumModel()
    model.set_albums([AlbumEntry(uuid="t", title="Trash")], icon="T")

    width, height = 260, 40
    delegate = TrashAlbumDelegate(row_height=height, padding=8, gap=12)
    assert delegate.sizeHint(QStyleOptionViewItem(), model.index(0, 0)).height() == height

    option = QStyleOptionViewItem()
    option.rect = QRect(0, 0, width, height)
    option.font = qapp.font()

    canvas = QPixmap(width, height)
    canvas.fill(QColor(0, 0, 0, 0))
    painter = QPainter(canvas)
    try:
        delegate.paint(painter, option, model.index(0, 0))
    finally:
        painter.end()

    image = canvas.toImage()

    def has_ink(x_from: int, x_to: int) -> bool:
        return any(
            image.pixelColor(x, y).alpha() > 0
            for x in range(max(0, x_from), min(width, x_to))
            for y in range(height)
        )

    assert has_ink(0, 40), "左侧图标没画出来"
    assert has_ink(width - 80, width), "右侧名称没画出来"
    assert not has_ink(width // 2 - 12, width // 2 + 12), "中间弹簧区不该有内容"


def test_trash_album_delegate_draws_selection_background(qapp):
    """选中 / hover 由委托自绘（QSS 的 item 高亮已被压平）。"""
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QPainter
    from PySide6.QtWidgets import QStyle, QStyleOptionViewItem

    from albuswall.ui.delegate import TrashAlbumDelegate
    from albuswall.ui.model import AlbumEntry, AlbumModel

    model = AlbumModel()
    model.set_albums([AlbumEntry(uuid="t", title="Trash")], icon="T")
    delegate = TrashAlbumDelegate(row_height=40)

    def paint(state_flags) -> "QImage":
        option = QStyleOptionViewItem()
        option.rect = QRect(0, 0, 200, 40)
        option.font = qapp.font()
        option.state |= state_flags
        canvas = QPixmap(200, 40)
        canvas.fill(QColor(0, 0, 0, 0))
        painter = QPainter(canvas)
        try:
            delegate.paint(painter, option, model.index(0, 0))
        finally:
            painter.end()
        return canvas.toImage()

    plain = paint(QStyle.StateFlag.State_None)
    selected = paint(QStyle.StateFlag.State_Selected)
    # 选中态在行内（远离文字的地方）铺了半透明底色
    assert plain.pixelColor(100, 20).alpha() == 0
    assert selected.pixelColor(100, 20).alpha() > 0
