#
""""""

from __future__ import annotations

from logging import getLogger
from typing import TYPE_CHECKING, Protocol, runtime_checkable, Callable, Sequence

from PySide6.QtCore import QObject, Signal, Qt

if TYPE_CHECKING:
    from albuswall.core import Container
    from ..window import Window

_logger = getLogger(__name__)


# 模块级 helper
def _import(module: str, cls_name: str):
    """延迟 + 相对 import，只在这里写一次。"""
    import importlib
    mod = importlib.import_module(module, __package__)
    return getattr(mod, cls_name)


def _thumb_spec():
    try:
        from albuswall.dto.thumbnail import ThumbSpec
        return ThumbSpec.MEDIUM
    except ImportError:
        _logger.warning("ThumbSpec unavailable; using default spec.")
        return None


@runtime_checkable
class Presenter(Protocol):
    def setup(self, container: "Container") -> None: ...

    def teardown(self) -> None: ...


class PresenterManager(QObject):
    """UI 层组合根。

    构建策略：每个 presenter 独立 try/except。任一 presenter 的依赖
    （模块缺失 / 服务缺失 / 构造异常）都不会影响其它 presenter。
    UI 主窗口框架在极端情况下也要能起来。
    """

    _refresh_after_import = Signal()

    def __init__(self, window: "Window", container: "Container") -> None:
        super().__init__()
        self._window = window
        self._container = container
        self._presenters: list[Presenter] = []

        self.window_presenter: Presenter | None = None
        self.album_presenter: Presenter | None = None
        self.thumb_presenter: Presenter | None = None
        self.ingest_presenter: Presenter | None = None
        self.viewer_presenter: Presenter | None = None

        self._import_service = None

    # ---------------- 构建 ----------------

    def build(self) -> "PresenterManager":
        self._construct()
        self._wire()
        return self

    def _construct(self) -> None:
        w, c = self._window, self._container

        # ---- 1. WindowPresenter（无业务依赖，最优先）----
        if (p := self._try_presenter(
                "WindowPresenter",
                lambda: _import(".window_presenter", "WindowPresenter")(w, w),
        )) is not None:
            self.window_presenter = self.add(p)

        # ---- 2. AlbumPresenter（依赖 view_service）----
        if (p := self._try_presenter(
                "AlbumPresenter",
                lambda: _import(".album_presenter", "AlbumPresenter")(
                    w.title_bar, w.album.view,
                    c.require("view_service"), parent=self,
                ),
                requires=("view_service",),
        )) is not None:
            self.album_presenter = self.add(p)

        # ---- 3. ThumbnailGridPresenter（依赖 thumbnail_repo + service）----
        if (p := self._try_presenter(
                "ThumbnailGridPresenter",
                lambda: _import(".image_presenter", "ThumbnailGridPresenter")(
                    w.content,
                    thumb_repo=c.require("thumbnail_repo"),
                    thumb_service=c.require("thumbnail_service"),
                    spec=_thumb_spec(),
                ),
                requires=("thumbnail_repo", "thumbnail_service"),
        )) is not None:
            self.thumb_presenter = self.add(p)

        # ---- 4. IngestSourcePresenter（依赖 source_service）----
        if (p := self._try_presenter(
                "IngestSourcePresenter",
                lambda: _import(".source_presenter", "IngestSourcePresenter")(
                    w.source, c.require("source_service"),
                ),
                requires=("source_service",),
        )) is not None:
            self.ingest_presenter = self.add(p)

        # ---- 5. ViewerPresenter（依赖 view_service，可选 thumbnail/asset）----
        if (p := self._try_presenter(
                "ViewerPresenter",
                lambda: _import(".viewer_presenter", "ViewerPresenter")(
                    w.detail,
                    c.require("view_service"),
                    c.get("thumbnail_service", None),
                    c.get("asset_repo", None),
                ),
                requires=("view_service",),
        )) is not None:
            self.viewer_presenter = self.add(p)

    def _try_presenter(
            self,
            name: str,
            factory: Callable[[], "Presenter | None"],
            *,
            requires: Sequence[str] = (),
    ) -> "Presenter | None":
        """声明式构建：缺失依赖或构建失败 → 返回 None。

        - requires 里的每个 key 用 container.get(key, None) 探测
        - 缺失 → 警告，跳过
        - 构造抛异常 → 记录完整 traceback，返回 None
        """
        missing = [k for k in requires if self._container.get(k, None) is None]
        if missing:
            _logger.warning("%s skipped: missing %s", name, missing)
            return None
        try:
            return factory()
        except Exception:
            _logger.exception("%s construction failed", name)
            return None

    def _wire(self) -> None:
        if self.thumb_presenter is not None and \
                self.viewer_presenter is not None:
            self.thumb_presenter.item_activated.connect(
                self._on_item_activated,
            )

        if self.album_presenter is not None:
            self.album_presenter.album_changed.connect(self._on_album_changed)

        if self.viewer_presenter is not None:
            view = self.viewer_presenter.view
            view.asset_navigate_requested.connect(self._on_detail_navigate)
            view.close_requested.connect(self._on_detail_close_requested)

        import_service = self._container.get("import_service", None)
        if import_service is not None:
            self._refresh_after_import.connect(
                self._refresh_grid, Qt.ConnectionType.QueuedConnection,
            )
            import_service.assets_imported.connect(self._on_assets_imported)
            self._import_service = import_service
        else:
            _logger.warning(
                "import_service unavailable; auto-refresh disabled.")

        if self.viewer_presenter is not None:
            view = self.viewer_presenter.view
            view.asset_navigate_requested.connect(self._on_detail_navigate)
            view.close_requested.connect(self._on_detail_close_requested)
            view.assets_changed.connect(self._on_assets_changed)  # ← 新增

    # ---------------- slots ----------------

    def _on_album_changed(self, album) -> None:
        self._close_viewer()
        self._apply_album_to_grid(album)

    def _on_assets_imported(self, _sender, **_totals) -> None:
        self._refresh_after_import.emit()

    def _on_assets_changed(self, _asset_ids) -> None:
        """收藏 / 软删等资产状态变化 → 立即刷新网格。

        不指定 ConnectionType，让它在同一线程走 DirectConnection：
        调用方 emit 之后就完成刷新，视觉上就是"删了立刻少一格"。
        """
        self._refresh_grid()

    def _refresh_grid(self) -> None:
        if self.album_presenter is None:
            return
        self._apply_album_to_grid(self.album_presenter.current)

    def _apply_album_to_grid(self, album) -> None:
        if self.thumb_presenter is None:
            return
        asset_ids, include_deleted = self._album_scope(album)
        self.thumb_presenter.set_assets(
            asset_ids, include_deleted=include_deleted,
        )

    # ---- viewer ----

    def _on_item_activated(self, asset_id) -> None:
        if self.album_presenter is None or self.viewer_presenter is None:
            return

        album = self.album_presenter.current
        if album is None:
            _logger.warning("item activated with no current album, ignored")
            return

        try:
            aid = int(asset_id)
        except (TypeError, ValueError):
            _logger.error("item_activated payload not int-like: %r", asset_id)
            return

        try:
            from .viewer_presenter import ViewerContext
        except ImportError as exc:
            _logger.error("ViewerContext unavailable: %s", exc)
            return

        ctx = ViewerContext(
            album_uuid=album.uuid,
            current_asset_id=aid,
            include_deleted=self._include_deleted(album),
        )
        ok = self.viewer_presenter.open(ctx)
        if self.window_presenter is not None:
            self.window_presenter.show_detail()
        if not ok:
            _logger.info("viewer opened with placeholder (asset=%d)", aid)

    def _on_detail_navigate(self, asset_id) -> None:
        if self.viewer_presenter is None:
            return
        try:
            aid = int(asset_id)
        except (TypeError, ValueError):
            _logger.error("navigate payload not int-like: %r", asset_id)
            return
        self.viewer_presenter.navigate(aid)

    def _on_detail_close_requested(self) -> None:
        self._close_viewer()

    def _close_viewer(self) -> None:
        if self.window_presenter is not None:
            self.window_presenter.hide_detail()
        if self.viewer_presenter is not None:
            self.viewer_presenter.teardown()
        # 详情页半透明浮层消失后，强制网格重绘，把覆盖期间的变更画出来
        self._window.detail.refresh()
        self._refresh_grid()

    # ---- scope helpers ----

    def _album_scope(self, album) -> tuple[list[int], bool]:
        view_service = self._container.get("view_service", None)
        if view_service is None or album is None:
            return [], False
        try:
            return (
                view_service.get_asset_ids(album),
                view_service.should_include_deleted(album.uuid),
            )
        except Exception as exc:
            _logger.error("resolve album scope failed for %s: %s", album, exc)
            return [], False

    def _include_deleted(self, album) -> bool:
        if album is None:
            return False
        view_service = self._container.get("view_service", None)
        if view_service is None:
            return False
        try:
            return view_service.should_include_deleted(album.uuid)
        except Exception as exc:
            _logger.error(
                "resolve include_deleted failed for %s: %s", album, exc)
            return False

    # ---------------- lifecycle ----------------

    def add(self, presenter: Presenter) -> Presenter:
        self._presenters.append(presenter)
        return presenter

    def setup(self, container: "Container") -> None:
        for p in self._presenters:
            try:
                p.setup(container)
            except Exception as exc:
                _logger.exception(
                    "presenter %s.setup failed: %s", type(p).__name__, exc)

    def teardown(self) -> None:
        if self._import_service is not None:
            try:
                self._import_service.assets_imported.disconnect(
                    self._on_assets_imported,
                )
            except (RuntimeError, TypeError):
                pass
            self._import_service = None

        for p in reversed(self._presenters):
            try:
                p.teardown()
            except Exception as exc:
                _logger.exception(
                    "presenter %s.teardown failed: %s",
                    type(p).__name__, exc)
        self._presenters.clear()

    # ---------------- debug / repr ----------------

    def __iter__(self):
        return iter(self._presenters)

    def __len__(self) -> int:
        return len(self._presenters)

    def __str__(self) -> str:
        return self.format(index=0, indent="\t")

    def format(self, index: int = 0, indent: str = "\t") -> str:
        head = f"{indent * index}{type(self).__name__}: ("
        body = [
            f"{indent * (index + 1)}{type(p).__name__}: "
            + f"\n{indent * (index + 1)}".join(str(p).splitlines())
            for p in self
        ]
        tail = f"{indent * index})"
        return "\n".join((head, *body, tail))
