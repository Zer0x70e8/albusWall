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

from collections.abc import Callable
from typing import (
    TYPE_CHECKING,
    Optional,
    Protocol,
    TypeVar,
    runtime_checkable,
)

from PySide6.QtCore import QObject

from albuswall.common.enums import ThumbSpec
from albuswall.log import getLogger
from albuswall.services.view import SCOPE_DELETED

from ..config.static import ThumbnailGridPresenterConfs
from ..utils.signal_adapter import SignalAdapter

if TYPE_CHECKING:
    from albuswall.core import Container

    from ..window import Window
    from .album_presenter import AlbumPresenter
    from .image_presenter import ThumbnailGridPresenter
    from .source_presenter import IngestSourcePresenter
    from .tag_editor_presenter import TagEditorPresenter
    from .tagbar_presenter import TagBarPresenter
    from .viewer_presenter import ViewerPresenter
    from .window_presenter import WindowPresenter

__all__ = ["Presenter", "PresenterManager"]

_logger = getLogger(__name__)


class _DependencyMissingError(Exception):
    """presenter 需要的 container 依赖没注册 / 构造失败。"""


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
        self.tagbar_presenter: Optional["TagBarPresenter"] = None
        self.tag_editor_presenter: Optional["TagEditorPresenter"] = None

        # 当前"标签作用于哪个资产"（详情打开 → 详情资产；否则 grid 选中）
        self._active_asset_uuid: str | None = None
        self._detail_visible: bool = False

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
        self.tagbar_presenter = self._build_tagbar_presenter()
        self.tag_editor_presenter = self._build_tag_editor_presenter()

    def _build_window_presenter(self) -> Optional["WindowPresenter"]:
        from .window_presenter import WindowPresenter

        w = self._window
        return self._try_presenter("WindowPresenter", lambda: WindowPresenter(w, w))

    def _build_album_presenter(self) -> Optional["AlbumPresenter"]:
        from .album_presenter import AlbumPresenter

        w = self._window
        return self._try_presenter(
            "AlbumPresenter",
            lambda: AlbumPresenter(
                w.title_bar, w.album.view,
                self._require("view_service"),
                parent=self,
            ),
        )

    def _build_thumb_presenter(self) -> Optional["ThumbnailGridPresenter"]:
        from .image_presenter import ThumbnailGridPresenter

        try:
            confs = ThumbnailGridPresenterConfs()
        except Exception:
            _logger.exception("ThumbnailGridPresenterConfs unavailable")
            confs = None

        p = self._try_presenter(
            "ThumbnailGridPresenter",
            lambda: ThumbnailGridPresenter(
                self._window.content,
                thumb_repo=self._require("thumbnail_repo"),
                thumb_service=self._require("thumbnail_service"),
                spec=ThumbSpec.MEDIUM,
                view_repo=self._container.get("view_repo", None),
                trash_retention_days=30,
            ),
        )
        if p is not None and confs is not None:
            p.set_favorite_icon(confs.favorite_on_icon, checked=True)
        return p

    def _build_ingest_presenter(self) -> Optional["IngestSourcePresenter"]:
        from .source_presenter import IngestSourcePresenter

        return self._try_presenter(
            "IngestSourcePresenter",
            lambda: IngestSourcePresenter(
                self._window.source,
                self._require("source_service"),
                self._container.get("source_trash_service", None),
            ),
        )

    def _build_viewer_presenter(self) -> Optional["ViewerPresenter"]:
        from .viewer_presenter import ViewerPresenter

        return self._try_presenter(
            "ViewerPresenter",
            lambda: ViewerPresenter(
                self._window.detail,
                self._require("view_service"),
                self._container.get("thumbnail_service", None),
                self._container.get("asset_repo", None),
            ),
        )

    def _build_tagbar_presenter(self) -> Optional["TagBarPresenter"]:
        from .tagbar_presenter import TagBarPresenter

        return self._try_presenter(
            "TagBarPresenter",
            lambda: TagBarPresenter(
                self._window.detail.tag_bar,
                tag_service=self._require("tag_service"),
                asset_tag_service=self._require("asset_tag_service"),
                current_asset_provider=self._current_asset_for_tags,
                parent=self,
            ),
        )

    def _build_tag_editor_presenter(self) -> Optional["TagEditorPresenter"]:
        from .tag_editor_presenter import TagEditorPresenter

        return self._try_presenter(
            "TagEditorPresenter",
            lambda: TagEditorPresenter(
                self._require("tag_service"),
                parent_widget=self._window,
                parent=self,
            ),
        )

    def _require(self, name: str):
        """取 container 依赖；缺失时抛 :class:`_DependencyMissingError`。"""
        try:
            dep = self._container.get(name, None)
        except Exception as exc:
            raise _DependencyMissingError(f"{name}: {exc}") from exc
        if dep is None:
            raise _DependencyMissingError(name)
        return dep

    def _try_presenter(
            self,
            name: str,
            factory: Callable[[], _P],
    ) -> Optional[_P]:
        """构造并注册单个 presenter。

        依赖缺失 → warning；构造异常 → exception。两者都只跳过自己，
        并返回 None。
        """
        try:
            presenter = factory()
        except _DependencyMissingError as exc:
            _logger.warning("%s skipped: missing %s", name, exc)
            return None
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
        self._wire_tag_signals()

    def _wire_tag_signals(self) -> None:
        """tagbar ↔ grid / viewer 的接线。

        单向原则：
            · 数据变更（tag 绑定 / 元数据）→ 触发 refresh，不直接改 UI 状态；
            · 用户动作（tag 点击）→ 走一层 manager 槽再分发，不让 tagbar 直连 grid。

        tag 相关连接全部同线程直连（queued=False，默认），因为 tag 操作
        都在 UI 线程发起、也在 UI 线程写库（repo 层同步）。后台触发的
        写操作（如果有）另加 queued=True。
        """
        if self.tagbar_presenter is not None:
            self._adapter.connect(
                self.tagbar_presenter.tags_changed,
                self._on_tags_changed,
            )
            self._adapter.connect(
                self.tagbar_presenter.tag_activated,
                self._on_tag_activated,
            )
            self._adapter.connect(
                self.tagbar_presenter.error_occurred,
                self._on_tag_error,
            )

        if self.tag_editor_presenter is not None:
            self._adapter.connect(
                self.tag_editor_presenter.changed,
                self._on_tag_metadata_changed,
            )
            self._adapter.connect(
                self.tag_editor_presenter.error_occurred,
                self._on_tag_error,
            )

        # grid 选中 → tagbar 刷新。当前 ThumbnailGridPresenter 只暴露
        # item_activated（点击进入详情），没有 selection_changed；
        # 将来 grid 补上该信号后这里会自动接上，不需要改其它代码。
        if self.thumb_presenter is not None:
            selection = getattr(self.thumb_presenter, "selection_changed", None)
            if selection is not None:
                self._adapter.connect(selection, self._on_selection_changed)

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
        self._connect_refresh_source(
            "import_service", "assets_imported", queued=True,
        )
        self._connect_refresh_source(
            "source_trash_service", "sources_changed", queued=True,
            predicate=_should_refresh_on_source_change,
        )

    def _connect_refresh_source(
            self,
            service_name: str,
            signal_name: str,
            *,
            queued: bool,
            predicate: Optional[Callable[..., bool]] = None,
    ) -> None:
        """把 ``service.<signal_name>`` 桥到 ``_refresh_grid``（后台 → 主线程）。"""
        service = self._container.get(service_name, None)
        if service is None:
            _logger.warning("%s unavailable; auto-refresh disabled.", service_name)
            return
        self._adapter.connect(
            getattr(service, signal_name),
            self._refresh_grid,
            queued=queued,
            predicate=predicate,
            transform=_drop_payload,
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
        self._refresh_tagbar()

    def _refresh_grid(self) -> None:
        if self.album_presenter is None:
            return
        # 封面是派生数据（相册内最新资产 / scope 首项），资产集合变了它
        # 也可能从「无」变成「有」。这里和网格一起刷，否则会出现
        # 「缩略图有图、相册封面一直空白」的割裂状态。
        self.album_presenter.reload_covers()
        self._apply_album_to_grid(self.album_presenter.current)

    def _apply_album_to_grid(self, album) -> None:
        if self.thumb_presenter is None:
            return
        asset_uuids, include_deleted = self._album_scope(album)
        self.thumb_presenter.set_assets(
            asset_uuids, include_deleted=include_deleted,
        )

    def _refresh_tagbar(self) -> None:
        if self.tagbar_presenter is not None:
            self.tagbar_presenter.refresh()

    # ---- viewer ----

    def _on_item_activated(self, asset_uuid: str) -> None:
        if self.album_presenter is None or self.viewer_presenter is None:
            return
        album = self.album_presenter.current
        if album is None:
            _logger.warning("item activated with no current album, ignored")
            return

        from .viewer_presenter import ViewerContext

        ctx = ViewerContext(
            album_uuid=album.uuid,
            current_asset_uuid=asset_uuid,
            include_deleted=self._include_deleted(album),
        )
        try:
            ok = self.viewer_presenter.open(ctx)
        except Exception:
            _logger.exception("viewer open failed for asset=%s", asset_uuid)
            return
        if self.window_presenter is not None:
            self.window_presenter.show_detail()
        self._active_asset_uuid = str(asset_uuid)
        self._detail_visible = True
        self._refresh_tagbar()
        if not ok:
            _logger.info(
                "viewer opened with placeholder (asset=%s)", asset_uuid,
            )

    def _on_detail_navigate(self, asset_uuid: str) -> None:
        self._active_asset_uuid = str(asset_uuid)
        if self.viewer_presenter is not None:
            self.viewer_presenter.navigate(asset_uuid)
        self._refresh_tagbar()

    def _on_detail_close_requested(self) -> None:
        self._close_viewer()

    def _close_viewer(self) -> None:
        if self.window_presenter is not None:
            self.window_presenter.hide_detail()
        if self.viewer_presenter is not None:
            self.viewer_presenter.teardown()
        self._window.detail.refresh()
        self._refresh_grid()
        self._detail_visible = False
        # 关详情后由 grid 选中决定标签作用域
        self._active_asset_uuid = self._grid_selected_uuid()
        self._refresh_tagbar()

    def _grid_selected_uuid(self) -> str | None:
        """grid 当前选中的资产 uuid（没有 grid / 没选中时为 None）。"""
        if self.thumb_presenter is None:
            return None
        getter = getattr(self.thumb_presenter, "current_asset_uuid", None)
        if not callable(getter):
            return None
        try:
            value = getter()
        except Exception:
            _logger.exception("current_asset_uuid failed")
            return None
        return str(value) if value else None

    def _on_selection_changed(self, asset_uuid: str) -> None:
        if self._detail_visible:
            return
        self._active_asset_uuid = str(asset_uuid) if asset_uuid else None
        self._refresh_tagbar()

    def _current_asset_for_tags(self) -> str | None:
        """TagBar 的"作用于哪个资产"来源。

        优先级：
            1) 详情打开 → 详情当前资产；
            2) 详情关闭 → grid 当前选中。
        由 manager 集中维护，避免 tagbar 反向依赖 viewer 或 grid。
        """
        return self._active_asset_uuid

    # ---------------- tag ----------------

    def _on_tags_changed(self) -> None:
        """资产上的标签绑定变更（新建 / 添加 / 移除）。

        只需要刷 tagbar 自身；grid 的资产集合没变，不用刷 grid。
        如果详情打开且看的是同一资产，详情侧栏的标签 chip 也应刷新——
        那由 viewer_presenter 自己的 assets_changed 路径负责，manager 不重复。
        """
        self._refresh_tagbar()

    def _on_tag_metadata_changed(self) -> None:
        """标签元数据变更（create / rename / delete / restore）。

        影响两处：
            · tagbar 的 chip（display_name 变了要重绘）；
            · tagbar 的 layer tree（结构可能变了）。
        """
        if self.tagbar_presenter is not None:
            self.tagbar_presenter.refresh()
            self.tagbar_presenter.refresh_tree()

    def _on_tag_activated(self, tag_uuid: str) -> None:
        """用户点击 chip 或树节点 → 按标签筛选。

        TODO(P2): 切到 "tag 虚拟相册"。
            · 短期：只记录日志，等虚拟相册功能上线；
            · 中期：如果 album_presenter 支持虚拟 album，调
              ``self.album_presenter.select_virtual(f"tag:{tag_uuid}")``；
            · 长期：虚拟相册与物理相簿统一为 ``Album`` DTO，
              tag 只是 ``album_type == "tag"`` 的一种。
        """
        _logger.info(
            "tag activated: %s (tag-virtual album not implemented yet)",
            tag_uuid,
        )

    def _on_tag_error(self, message: str) -> None:
        """tag 业务错误 → 弹提示。"""
        toast = getattr(self._window, "show_toast", None)
        if callable(toast):
            toast(message)
        else:
            _logger.warning("tag error (no toast available): %s", message)

    # ---- scope helpers ----

    def _scope_of(self, album) -> str | None:
        """``view_service.get_scope(album.uuid)``；取不到时为 None。"""
        album_uuid = getattr(album, "uuid", None)
        if album_uuid is None:
            return None
        view_service = self._container.get("view_service", None)
        if view_service is None:
            return None
        try:
            return view_service.get_scope(album_uuid)
        except Exception:
            _logger.exception("resolve scope failed for %s", album)
            return None

    def _include_deleted(self, album) -> bool:
        """album 所属视图是否包含已删资产（Trash 视图）。"""
        return self._scope_of(album) == SCOPE_DELETED

    def _album_scope(self, album) -> tuple[list[str], bool]:
        """返回 ``(asset_uuids, include_deleted)``。

        - ``asset_uuids`` 是字符串 uuid 列表；
        - ``include_deleted`` 由 ``view_service.get_scope(album.uuid)``
          决定：``scope == "deleted"`` → True（Trash 视图）；其它 → False。
        """
        view_service = self._container.get("view_service", None)
        if view_service is None or album is None:
            return [], self._include_deleted(album)
        try:
            uuids = view_service.get_asset_uuids(album)
        except Exception:
            _logger.exception("resolve album scope failed for %s", album)
            return [], self._include_deleted(album)
        return [str(u) for u in uuids], self._include_deleted(album)

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
        self._refresh_tagbar()

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
