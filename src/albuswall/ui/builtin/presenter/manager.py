#
"""UI 层组合根（composition root）。

职责
----
- 构造各 presenter：任一 presenter 的依赖缺失 / 构造异常不影响其它；
- 用 :class:`SignalAdapter` 统一接线（直连 / 跨线程排队 / 转发）；
- ``teardown`` 一次性断开所有连接并逆序拆解 presenter。

构建契约
--------
组合根只通过 uuid 与服务层交互：

- ``view_service.get_asset_uuids(album)`` → ``list[UUID]``
- ``view_service.get_scope(album.uuid)``  → ``"active" | "deleted" | None``

不调用任何 ``*_by_id`` 接口。

信号接线约定
------------
- 所有跨对象信号走 :class:`SignalAdapter`，不再手写
  「后端槽 → 内部中转信号 → 主线程槽」三件套；
- 跨线程桥接用 ``adapter.connect(..., queued=True)``，自动
  ``QueuedConnection``，把后台线程触发投递到主线程；
- 参数过滤走 ``predicate=``，参数清空走 ``transform=``；
- ``teardown`` 一句 ``adapter.disconnect_all()`` 清场。

生命周期
--------
::

    mgr = PresenterManager(window, container).build()
    mgr.setup(container)          # 可选：逐 presenter 调 setup()
    ...
    mgr.teardown()                # 断开 adapter 连接 + 逆序 teardown

``build()`` = ``_construct()`` + ``_wire()``；返回 self 便于链式调用。

最小示例
--------
::

    container = Container(...)
    window = Window()
    mgr = PresenterManager(window, container).build()
    mgr.setup(container)
    app.aboutToQuit.connect(mgr.teardown)
"""

from __future__ import annotations

from logging import getLogger
from typing import (
    TYPE_CHECKING,
    Callable,
    Optional,
    Protocol,
    Sequence,
    TypeVar,
    runtime_checkable,
)

from PySide6.QtCore import QObject

from albuswall.services.view import SCOPE_DELETED

from ..utils.signal_adapter import SignalAdapter

if TYPE_CHECKING:
    from albuswall.core import Container
    from ..window import Window

    from .album_presenter import AlbumPresenter
    from .image_presenter import ThumbnailGridPresenter
    from .source_presenter import IngestSourcePresenter
    from .viewer_presenter import ViewerPresenter
    from .window_presenter import WindowPresenter

_logger = getLogger(__name__)


# --------------------------------------------------------------------------- #
# 懒加载 helpers
# --------------------------------------------------------------------------- #
def _import(module: str, name: str):
    """延迟 + 相对 import，只在这里写一次。"""
    import importlib
    mod = importlib.import_module(module, __package__)
    return getattr(mod, name)


def _thumb_spec():
    try:
        from albuswall.dto.thumbnail import ThumbSpec
        return ThumbSpec.MEDIUM
    except ImportError:
        _logger.warning("ThumbSpec unavailable; using default spec.")
        return None


# --------------------------------------------------------------------------- #
# 信号 predicate / transform（模块级便于单测）
# --------------------------------------------------------------------------- #
def _drop_payload(_args, _kwargs):
    """``transform``：忽略入参，把载荷清成空。"""
    return (), {}


def _should_refresh_on_source_change(**kwargs) -> bool:
    """``predicate``：``kind == "purged"`` 时跳过刷新。"""
    return kwargs.get("kind") != "purged"


# --------------------------------------------------------------------------- #
# Presenter 协议
# --------------------------------------------------------------------------- #
@runtime_checkable
class Presenter(Protocol):
    def setup(self, container: "Container") -> None: ...

    def teardown(self) -> None: ...


_P = TypeVar("_P", bound=Presenter)


# --------------------------------------------------------------------------- #
# 主类
# --------------------------------------------------------------------------- #
# noinspection broad-exception
class PresenterManager(QObject):
    """UI 层组合根。详见模块 docstring。

    每个 presenter 独立 try/except 构造：任一 presenter 的依赖缺失
    （模块缺失 / 服务缺失 / 构造异常）都不会影响其它 presenter。
    """

    def __init__(self, window: "Window", container: "Container") -> None:
        super().__init__()
        self._window = window
        self._container = container
        self._presenters: list[Presenter] = []

        #: 统一信号接线；parent=self 让内部中继挂到本对象的生命周期上。
        self._adapter = SignalAdapter(self)

        self.window_presenter: Optional["WindowPresenter"] = None
        self.album_presenter: Optional["AlbumPresenter"] = None
        self.thumb_presenter: Optional["ThumbnailGridPresenter"] = None
        self.ingest_presenter: Optional["IngestSourcePresenter"] = None
        self.viewer_presenter: Optional["ViewerPresenter"] = None

    # ------------------------------------------------------------------ #
    # build
    # ------------------------------------------------------------------ #
    def build(self) -> "PresenterManager":
        """构造 + 接线。返回 self 便于链式调用。"""
        self._construct()
        self._wire()
        return self

    def _construct(self) -> None:
        """按固定顺序构造各 presenter；任一失败只跳过自己。"""
        self.window_presenter = self._build_window_presenter()
        self.album_presenter = self._build_album_presenter()
        self.thumb_presenter = self._build_thumb_presenter()
        self.ingest_presenter = self._build_ingest_presenter()
        self.viewer_presenter = self._build_viewer_presenter()

    def _build_window_presenter(self) -> Optional["WindowPresenter"]:
        w = self._window
        return self._try_presenter(
            "WindowPresenter",
            lambda: _import(".window_presenter", "WindowPresenter")(w, w),
        )

    def _build_album_presenter(self) -> Optional["AlbumPresenter"]:
        w, c = self._window, self._container
        return self._try_presenter(
            "AlbumPresenter",
            lambda: _import(".album_presenter", "AlbumPresenter")(
                w.title_bar, w.album.view,
                c.require("view_service"), parent=self,
            ),
            requires=("view_service",),
        )

    def _build_thumb_presenter(self) -> Optional["ThumbnailGridPresenter"]:
        w, c = self._window, self._container
        return self._try_presenter(
            "ThumbnailGridPresenter",
            lambda: _import(".image_presenter", "ThumbnailGridPresenter")(
                w.content,
                thumb_repo=c.require("thumbnail_repo"),
                thumb_service=c.require("thumbnail_service"),
                spec=_thumb_spec(),
            ),
            requires=("thumbnail_repo", "thumbnail_service"),
        )

    def _build_ingest_presenter(self) -> Optional["IngestSourcePresenter"]:
        w, c = self._window, self._container
        return self._try_presenter(
            "IngestSourcePresenter",
            lambda: _import(".source_presenter", "IngestSourcePresenter")(
                w.source,
                c.require("source_service"),
                c.require("source_trash_service"),
            ),
            requires=("source_service",),
        )

    def _build_viewer_presenter(self) -> Optional["ViewerPresenter"]:
        w, c = self._window, self._container
        return self._try_presenter(
            "ViewerPresenter",
            lambda: _import(".viewer_presenter", "ViewerPresenter")(
                w.detail,
                c.require("view_service"),
                c.get("thumbnail_service", None),
                c.get("asset_repo", None),
            ),
            requires=("view_service",),
        )

    def _try_presenter(
            self,
            name: str,
            factory: Callable[[], _P],
            *,
            requires: Sequence[str] = (),
    ) -> Optional[_P]:
        """构造并注册单个 presenter。

        依赖缺失 / 构造异常时记录日志、返回 None，不影响其它 presenter。
        成功时把实例加入 ``_presenters`` 并返回。
        """
        missing = [k for k in requires if self._container.get(k, None) is None]
        if missing:
            _logger.warning("%s skipped: missing %s", name, missing)
            return None
        try:
            presenter = factory()
        except Exception:
            _logger.exception("%s construction failed", name)
            return None
        return self.add(presenter)

    # ------------------------------------------------------------------ #
    # wire
    # ------------------------------------------------------------------ #
    def _wire(self) -> None:
        """用 SignalAdapter 建立所有跨对象连接。"""
        self._wire_presenter_signals()
        self._wire_viewer_view_signals()
        self._wire_grid_refresh_signals()

    def _wire_presenter_signals(self) -> None:
        """presenter ↔ presenter 之间的信号（同线程直连）。"""
        if self.thumb_presenter is not None and \
                self.viewer_presenter is not None:
            self._adapter.connect(
                self.thumb_presenter.item_activated,
                self._on_item_activated,
            )

        if self.album_presenter is not None:
            self._adapter.connect(
                self.album_presenter.album_changed,
                self._on_album_changed,
            )

    def _wire_viewer_view_signals(self) -> None:
        """viewer 视图 → manager（同线程直连）。"""
        if self.viewer_presenter is None:
            return
        view = self.viewer_presenter.view
        self._adapter.connect(
            view.asset_navigate_requested, self._on_detail_navigate,
        )
        self._adapter.connect(
            view.close_requested, self._on_detail_close_requested,
        )
        self._adapter.connect(
            view.assets_changed, self._on_assets_changed,
        )

    def _wire_grid_refresh_signals(self) -> None:
        """后台服务 → 刷新网格。

        两个源都触发 ``_refresh_grid``，都是跨线程桥接（``queued=True``）：

        · ``import_service.assets_imported`` —— 载荷是导入统计，不关心，
          用 ``transform`` 清空参数。
        · ``source_trash_service.sources_changed`` —— 只处理
          ``kind != "purged"`` 的情形：

            - ``"soft_delete"`` / ``"restore"``：源行的软删状态变了。
              刷新用的 SQL 里的「EXISTS (… s.is_deleted = 0)」会立刻
              剔除 / 恢复名下资产，此时必须刷新，用户才能拿到即时反馈。
            - ``"purged"``：只是把 DB 行和磁盘文件真正清掉。UI 可见
              集合在 soft_delete 那一步就已经空了，再刷一次纯属浪费
              且会闪烁。

          因此用 ``predicate`` 过滤掉 ``kind == "purged"``。
        """
        import_service = self._container.get("import_service", None)
        if import_service is not None:
            self._adapter.connect(
                import_service.assets_imported,
                self._refresh_grid,
                queued=True,
                transform=_drop_payload,
            )
        else:
            _logger.warning(
                "import_service unavailable; auto-refresh disabled."
            )

        source_trash = self._container.get("source_trash_service", None)
        if source_trash is not None:
            self._adapter.connect(
                source_trash.sources_changed,
                self._refresh_grid,
                queued=True,
                predicate=_should_refresh_on_source_change,
                transform=_drop_payload,
            )
        else:
            _logger.warning(
                "source_trash_service unavailable; "
                "source-level refresh disabled."
            )

    # ------------------------------------------------------------------ #
    # slots
    # ------------------------------------------------------------------ #
    def _on_album_changed(self, album) -> None:
        self._close_viewer()
        self._apply_album_to_grid(album)

    def _on_assets_changed(self, _asset_uuids) -> None:
        """收藏 / 软删等资产状态变化 → 立即刷新网格。

        入参是本批变化的资产 uuid 列表；本槽只关心"有变化"这一事实。
        """
        self._refresh_grid()

    def _refresh_grid(self) -> None:
        if self.album_presenter is None:
            return
        self._apply_album_to_grid(self.album_presenter.current)

    def _apply_album_to_grid(self, album) -> None:
        if self.thumb_presenter is None:
            return
        asset_uuids, include_deleted = self._album_scope(album)
        self.thumb_presenter.set_assets(
            asset_uuids, include_deleted=include_deleted,
        )

    # ---- viewer ----

    def _on_item_activated(self, asset_uuid: str) -> None:
        if self.album_presenter is None or self.viewer_presenter is None:
            return
        album = self.album_presenter.current
        if album is None:
            _logger.warning("item activated with no current album, ignored")
            return

        try:
            # noinspection pep8-naming
            ViewerContext = _import(".viewer_presenter", "ViewerContext")
        except ImportError:
            _logger.exception("ViewerContext unavailable")
            return

        ctx = ViewerContext(
            album_uuid=album.uuid,
            current_asset_uuid=asset_uuid,
            include_deleted=self._include_deleted(album),
        )
        try:
            ok = self.viewer_presenter.open(ctx)
        except Exception:
            _logger.exception(
                "viewer open failed for asset=%s", asset_uuid,
            )
            return
        if self.window_presenter is not None:
            self.window_presenter.show_detail()
        if not ok:
            _logger.info(
                "viewer opened with placeholder (asset=%s)", asset_uuid,
            )

    def _on_detail_navigate(self, asset_uuid: str) -> None:
        if self.viewer_presenter is None:
            return
        self.viewer_presenter.navigate(asset_uuid)

    def _on_detail_close_requested(self) -> None:
        self._close_viewer()

    def _close_viewer(self) -> None:
        if self.window_presenter is not None:
            self.window_presenter.hide_detail()
        if self.viewer_presenter is not None:
            self.viewer_presenter.teardown()
        self._window.detail.refresh()
        self._refresh_grid()

    # ---- scope helpers ----

    def _album_scope(self, album) -> tuple[list[str], bool]:
        """返回 ``(asset_uuids, include_deleted)``。

        - ``asset_uuids`` 是字符串 uuid 列表；
        - ``include_deleted`` 由 ``view_service.get_scope(album.uuid)``
          决定：``scope == "deleted"`` → True（Trash 视图）；其它 → False。
        """
        view_service = self._container.get("view_service", None)
        if view_service is None or album is None:
            return [], False
        try:
            uuids = view_service.get_asset_uuids(album)
            # noinspection unresolved-references
            include_deleted = (
                    view_service.get_scope(album.uuid) == SCOPE_DELETED
            )
            return [str(u) for u in uuids], include_deleted
        except Exception:
            _logger.exception("resolve album scope failed for %s", album)
            return [], False

    def _include_deleted(self, album) -> bool:
        """album 所属视图是否包含已删资产（Trash 视图）。"""
        if album is None:
            return False
        view_service = self._container.get("view_service", None)
        if view_service is None:
            return False
        try:
            return view_service.get_scope(album.uuid) == SCOPE_DELETED
        except Exception:
            _logger.exception(
                "resolve include_deleted failed for %s", album,
            )
            return False

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def add(self, presenter: _P) -> _P:
        """登记 presenter 并返回它（便于链式使用）。"""
        self._presenters.append(presenter)
        return presenter

    def setup(self, container: "Container") -> None:
        """逐 presenter 调用 ``setup()``；单个失败不影响其它。"""
        for p in self._presenters:
            try:
                p.setup(container)
            except Exception:
                _logger.exception(
                    "presenter %s.setup failed", type(p).__name__,
                )

    def teardown(self) -> None:
        """断开所有 adapter 连接并逆序 teardown presenter。"""
        # 一句清场：所有 adapter 建立的连接（含跨线程中继）一次性断开。
        self._adapter.disconnect_all()

        for p in reversed(self._presenters):
            try:
                p.teardown()
            except Exception:
                _logger.exception(
                    "presenter %s.teardown failed", type(p).__name__,
                )
        self._presenters.clear()

    # ------------------------------------------------------------------ #
    # debug / repr
    # ------------------------------------------------------------------ #
    def __iter__(self):
        return iter(self._presenters)

    def __len__(self) -> int:
        return len(self._presenters)

    def __str__(self) -> str:
        return self.format(index=0, indent="\t")

    def format(self, index: int = 0, indent: str = "\t") -> str:
        pad = indent * index
        child_pad = indent * (index + 1)
        head = f"{pad}{type(self).__name__}: ("
        body = []
        for p in self:
            nested = f"\n{child_pad}".join(str(p).splitlines() or [""])
            body.append(f"{child_pad}{type(p).__name__}: {nested}")
        tail = f"{pad})"
        return "\n".join((head, *body, tail))
