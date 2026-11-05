#
"""``MainPresenter`` —— presenter 树的根：编排子 presenter + 接线 + 主题。

职责
====

1. **编排**：按 :func:`~albuswall.ui.presenter.base.register_presenter` 的登记
   顺序，用依赖容器构造子 presenter（``factory(container, parent)``），并让
   ``start()`` / ``stop()`` 级联到整棵树；
2. **接线**：子 presenter 彼此不 import、不互相持有，跨 presenter 的流程
   全部在这里用 Qt 信号连起来（见 :meth:`MainPresenter._wire`）：
   相册切换 → 内容网格 / 搜索作用域；网格激活 → 详情页；当前资产
   （网格光标 / 详情页翻页）→ 标签栏；标签点击 → 搜索过滤；标签结构编辑
   意图 → 标签编辑器 …… 错误统一转成窗口 toast；
3. **主题**：读 ``[ui]`` 配置，把内置主题的 QSS 套到窗口上；``set_theme()``
   切换主题并回写配置（配置器会按 ``[main]`` 文件的 autosave 策略落盘）；
4. **语言**：窗口收到 ``LanguageChange`` 后让每个 presenter 重算自己持有的
   文案 / 数据（控件由 Qt 自己刷新，presenter 收不到那条事件）。

子 presenter 一览
=================

===========================  =====================================
``window``（WindowPresenter） 浮层焦点调度、窗口级 toast
``album``（AlbumPresenter）   相册列表 / 当前相册 / 标题栏
``content``（ContentPresenter） 主内容网格（按需缩略图 + 标签过滤）
``viewer``（ViewerPresenter） 详情页（大图 / 导航 / 收藏 / 软删恢复）
``tagbar``（TagBarPresenter） 当前资产的标签 chip + 标签树
``tag_editor``（TagEditorPresenter） 标签新建 / 重命名 / 删除
``source``（SourcePresenter） 导入源页（卡片 + 表单）
``search``（SearchPresenter） 浮动搜索栏（按标签过滤）
===========================  =====================================

构造 / 启动顺序 = 各 presenter 模块的 **import 顺序**（每个模块在 import 期
用 ``register_presenter`` 自登记，见本模块顶部 import 块）。启动顺序不影响
正确性：接线发生在所有子节点构造完成之后，子节点启动期发出的信号也已被
连上，不会漏接。

和界面 boot 层的关系
====================

:meth:`MainPresenter.bootstrap` 是**唯一**入口 —— boot 层
(:func:`albuswall.ui.bootstrap.setup_ui`) 建好主窗口后调用它：

.. code-block:: python

    self.presenter = MainPresenter.bootstrap(
        container, app=app, window=window)

``bootstrap`` 做四件事（全部幂等）：

1. 从容器取 ``presenter``（没有就新建一个）—— 插件可以在
   ``init`` 阶段用 ``container.get("presenter")`` 直接拿到它；
2. 把 ``presenter`` / ``main_presenter`` 两个别名登记进容器；
3. 绑定 QApplication 与主窗口，并挂上 ``aboutToQuit`` → ``stop()``；
4. ``start()`` 整棵树。

依赖注入的边界在这里收口：子 presenter **不** import 主 presenter 的类型，
只通过 ``factory(container, parent)`` 这层契约被构造；本模块 import 具体
presenter 类**仅**用于接线时的类型定位（``_child_of``）与 ``isinstance`` 判断，
不参与构造 —— 构造永远走登记表。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QEvent, QObject, Signal
from PySide6.QtWidgets import QApplication, QWidget

from albuswall.configue.errors import ConfigError

from .album_presenter import AlbumPresenter
from .base import Presenter, registered_presenters
from .content_presenter import ContentPresenter
from .search_presenter import SearchPresenter
from .source_presenter import SourcePresenter
from .tag_editor_presenter import TagEditorPresenter
from .tagbar_presenter import TagBarPresenter
from .theme import Theme, ThemeConfig, resolve_theme
from .viewer_presenter import ViewerContext, ViewerPresenter
from .window_presenter import WindowPresenter

if TYPE_CHECKING:
    from albuswall.core.container import Container

__all__ = ["MainPresenter"]

logger = logging.getLogger(__package__)


class MainPresenter(Presenter):
    """presenter 树的根。

    * ``bootstrap()`` 之前：只是一个拿着容器的空壳；
    * ``bootstrap()`` 之后：``app`` / ``window`` 已绑定、主题已套上、子
      presenter 已启动。
    """

    name = "main"

    #: 容器里的主键 / 别名（插件通过它们拿主 presenter）。
    CONTAINER_KEY = "presenter"
    CONTAINER_ALIASES = ("presenter", "main_presenter")

    #: 主题被重新应用（载荷：``Theme``）。
    theme_changed = Signal(object)

    def __init__(
            self,
            container: Container,
            *,
            parent: Presenter | None = None,
            app: QApplication | None = None,
            window: QWidget | None = None,
    ) -> None:
        super().__init__(container, parent=parent)
        self._app: QApplication | None = None
        self._window: QWidget | None = None
        self._theme: Theme | None = None
        self._shutdown_hooked = False
        self._wired = False
        self.attach(app, window)

    # ------------------------------------------------------------------ #
    # 依赖注入入口
    # ------------------------------------------------------------------ #
    @classmethod
    def bootstrap(
            cls,
            container: Container,
            *,
            app: QApplication | None = None,
            window: QWidget | None = None,
    ) -> "MainPresenter":
        """UI 应用类的入口：注册 → 绑定 → 启动，返回主 presenter。

        Args:
            container: 依赖容器（``Application`` 持有同一个对象）。
            app: 宿主 ``QApplication``；``None`` 时主题只会在有
                ``QApplication.instance()`` 时应用。
            window: 主窗口（视图层对象）。主题默认套到整个应用上，这里只
                作为「没有 QApplication 时」的兜底目标，并供子 presenter 取用。

        Raises:
            TypeError: 容器里的 ``presenter`` 不是 :class:`MainPresenter`
                （别的插件塞了不兼容的对象）。
        """
        presenter = container.get(cls.CONTAINER_KEY)
        if presenter is None:
            presenter = cls(container)
            logger.debug("presenter: main presenter created")
        elif not isinstance(presenter, cls):
            raise TypeError(
                f"container[{cls.CONTAINER_KEY!r}] must be a "
                f"{cls.__name__}, got {type(presenter).__name__}")

        for alias in cls.CONTAINER_ALIASES:
            container.set(alias, presenter)

        presenter.attach(app, window)
        presenter.start()
        return presenter

    # ------------------------------------------------------------------ #
    # 宿主绑定
    # ------------------------------------------------------------------ #
    def attach(
            self,
            app: QApplication | None = None,
            window: QWidget | None = None,
    ) -> None:
        """绑定宿主应用 / 主窗口（重复调用只覆盖非 ``None`` 的那几个）。"""
        if app is not None:
            self._app = app
        if window is not None:
            self._window = window
            logger.debug(
                "presenter: window bound (%s)", type(window).__name__)
        self._connect_shutdown()

    @property
    def app(self) -> QApplication | None:
        """绑定过的宿主应用；没有时退回 ``QApplication.instance()``。"""
        if self._app is not None:
            return self._app
        instance = QApplication.instance()
        return instance if isinstance(instance, QApplication) else None

    @property
    def window(self) -> QWidget | None:
        """主窗口（视图层对象，presenter 只往里写，不读业务数据）。"""
        return self._window

    def _connect_shutdown(self) -> None:
        """``aboutToQuit`` → ``stop()``：事件循环退出前收起整棵树。

        幂等；没有 Qt 应用（单测直接构造）时静默跳过。
        """
        app = self._app
        if app is None or self._shutdown_hooked:
            return
        try:
            app.aboutToQuit.connect(self.stop)
        except (AttributeError, RuntimeError) as exc:
            logger.debug("presenter: cannot hook aboutToQuit (%s)", exc)
            return
        self._shutdown_hooked = True
        logger.debug("presenter: shutdown hooked on aboutToQuit")

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def on_start(self) -> None:
        """套主题 → 构造并接线子 presenter → 挂语言监听。

        子 presenter 的 ``on_start`` 在 ``on_start`` 返回后由基类按登记顺序
        级联调用，因此这里接线时拿到的子节点还没启动：**信号先连上**，
        子节点启动期自己发出的信号（例如相册首刷）不会漏接。
        """
        self.apply_theme()
        self._install_children()
        self._wire()
        self._install_language_filter()

    def on_stop(self) -> None:
        """退出路径：摘掉语言监听；样式表保持不动（应用即将销毁）。"""
        self._remove_language_filter()

    def _install_children(self) -> None:
        """按登记顺序构造子 presenter（失败一个不影响其它）。"""
        for factory in registered_presenters():
            try:
                child = factory(self._container, self)
                if child is None:
                    continue
                # 工厂里已经 ``parent=`` 挂上的情况：同一对象是 no-op。
                self.add_child(child)
            except Exception:  # noqa: BLE001 - 单个 presenter 坏了不拖垮整棵 UI
                logger.exception(
                    "presenter: failed to build child via %r", factory)

    # ------------------------------------------------------------------ #
    # 子节点定位
    # ------------------------------------------------------------------ #
    def _child_of(self, cls: type) -> Any | None:
        """按类型取直接子节点（找不到返回 ``None``）。"""
        for child in self._children.values():
            if isinstance(child, cls):
                return child
        return None

    # ------------------------------------------------------------------ #
    # 接线（跨 presenter 的唯一落点）
    # ------------------------------------------------------------------ #
    def _wire(self) -> None:
        """把子 presenter 之间的流程接起来（幂等）。"""
        if self._wired:
            return
        self._wired = True

        album = self._child_of(AlbumPresenter)
        content = self._child_of(ContentPresenter)
        viewer = self._child_of(ViewerPresenter)
        tagbar = self._child_of(TagBarPresenter)
        tag_editor = self._child_of(TagEditorPresenter)
        source = self._child_of(SourcePresenter)
        search = self._child_of(SearchPresenter)
        window_p = self._child_of(WindowPresenter)

        # ---- 错误 / 提示 → 窗口 toast ----
        for presenter in self._children.values():
            for signal_name in ("error", "notice"):
                signal = getattr(presenter, signal_name, None)
                if signal is not None:
                    signal.connect(self._on_notice)

        # ---- 相册 → 内容网格 / 搜索作用域 ----
        if album is not None and content is not None:
            album.album_changed.connect(content.set_album)
            album.album_list_changed.connect(content.refresh)
        if album is not None and search is not None:
            album.album_changed.connect(search.set_album)
        if album is not None:
            album.album_changed.connect(self._on_album_changed)

        # ---- 内容网格 → 详情页 / 标签作用对象 ----
        if content is not None and viewer is not None:
            content.asset_activated.connect(
                lambda asset_uuid: self._open_detail(asset_uuid))
            # 空格 = 快速预览（HUD 收起），回车 / 点击 = 完整详情：两条出口
            # 都进同一页，但预览模式下再按空格就退出预览。
            content.asset_previewed.connect(
                lambda asset_uuid: self._open_detail(asset_uuid, preview=True))
        if content is not None and tagbar is not None:
            content.selection_changed.connect(self._on_selection_changed)
        if content is not None and album is not None:
            content.assets_changed.connect(album.reload_covers)

        # ---- 详情页写操作 → 网格 / 相册封面 / 标签刷新 ----
        if viewer is not None and content is not None:
            viewer.assets_changed.connect(self._on_assets_changed)

        # ---- 标签：chip 点击过滤、结构编辑 ----
        if tagbar is not None and search is not None:
            tagbar.tag_activated.connect(search.apply_filter)
        # 详情页翻页 / 打开 = 「当前资产」变了：标签栏必须跟着换对象，
        # 否则详情里左右翻页后标签栏还停在上一个资产上。
        if viewer is not None and tagbar is not None:
            viewer.asset_changed.connect(tagbar.set_current_asset)
        if tagbar is not None and tag_editor is not None:
            tagbar.tag_create_requested.connect(tag_editor.create)
            tagbar.tag_rename_requested.connect(tag_editor.rename)
            tagbar.tag_delete_requested.connect(tag_editor.delete)
        if tag_editor is not None and tagbar is not None:
            tag_editor.changed.connect(tagbar.refresh_tree)
        if search is not None and content is not None:
            search.filter_requested.connect(content.set_tag_filter)

        # ---- 主题变化 → 窗口 presenter 重新装图标 ----
        if window_p is not None:
            self.theme_changed.connect(window_p.apply_theme)

        # ---- 详情页关闭 → 收起浮层 + 标签作用对象回到网格光标 ----
        if viewer is not None and window_p is not None:
            viewer.closed.connect(window_p.hide_detail)
            viewer.closed.connect(viewer.close)
        if viewer is not None:
            viewer.closed.connect(self._on_detail_closed)
        if source is not None:
            source.sources_changed.connect(self._on_sources_changed)

        logger.debug("presenter: wiring complete (%d children)",
                     len(self._children))

    # ------------------------------------------------------------------ #
    # 接线槽
    # ------------------------------------------------------------------ #
    def _open_detail(self, asset_uuid: str, *, preview: bool = False) -> None:
        """网格激活 / 外部请求 → 打开详情页。

        ``preview=True`` 是内容网格的空格「快速预览」：同一页、收起 HUD；
        预览里的空格（退出预览）/ 回车（升级成完整详情）由
        :class:`~albuswall.ui.window.detail.Detail` 自己的 ``eventFilter``
        处理，这里只负责进入。
        """
        viewer = self._child_of(ViewerPresenter)
        album = self._child_of(AlbumPresenter)
        window_p = self._child_of(WindowPresenter)
        if viewer is None or album is None or album.current_uuid is None:
            return
        # ``viewer.open`` 自己会广播 ``asset_changed``（打开 / 翻页同一条路），
        # 标签栏由那里的接线跟随，这里不再单独 set_current_asset。
        viewer.open(ViewerContext(
            album_uuid=album.current_uuid,
            current_asset_uuid=str(asset_uuid),
        ))
        if window_p is not None:
            window_p.show_detail()
        if preview:
            detail = getattr(self._window, "detail", None)
            if detail is not None:
                detail.set_preview_mode(True)

    def _on_album_changed(self, _album: Any) -> None:
        """相册切换后标签作用对象跟到新相册的第一个光标资产。"""
        tagbar = self._child_of(TagBarPresenter)
        content = self._child_of(ContentPresenter)
        if tagbar is not None and content is not None:
            tagbar.set_current_asset(content.current_asset_uuid())

    def _on_detail_closed(self) -> None:
        """详情页关闭：标签作用对象回到网格光标（可能为空）。"""
        tagbar = self._child_of(TagBarPresenter)
        content = self._child_of(ContentPresenter)
        if tagbar is not None and content is not None:
            tagbar.set_current_asset(content.current_asset_uuid())

    def _on_selection_changed(self, asset_uuid: str) -> None:
        tagbar = self._child_of(TagBarPresenter)
        if tagbar is not None:
            tagbar.set_current_asset(asset_uuid or None)

    def _on_assets_changed(self, asset_uuids: Any) -> None:
        """资产被写操作改过：内容网格 / 相册封面 / 标签都要重新对齐。"""
        content = self._child_of(ContentPresenter)
        album = self._child_of(AlbumPresenter)
        tagbar = self._child_of(TagBarPresenter)
        if content is not None:
            content.refresh()
        if album is not None:
            album.reload_covers()
        if tagbar is not None and content is not None:
            tagbar.set_current_asset(content.current_asset_uuid())
            tagbar.refresh()
        logger.debug("presenter: assets changed %s", asset_uuids)

    def _on_sources_changed(self) -> None:
        """导入源变化：目前只记日志（网格数据由导入 worker 自己驱动）。"""
        logger.debug("presenter: import sources changed")

    def _on_notice(self, message: str) -> None:
        """任何 presenter 的 ``error`` / ``notice`` → 窗口 toast。"""
        window_p = self._child_of(WindowPresenter)
        if window_p is not None:
            window_p.toast(str(message))
            return
        logger.warning("presenter error: %s", message)

    # ------------------------------------------------------------------ #
    # 语言
    # ------------------------------------------------------------------ #
    def _install_language_filter(self) -> None:
        window = self._window
        if window is None:
            return
        window.installEventFilter(self)

    def _remove_language_filter(self) -> None:
        window = self._window
        if window is None:
            return
        try:
            window.removeEventFilter(self)
        except (RuntimeError, TypeError):
            pass

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        """窗口收到 ``LanguageChange`` → 让每个 presenter 重算文案。"""
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        return super().eventFilter(watched, event)

    def retranslate(self) -> None:
        """逐个调用子 presenter 的 ``retranslate()``（单个失败不影响其它）。"""
        for child in self._children.values():
            try:
                child.retranslate()
            except Exception:  # noqa: BLE001 - 一个 presenter 的文案失败不该崩
                logger.exception(
                    "presenter: %r.retranslate failed", child.name)

    # ------------------------------------------------------------------ #
    # 主题
    # ------------------------------------------------------------------ #
    @property
    def theme(self) -> Theme | None:
        """当前生效的主题；``start()`` 之前为 ``None``。"""
        return self._theme

    @property
    def theme_name(self) -> str:
        """当前生效的主题名；还没套过主题时返回 ``""``。"""
        return self._theme.name if self._theme is not None else ""

    def apply_theme(self, name: str | None = None) -> Theme:
        """解析并应用主题，返回解析结果。

        Args:
            name: 主题名；``None`` → 按 ``[ui]`` 配置解析：``[ui] style_sheet``
                展开出的用户主题文件优先，其次随包 ``resources/themes/<name>/``
                （路径由配置器的 ``${...}`` 组合，见
                :mod:`albuswall.ui.presenter.theme`）。

        不写回配置：启动时套主题是「读」，不是「用户改」。要切换并记住，
        用 :meth:`set_theme`。
        """
        theme = resolve_theme(name)
        self._theme = theme
        self._apply_style_sheet(theme.style_sheet)
        logger.debug(
            "presenter: theme %r applied (source=%s, qss=%s)",
            theme.name, theme.source,
            theme.qss_path if theme.qss_path is not None else "-")
        self.theme_changed.emit(theme)
        return theme

    def set_theme(self, name: str) -> Theme:
        """切换主题并回写 ``[ui] theme``（配置器负责落盘）。

        只有**真正生效**的主题名才回写：请求了不存在的主题时
        :func:`resolve_theme` 会回退到 ``default``，此时保持用户配置原样，
        只记 WARNING —— 静默把用户的错别字改写成 ``default`` 会让人
        找不到自己写的那一行。
        """
        requested = str(name or "").strip()
        theme = self.apply_theme(requested or None)
        if requested and theme.name == requested and theme.source != "empty":
            self._persist_theme(theme.name)
        elif requested:
            logger.warning(
                "presenter: theme %r did not take effect (resolved to %r, "
                "source=%s); config left untouched",
                requested, theme.name, theme.source)
        return theme

    def reload_theme(self) -> Theme:
        """重新按配置解析主题（设置页改完 ``[ui]`` 后调用）。"""
        return self.apply_theme()

    def _apply_style_sheet(self, style_sheet: str) -> None:
        """把 QSS 套到应用（没有应用时退到窗口），都没有则只记录。"""
        target = self.app
        if target is not None:
            target.setStyleSheet(style_sheet)
            return
        if self._window is not None:
            self._window.setStyleSheet(style_sheet)
            return
        logger.debug(
            "presenter: no QApplication/window bound; theme not applied")

    def _persist_theme(self, name: str) -> None:
        """把主题名写进 ``[ui] theme``（配置器的 autosave 负责落盘）。"""
        try:
            ThemeConfig().theme = name
        except (ConfigError, AttributeError, ValueError) as exc:
            logger.warning(
                "presenter: cannot persist theme %r (%s: %s)",
                name, type(exc).__name__, exc)
            return
        logger.debug("presenter: theme %r persisted to [ui] theme", name)

