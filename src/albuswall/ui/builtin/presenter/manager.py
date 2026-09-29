#
""""""

from __future__ import annotations

from logging import getLogger
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from PySide6.QtCore import QObject, Signal, Qt

from albuswall.dto.thumbnail import ThumbSpec

from .viewer_presenter import ViewerContext

if TYPE_CHECKING:
    from albuswall.core import Container
    from ..window import Window

_logger = getLogger(__name__)


@runtime_checkable
class Presenter(Protocol):
    def setup(self, container: "Container") -> None: ...

    def teardown(self) -> None: ...


class PresenterManager(QObject):
    """UI 层组合根。

    职责：
    1. 构造所有 presenter，从 container 注入依赖（只 get，不 reg）；
    2. 连接 UI 内信号与跨线程桥接（显式 QueuedConnection）；
    3. 按注册顺序 setup、逆序 teardown。

    容器保持 Qt 无关；所有"谁听谁"的决策都在这里。
    """

    # 后端 assets_imported（worker 线程）→ 主线程刷新网格
    _refresh_after_import = Signal()

    window_presenter = None
    album_presenter = None
    thumb_presenter = None
    ingest_presenter = None
    viewer_presenter = None

    def __init__(self, window: "Window", container: "Container") -> None:
        super().__init__()
        self._window = window
        self._container = container
        self._presenters: list[Presenter] = []

    # ---------------- 构建 ----------------

    def build(self) -> "PresenterManager":
        self._construct()
        self._wire()
        return self

    def _construct(self) -> None:
        # 延迟导入，避免与 presenter 包产生循环
        from .window_presenter import WindowPresenter
        from .album_presenter import AlbumPresenter
        from .image_presenter import ThumbnailGridPresenter
        from .source_presenter import IngestSourcePresenter
        from .viewer_presenter import ViewerPresenter

        w, c = self._window, self._container

        self.window_presenter = WindowPresenter(w, w)
        self.album_presenter = AlbumPresenter(
            w.title_bar,
            w.album.view,
            c.get("view_service"),
            parent=self
        )
        self.thumb_presenter = ThumbnailGridPresenter(
            w.content,
            thumb_repo=c.get("thumbnail_repo"),
            thumb_service=c.get("thumbnail_service"),
            spec=ThumbSpec.MEDIUM,
        )
        self.ingest_presenter = IngestSourcePresenter(
            w.source, c.get("source_service"))
        self.viewer_presenter = ViewerPresenter(
            w.detail,
            c.get("view_service"),
            c.get("thumbnail_service"),
            c.get("asset_repo")
        )

        # 注册顺序 = setup 顺序；teardown 逆序
        self.add(self.window_presenter)
        self.add(self.album_presenter)
        self.add(self.thumb_presenter)
        self.add(self.ingest_presenter)
        self.add(self.viewer_presenter)

    def _wire(self) -> None:
        # ---- ui ----
        # 缩略图被激活 → 走 ViewerPresenter 的统一入口，而不是直接调 window
        self.thumb_presenter.item_activated.connect(self._on_item_activated)
        # self.album_presenter.album_changed.connect(self._on_album_changed)

        # 详情页内部导航
        self.viewer_presenter.view.asset_navigate_requested.connect(
            self._on_detail_navigate,
        )
        # 详情页关闭 → 释放 Presenter 资源（切页已由 WindowPresenter 自己处理）
        self.viewer_presenter.view.close_requested.connect(
            self.viewer_presenter.teardown,
        )

        # 详情页关闭（Esc / 关闭按钮）→ 视图栈调度 + Presenter 自清理
        # 注意：ViewerPresenter 内部已 connect 了一个 _on_close_requested 做在途取消，
        # 这里再挂一个做"视图收起"，两者互不冲突（Qt 允许多槽）。
        self.viewer_presenter.view.close_requested.connect(
            self._on_detail_close_requested,
        )

        # 相册变化 → 网格刷新（详情页要先关，见 _on_album_changed）
        self.album_presenter.album_changed.connect(self._on_album_changed)

        # ---- threading ----（保持不变）
        self._refresh_after_import.connect(
            self._refresh_grid, Qt.ConnectionType.QueuedConnection,
        )
        self._container.get("import_service").assets_imported.connect(
            self._on_assets_imported,
        )

    # slots
    def _on_album_changed(self, album) -> None:
        self.window_presenter.hide_detail()
        self.viewer_presenter.teardown()

        view_service = self._container.get("view_service")
        try:
            asset_ids = view_service.get_asset_ids(album)
            include_deleted = view_service.should_include_deleted(album.uuid)
        except Exception as exc:
            _logger.error("list asset ids failed for album %s: %s", album, exc)
            asset_ids = []
            include_deleted = False

        self.thumb_presenter.set_assets(
            asset_ids, include_deleted=include_deleted,
        )

    def _on_assets_imported(self, _sender, **_totals) -> None:
        # 运行在 albuswall-import-worker 线程；只发 Qt 信号，不碰 UI
        self._refresh_after_import.emit()

    def _refresh_grid(self) -> None:
        try:
            album = self.album_presenter.current
            view_service = self._container.get("view_service")
            asset_ids = view_service.get_asset_ids(album)
            include_deleted = (
                view_service.should_include_deleted(album.uuid)
                if album is not None else False
            )
        except Exception as exc:
            _logger.error("refresh grid after import failed: %s", exc)
            return
        self.thumb_presenter.set_assets(
            asset_ids, include_deleted=include_deleted,
        )

    # ------------------------------------------------------------------ #
    # viewer 相关
    # ------------------------------------------------------------------ #
    def _on_item_activated(self, asset_id) -> None:
        """缩略图被激活 → 打开详情页。

        item_activated 的签名按 Signal(int) 处理；若上游发的是 uuid / DTO，
        这里包一层 adapter 归一到 int，别让 ViewerContext 收到错误类型。
        """
        album = self.album_presenter.current
        if album is None:
            _logger.warning("item activated with no current album, ignored")
            return

        try:
            aid = int(asset_id)
        except (TypeError, ValueError):
            _logger.error("item_activated payload not int-like: %r", asset_id)
            return

        view_service = self._container.get("view_service")
        include_deleted = view_service.should_include_deleted(album.uuid)

        ctx = ViewerContext(
            album_uuid=album.uuid,
            current_asset_id=aid,
            include_deleted=include_deleted,  # ← 由当前相册 scope 决定
        )
        ok = self.viewer_presenter.open(ctx)

        self.window_presenter.show_detail()
        if not ok:
            _logger.info("viewer opened with placeholder (asset=%d)", aid)

    def _on_detail_navigate(self, asset_id) -> None:
        """详情页内部翻页：沿用同一 album_uuid，只换 current_asset_id。"""
        try:
            aid = int(asset_id)
        except (TypeError, ValueError):
            return
        self.viewer_presenter.navigate(aid)

    def _on_detail_close_requested(self) -> None:
        """详情页关闭请求 → 收起视图栈 + 释放 Presenter 资源。"""
        self.window_presenter.hide_detail()      # 若不存在，见第 4 节
        self.viewer_presenter.teardown()         # 可重复调用，安全

    def _album_scope(self, album) -> tuple[list[int], bool]:
        """取 (asset_ids, include_deleted)，供网格/刷新/详情统一使用。"""
        view_service = self._container.get("view_service")
        if album is None:
            return [], False
        try:
            return (
                view_service.get_asset_ids(album),
                view_service.should_include_deleted(album.uuid),
            )
        except Exception as exc:
            _logger.error("resolve album scope failed for %s: %s", album, exc)
            return [], False

    # interface
    def add(self, presenter):
        self._presenters.append(presenter)
        return presenter  # 方便链式调用

    # noinspection calling-non-callable
    def setup(self, container: "Container") -> None:
        for p in self._presenters:
            setup = getattr(p, "setup", None)
            if callable(setup):
                setup(container)

    # noinspection calling-non-callable
    def teardown(self) -> None:
        for p in reversed(self._presenters):
            teardown = getattr(p, "teardown", None)
            if callable(teardown):
                teardown()
        self._presenters.clear()

    def __iter__(self):
        return iter(self._presenters)

    def __len__(self) -> int:
        return len(self._presenters)

    def __str__(self) -> str:
        return "\n".join((
            f"{type(self).__name__}: (",
            *[
                f"\t{type(i).__name__}: " +
                "\n\t".join(str(i).splitlines())
                for i in self
            ],
            ")",
        ))

    def format(self, index=0, indent="\t"):
        return "\n".join((
            f"{indent * index}{type(self).__name__}: (",
            *[f"{indent * (index + 1)}{type(i).__name__}: " +
              f"\n{(indent * (index + 1))}".join(
                  str(i).splitlines()
              ) for i in self],
            f"{indent * index})",
        ))

    # def __format__(self, spec: str, /) -> str:
    #     if spec.startswith("index"):
    #         index_str = spec[5:].lstrip("=")
    #         if index_str.isdigit():
    #             return self.format(int(index_str))
    #         elif (index_str.startswith("-") and
    #               index_str[1:].isdigit()):
    #             return self.format(int(index_str))
    #     return str(self)
