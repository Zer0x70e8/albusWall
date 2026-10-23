#
"""标签功能回归：搜索 / 当前相册过滤 / 路径式新建。

不依赖 pytest：``python test/test_tag_features.py`` 直接运行。
使用内存 SQLite + offscreen Qt，不需要显示器。
"""

from __future__ import annotations

import os
import sys
import traceback
import uuid
from pathlib import Path
from types import SimpleNamespace

# 强制离屏：桌面会话里可能已有 QT_QPA_PLATFORM=wayland;xcb，
# setdefault 会被它绕过 —— 测试就会真的弹窗口。
os.environ["QT_QPA_PLATFORM"] = "offscreen"

try:  # 允许直接以文件路径运行
    import albuswall  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtWidgets import QApplication, QWidget

from albuswall.core import Container
from albuswall.infrastructure.database import Connector
from albuswall.repositories.asset import AssetRepository
from albuswall.repositories.tag import TagRepository
from albuswall.repositories.view import ViewRepository
from albuswall.services.asset_tag import AssetTagService
from albuswall.services.tag import TagError, TagService
from albuswall.services.view import ViewService

_app = QApplication.instance() or QApplication([])


# ══════════════════════════════════════════════════════════════
# 测试数据
# ══════════════════════════════════════════════════════════════
class Fixture:
    def __init__(self) -> None:
        self.db = Connector(None)
        self.conn = self.db.connect()

        self.album_uuid = str(uuid.uuid4())
        self.a1, self.a2, self.a3 = (str(uuid.uuid4()) for _ in range(3))

        for asset_uuid in (self.a1, self.a2, self.a3):
            self.conn.execute(
                "INSERT INTO assets "
                "(uuid, file_path, source_id, original_name, mime_type, "
                " file_hash) VALUES (?, ?, 0, ?, ?, ?)",
                (asset_uuid, f"x/{asset_uuid}.jpg", "a.jpg", "image/jpeg",
                 asset_uuid),
            )
        self.conn.execute(
            "INSERT INTO albums (uuid, title) VALUES (?, 'Trip')",
            (self.album_uuid,),
        )
        for asset_uuid in (self.a1, self.a2):
            self.conn.execute(
                "INSERT INTO album_assets (album_id, asset_id) "
                "SELECT id, (SELECT id FROM assets WHERE uuid = ?) "
                "  FROM albums WHERE uuid = ?",
                (asset_uuid, self.album_uuid),
            )
        self.conn.commit()

        self.tag_repo = TagRepository(self.db)
        self.tag_service = TagService(self.tag_repo)
        self.asset_tag_service = AssetTagService(
            AssetRepository(self.db), self.tag_repo, ViewRepository(self.db),
        )
        self.view_service = ViewService(ViewRepository(self.db))

        self.travel = self.tag_service.create("旅行")
        self.japan = self.tag_service.create("日本", self.travel)
        self.work = self.tag_service.create("工作")
        self.asset_tag_service.add(self.a1, [str(self.japan)])
        # 工作只绑在相册外的 a3 上，用来验证「当前相册」过滤范围
        self.asset_tag_service.add(self.a3, [str(self.work)])


# ══════════════════════════════════════════════════════════════
# 1. 仓库 / service：按当前相册搜索标签
# ══════════════════════════════════════════════════════════════
def test_search_tags_scoped_to_album() -> None:
    fx = Fixture()

    rows = fx.tag_service.search("日", album_uuid=fx.album_uuid)
    assert [m.path for m in rows] == ["旅行/日本"], rows
    assert rows[0].asset_count == 1

    # a3 不在相册里，因此 "工作" 不在结果中
    assert fx.tag_service.search("工作", album_uuid=fx.album_uuid) == []

    # 虚拟 active scope 能看到 a3 上的标签
    rows = fx.tag_service.search("工作", scope="active")
    assert [m.path for m in rows] == ["工作"], rows


def test_search_matches_full_path() -> None:
    fx = Fixture()
    # 父标签本身没有直接绑定资产，但子路径命中后仍可被搜索到
    rows = fx.tag_service.search("旅行", album_uuid=fx.album_uuid)
    assert [m.path for m in rows] == ["旅行/日本"], rows


def test_subtree_asset_uuids() -> None:
    fx = Fixture()
    assert [str(u) for u in fx.tag_service.asset_uuids(fx.travel)] == [fx.a1]
    assert fx.tag_service.asset_uuids(
        fx.travel, include_descendants=False
    ) == []


def test_ensure_path_reuses_existing_segments() -> None:
    fx = Fixture()

    kyoto = fx.tag_service.ensure_path("旅行/日本/京都")
    assert fx.tag_service.path_label(kyoto) == "旅行/日本/京都"

    # 再输入一次不会新建，也不会报重名
    again = fx.tag_service.ensure_path("旅行/日本/京都")
    assert again == kyoto

    try:
        fx.tag_service.ensure_path(" / ")
    except TagError:
        pass
    else:  # pragma: no cover
        raise AssertionError("空路径应抛 TagError")


def test_cascade_delete_removes_whole_subtree() -> None:
    from albuswall.services.tag import TagHasChildren, TagNotFound

    fx = Fixture()
    kyoto = fx.tag_service.ensure_path("旅行/日本/京都")

    try:
        fx.tag_service.delete(fx.travel)
    except TagHasChildren:
        pass
    else:  # pragma: no cover
        raise AssertionError("不级联时应拒绝删除含子节点的标签")

    fx.tag_service.delete(fx.travel, cascade=True)
    for uuid_value in (fx.travel, fx.japan, kyoto):
        try:
            fx.tag_service.get(uuid_value)
        except TagNotFound:
            continue
        raise AssertionError(f"子孙标签未被级联软删: {uuid_value}")


# ══════════════════════════════════════════════════════════════
# 2. SearchPresenter：模式 / 过滤意图
# ══════════════════════════════════════════════════════════════
def _make_search_presenter(fx: Fixture):
    from albuswall.ui.builtin.presenter.search_presenter import (
        SearchPresenter,
    )
    from albuswall.ui.builtin.widgets.floating_search_bar import (
        FloatingSearchBar,
        SearchBarLayoutPlaceholder,
    )

    host = QWidget()
    host.resize(900, 300)
    host.show()
    placeholder = SearchBarLayoutPlaceholder(host)
    placeholder.resize(100, 32)
    placeholder.show()

    view = FloatingSearchBar(host, host, placeholder)
    album = SimpleNamespace(uuid=fx.album_uuid)
    presenter = SearchPresenter(
        view,
        tag_service=fx.tag_service,
        view_service=fx.view_service,
        current_album_provider=lambda: album,
    )
    view.show()
    return presenter, view, host


def test_search_bar_modes() -> None:
    view_modes = None
    from albuswall.ui.builtin.widgets.floating_search_bar import (
        FloatingSearchBar,
        SearchBarLayoutPlaceholder,
    )

    host = QWidget()
    placeholder = SearchBarLayoutPlaceholder(host)
    view = FloatingSearchBar(host, host, placeholder)
    # 模式标识存在 itemData 里（稳定，不随界面语言变化），
    # itemText 是展示文案（会随语言切换重写）。
    view_modes = [
        view.option_combo.itemData(i)
        for i in range(view.option_combo.count())
    ]
    assert view_modes == [
        FloatingSearchBar.MODE_SIMPLE,
        FloatingSearchBar.MODE_COMMAND,
    ], view_modes
    texts = [
        view.option_combo.itemText(i)
        for i in range(view.option_combo.count())
    ]
    assert all(texts), texts
    assert len(texts) == len(view_modes)


def test_simple_mode_applies_tag_filter() -> None:
    fx = Fixture()
    presenter, view, _host = _make_search_presenter(fx)
    seen: list = []
    presenter.filter_requested.connect(seen.append)

    view.search.emit({"text": "日本", "option": view.MODE_SIMPLE})
    assert seen == [str(fx.japan)], seen
    assert view.line_edit.text() == "旅行/日本"

    # 空搜索 = 清除过滤
    view.search.emit({"text": "", "option": view.MODE_SIMPLE})
    assert seen[-1:] == [None], seen


def test_command_mode_is_placeholder() -> None:
    fx = Fixture()
    presenter, view, _host = _make_search_presenter(fx)
    seen: list = []
    errors: list = []
    presenter.filter_requested.connect(seen.append)
    presenter.error_occurred.connect(errors.append)

    view.search.emit({"text": "日本", "option": view.MODE_COMMAND})
    assert seen == []
    assert errors, "命令模式应提示未实现"

    presenter.teardown()


def test_ambiguous_query_shows_suggestions() -> None:
    fx = Fixture()
    # 造一个和 "旅行/日本" 都包含 "日" 且当前相册内有资产的候选
    daily = fx.tag_service.create("日常")
    fx.asset_tag_service.add(fx.a2, [str(daily)])
    presenter, view, _host = _make_search_presenter(fx)
    seen: list = []
    presenter.filter_requested.connect(seen.append)

    view.search.emit({"text": "日", "option": view.MODE_SIMPLE})
    assert seen == [], "多结果时不应立即过滤"
    assert view._suggestions.list.count() >= 2  # noqa: SLF001


# ══════════════════════════════════════════════════════════════
# 3. PresenterManager：相册资产 ∩ 标签资产
# ══════════════════════════════════════════════════════════════
def test_manager_intersects_album_with_tag() -> None:
    fx = Fixture()
    from albuswall.ui.builtin.presenter.manager import PresenterManager

    container = Container()
    container.register("tag_service", lambda: fx.tag_service)
    container.register("view_service", lambda: fx.view_service)

    manager = PresenterManager(None, container)
    manager._tag_filter_uuid = str(fx.travel)  # noqa: SLF001

    album_uuids = [fx.a1, fx.a2]
    assert manager._apply_tag_filter(album_uuids, False) == [fx.a1]  # noqa: SLF001

    # 清除过滤后恢复相册全集
    manager._tag_filter_uuid = None  # noqa: SLF001
    assert manager._apply_tag_filter(album_uuids, False) == album_uuids  # noqa: SLF001


def test_manager_wires_search_filter_signal() -> None:
    fx = Fixture()
    from albuswall.ui.builtin.presenter.manager import PresenterManager

    presenter, _view, _host = _make_search_presenter(fx)
    container = Container()
    container.register("tag_service", lambda: fx.tag_service)
    container.register("view_service", lambda: fx.view_service)

    manager = PresenterManager(None, container)
    manager.search_presenter = presenter
    manager._wire_search_signals()  # noqa: SLF001

    presenter.filter_requested.emit(str(fx.travel))
    assert manager._tag_filter_uuid == str(fx.travel)  # noqa: SLF001

    presenter.filter_requested.emit(None)
    assert manager._tag_filter_uuid is None  # noqa: SLF001


# ══════════════════════════════════════════════════════════════
# 4. TagBar：路径式打标 + chip 信号透出
# ══════════════════════════════════════════════════════════════
def test_tagbar_applies_nested_path_and_relays_chip_uuid() -> None:
    fx = Fixture()
    from albuswall.ui.builtin.presenter.tagbar_presenter import TagBarPresenter
    from albuswall.ui.builtin.window.tagbar import TagBar

    bar = TagBar()
    bar.show()
    presenter = TagBarPresenter(
        bar,
        tag_service=fx.tag_service,
        asset_tag_service=fx.asset_tag_service,
        current_asset_provider=lambda: fx.a2,
    )
    errors: list = []
    presenter.error_occurred.connect(errors.append)

    try:
        bar.tag_line_edit.setText("旅行/日本/京都")
        bar.tag_apply_button.click()
        assert errors == [], errors
        assert bar.tags_view.model().rowCount() == 1

        chip_uuid = bar.tags_view.model().uuid_at(0)
        assert fx.tag_service.path_label(chip_uuid) == "旅行/日本/京都"

        # chip 激活信号必须是 str（Signal(str) 传 UUID 会被 Qt 转成空串）
        relayed: list = []
        bar.chip_activated.connect(relayed.append)
        bar.tags_view.chip_activated.emit(str(chip_uuid))
        assert relayed == [str(chip_uuid)], relayed
    finally:
        presenter.teardown()


# ══════════════════════════════════════════════════════════════
# 运行器
# ══════════════════════════════════════════════════════════════
def main() -> int:
    tests = [
        (name, obj)
        for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    failures = 0
    for name, func in tests:
        try:
            func()
        except Exception:  # noqa: BLE001
            failures += 1
            print(f"FAIL {name}")
            traceback.print_exc()
        else:
            print(f"ok   {name}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
