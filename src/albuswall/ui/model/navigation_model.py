#
"""详情页底部导航条的缩略图模型。

旧版把「懒加载 + 缩略图生成 + 服务信号」全塞在模型里（450 行），和
``ThumbnailService`` 硬绑定；新版把职责切干净：

* **模型只认数据**：标识 + 缩略图，外加一个可选的 ``lazy_provider`` 回调；
* **加载策略归调用方**：想让谁去解码（``ImageLoader``、线程池、本地缓存）
  由调用方决定，加载完成后 ``set_pixmap(row, pixmap)`` 回填即可；
* **倒计时 / 收藏这类格子状态不在这里**：导航条只负责「哪一张、缩略图」。

``lazy_provider`` 的契约很窄：``provider(identifier) -> QPixmap | None``。
返回 ``None`` 表示「还没好」，之后由调用方回填；不要在 provider 里阻塞。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QModelIndex, QSize, Qt
from PySide6.QtGui import QIcon, QPixmap

from .base import EntryListModel
from .roles import NavigationRole

__all__ = ["NavigationEntry", "NavigationModel", "LazyPixmapProvider"]

#: 懒加载回调：标识 → 缩略图（尚未就绪时返回 ``None``）。
LazyPixmapProvider = Callable[[str], "QPixmap | None"]

#: 把 ``QIcon`` 转成 ``QPixmap`` 时用的默认边长。
DEFAULT_ICON_SIZE = QSize(80, 80)


@dataclass(slots=True)
class NavigationEntry:
    """一条导航缩略图。"""

    id: str
    title: str = ""
    pixmap: QPixmap | None = None
    icon: QIcon | None = None


class NavigationModel(EntryListModel[NavigationEntry]):
    """导航条缩略图列表模型。

    Roles::

        DisplayRole            → 标题（通常是文件名 / 序号，可留空）
        DecorationRole         → 缩略图
        NavigationRole.Id      → 标识（只用于回传信号）
        NavigationRole.Pixmap  → 缩略图

    :param icon_size: 传入 ``QIcon`` 时用它转成 ``QPixmap`` 的边长。
    :param lazy_provider: 可选懒加载回调，见模块文档。
    """

    ROLE_FIELDS = {
        NavigationRole.Id: "id",
        NavigationRole.Pixmap: "pixmap",
    }
    DISPLAY_FIELD = "title"
    TOOLTIP_FIELD = "title"
    DECORATION_FIELD = "pixmap"

    def __init__(
            self,
            entries: Iterable[NavigationEntry] | None = None,
            *,
            icon_size: QSize | None = None,
            lazy_provider: LazyPixmapProvider | None = None,
            parent: Any = None,
    ) -> None:
        super().__init__(entries, parent)
        self._icon_size = QSize(icon_size or DEFAULT_ICON_SIZE)
        self._lazy_provider = lazy_provider
        #: 已向 provider 问过的行，避免每帧重复请求
        self._asked: set[int] = set()

    # ------------------------------------------------------------------ #
    # 数据：缩略图按需加载
    # ------------------------------------------------------------------ #
    def data(
            self,
            index: QModelIndex,
            role: int = int(Qt.ItemDataRole.DisplayRole),
    ) -> Any:
        value = super().data(index, role)
        if value is not None or not index.isValid():
            return value
        if int(role) not in (
                int(NavigationRole.Pixmap),
                int(Qt.ItemDataRole.DecorationRole)):
            return None
        return self._lazy_pixmap(index.row())

    def _lazy_pixmap(self, row: int) -> QPixmap | None:
        """向 provider 要一次缩略图；就绪时顺手缓存进条目。"""
        entry = self.entry_at(row)
        if entry is None or entry.pixmap is not None:
            return entry.pixmap if entry is not None else None
        if self._lazy_provider is None or row in self._asked:
            return None
        self._asked.add(row)
        pixmap = self._lazy_provider(entry.id)
        if pixmap is not None and not pixmap.isNull():
            entry.pixmap = pixmap
        return entry.pixmap

    # ------------------------------------------------------------------ #
    # 装载
    # ------------------------------------------------------------------ #
    def set_items(
            self,
            items: Iterable[tuple[str, QIcon | QPixmap | None]],
            *,
            pixmaps: dict[str, QPixmap] | None = None,
    ) -> None:
        """用 ``(标识, 图标)`` 序列重建导航条。

        :param items: 图标可以是 ``QIcon`` / ``QPixmap`` / ``None``（占位）。
        :param pixmaps: 可选的「标识 → 缩略图」映射，命中时优先于图标。
        """
        mapping = pixmaps or {}
        entries: list[NavigationEntry] = []
        for nav_id, icon in items:
            key = str(nav_id)
            pixmap = mapping.get(key)
            if pixmap is None:
                pixmap = self._to_pixmap(icon)
            entries.append(NavigationEntry(id=key, pixmap=pixmap))
        self.set_entries(entries)

    def set_pixmap(self, row: int, pixmap: QPixmap | None) -> None:
        """回填某行缩略图（异步加载完成的出口）。"""
        entry = self.entry_at(row)
        if entry is None or entry.pixmap is pixmap:
            return
        entry.pixmap = pixmap
        self.touch_row(
            row, (NavigationRole.Pixmap, Qt.ItemDataRole.DecorationRole))

    def set_pixmap_for(self, nav_id: Any, pixmap: QPixmap | None) -> bool:
        """按标识回填缩略图；找不到返回 ``False``。"""
        row = self.row_of(nav_id)
        if row < 0:
            return False
        self.set_pixmap(row, pixmap)
        return True

    # ------------------------------------------------------------------ #
    # 查询 / 当前项
    # ------------------------------------------------------------------ #
    def id_at(self, row: int) -> str:
        entry = self.entry_at(row)
        return entry.id if entry is not None else ""

    def row_of(self, nav_id: Any) -> int:
        """返回标识对应的行号；找不到时返回 ``-1``。"""
        target = "" if nav_id is None else str(nav_id)
        for row, entry in enumerate(self._entries):
            if entry.id == target:
                return row
        return -1

    def index_of(self, nav_id: Any) -> QModelIndex:
        row = self.row_of(nav_id)
        return self.index(row, 0) if row >= 0 else QModelIndex()

    def pixmap_at(self, row: int) -> QPixmap | None:
        entry = self.entry_at(row)
        return entry.pixmap if entry is not None else None

    # ------------------------------------------------------------------ #
    # 懒加载配置
    # ------------------------------------------------------------------ #
    def set_lazy_provider(self, provider: LazyPixmapProvider | None) -> None:
        """替换懒加载回调；换人后清空「已问过」账本，允许重新请求。"""
        self._lazy_provider = provider
        self._asked.clear()

    def lazy_provider(self) -> LazyPixmapProvider | None:
        return self._lazy_provider

    def set_icon_size(self, size: QSize) -> None:
        """设置 ``QIcon`` → ``QPixmap`` 的转换边长。"""
        self._icon_size = QSize(size)

    def retry_lazy(self, row: int | None = None) -> None:
        """允许某行（或全部行）重新向 provider 请求缩略图。"""
        if row is None:
            self._asked.clear()
        else:
            self._asked.discard(int(row))

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _to_pixmap(self, icon: QIcon | QPixmap | None) -> QPixmap | None:
        if isinstance(icon, QPixmap):
            return None if icon.isNull() else icon
        if isinstance(icon, QIcon) and not icon.isNull():
            pixmap = icon.pixmap(self._icon_size)
            return None if pixmap.isNull() else pixmap
        return None
