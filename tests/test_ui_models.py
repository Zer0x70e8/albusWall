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
    assert model.data(model.index(1, 0), MetadataRole.Value) in ("是", "True")


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


def test_info_cell_delegate_from_model_reads_item_roles(qapp, pixmap):
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QPainter

    from albuswall.ui.delegate import InfoCellDelegate, InfoMode
    from albuswall.ui.model import AssetGridModel
    from albuswall.ui.widgets.virtual_scroll.delegate import CellOption

    model = AssetGridModel()
    model.append_asset("a1", "图", pixmap, favorite=True, days_left=3)

    delegate = InfoCellDelegate.from_model(model, mode=InfoMode.FAVORITE)
    canvas = QPixmap(48, 48)
    painter = QPainter(canvas)
    try:
        delegate.paint(
            painter,
            CellOption(index=0, rect=QRect(0, 0, 48, 48), cell_size=48),
        )
    finally:
        painter.end()
