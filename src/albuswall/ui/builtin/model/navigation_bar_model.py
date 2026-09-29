#
"""横向缩略图导航栏的 ``QAbstractListModel`` 实现。

缩略图按需加载：只有当视图调用 ``data(idx, DecorationRole)`` 时
才向 ``ImageLoader`` 提交该索引的缩略图请求，避免一次性把几千张
缩略图塞进内存。

假定 ``ImageLoader`` 提供：
    - ``load_thumb(path: str, token: str) -> None``
    - ``cancel(token: str) -> None``
    - 信号 ``thumb_loaded(token: str, pixmap: QPixmap)``
    - 信号 ``load_failed(token: str, path: str)``
（与 ``load_full`` / ``full_loaded`` 对称。）
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence, cast
from uuid import uuid4

from PySide6.QtCore import QAbstractListModel, QModelIndex, QSize, Qt
from PySide6.QtGui import QPixmap

from ..widgets.image_loader import ImageLoader, get_loader


class ThumbnailModel(QAbstractListModel):
    """供 ``Detail.item_line_viewer`` 使用的缩略图模型。

    Roles:
        ``DecorationRole``  → 缩略图 ``QPixmap``（懒加载，未就绪时返回 ``None``）。
        ``UserRole``        → ``asset_id``（int）。
        ``ToolTipRole``     → 资产 id + 缩略图路径。

    懒加载策略：
        - ``data(DecorationRole)`` 被调用时若该行尚无 pixmap，则向
          ``ImageLoader.load_thumb`` 提交一次异步请求，并在
          ``_row_to_token`` 里登记，保证同一行不会重复提交；
        - 加载完成信号 ``thumb_loaded`` 到达时按 token 找到行号，
          缓存 pixmap 并 ``dataChanged`` 通知视图重绘该格；
        - 只缓存已经渲染过的行；未滚到视口中的行不会被请求。

    当前项高亮：
        本模型只记录 ``_current_index``，实际高亮由 ``DetailOverlay``
        的 QSS（``QListView::item:selected``）负责渲染。
    """

    # 缩略图单元格建议尺寸；具体由 delegate / QSS 决定
    THUMB_SIZE = QSize(80, 80)

    def __init__(
            self,
            loader: Optional[ImageLoader] = None,
            parent=None,
    ) -> None:
        super().__init__(parent=parent)

        # get_loader() 在类型上可能返回 Optional，这里 cast 一把
        self._loader: ImageLoader = (
            loader if loader is not None
            else cast(ImageLoader, get_loader())
        )

        # ---- 数据 --------------------------------------------------------
        self._asset_ids: list[int] = []
        # 与 _asset_ids 一一对应；缺项时为 ""（占位）
        self._thumb_paths: list[str] = []

        # ---- 缩略图缓存 / 在途请求 --------------------------------------
        self._pixmaps: dict[int, QPixmap] = {}  # row -> 已就绪 QPixmap
        self._row_to_token: dict[int, str] = {}  # row -> token
        self._token_to_row: dict[str, int] = {}  # token -> row

        self._current_index: int = -1
        self._connected: bool = False

        self._connect_loader()

    # ------------------------------------------------------------------ #
    # QAbstractListModel
    # ------------------------------------------------------------------ #
    # noinspection PyMethodOverriding
    def rowCount(  # noqa: N802
            self,
            parent: QModelIndex = QModelIndex(),
    ) -> int:
        if parent.isValid():
            return 0
        return len(self._asset_ids)

    # noinspection PyMethodOverriding
    def data(  # noqa: N802
            self,
            index: QModelIndex,
            role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if not index.isValid():
            return None
        row = index.row()
        if not 0 <= row < len(self._asset_ids):
            return None

        if role == Qt.ItemDataRole.UserRole:
            return self._asset_ids[row]

        if role == Qt.ItemDataRole.ToolTipRole:
            path = self._thumb_paths[row] if row < len(self._thumb_paths) else ""
            asset_id = self._asset_ids[row]
            return f"#{asset_id}\n{path}" if path else f"#{asset_id}"

        if role == Qt.ItemDataRole.DecorationRole:
            return self._pixmap_for_row(row)

        if role == Qt.ItemDataRole.SizeHintRole:
            return self.THUMB_SIZE

        return None

    # noinspection PyMethodOverriding
    def flags(self, index: QModelIndex) -> Qt.ItemFlag:  # noqa: N802
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        # 用 .value 走 int 位或再包回 ItemFlag：避开 PySide6 stub 里
        # ``ItemIsSelectable`` 被声明为 Literal 导致 ``__or__`` 报错的问题。
        return Qt.ItemFlag(
            Qt.ItemFlag.ItemIsEnabled.value | Qt.ItemFlag.ItemIsSelectable.value
        )

    # ------------------------------------------------------------------ #
    # 公共 API
    # ------------------------------------------------------------------ #
    def set_items(
            self,
            asset_ids: Sequence[int],
            thumb_paths: Mapping[int, str],
    ) -> None:
        """重建模型；``thumb_paths`` 里缺失的 asset_id 用空路径占位。"""
        # 旧数据里的在途请求失效，先取消
        self._cancel_inflight()

        self.beginResetModel()
        self._asset_ids = [int(a) for a in asset_ids]
        self._thumb_paths = [
            (thumb_paths.get(aid) or "") for aid in self._asset_ids
        ]
        self._pixmaps.clear()
        self._current_index = -1
        self.endResetModel()

    def set_current_index(self, index: int) -> None:
        """记录当前高亮的行号（实际高亮由 DetailOverlay 的 QSS 渲染）。"""
        if index == self._current_index:
            return
        old = self._current_index
        self._current_index = index

        for row in (old, index):
            if 0 <= row < len(self._asset_ids):
                idx = self.index(row, 0)
                self.dataChanged.emit(idx, idx)

    def current_index(self) -> int:
        return self._current_index

    def asset_id_at(self, row: int) -> Optional[int]:
        if 0 <= row < len(self._asset_ids):
            return self._asset_ids[row]
        return None

    def row_of(self, asset_id: int) -> int:
        """返回 ``asset_id`` 对应的行号；找不到时返回 -1。"""
        try:
            return self._asset_ids.index(int(asset_id))
        except ValueError:
            return -1

    def clear(self) -> None:
        self.set_items([], {})

    def shutdown(self) -> None:
        """断开与 ``ImageLoader`` 的连接；可在窗口销毁前调用。"""
        self._cancel_inflight()
        self._disconnect_loader()
        self._pixmaps.clear()

    # ------------------------------------------------------------------ #
    # 懒加载实现
    # ------------------------------------------------------------------ #
    def _pixmap_for_row(self, row: int) -> Optional[QPixmap]:
        cached = self._pixmaps.get(row)
        if cached is not None:
            return cached

        # 已经提交过请求 → 等信号，别重复提交
        if row in self._row_to_token:
            return None

        path = self._thumb_paths[row] if row < len(self._thumb_paths) else ""
        if not path:
            return None

        token = uuid4().hex
        self._row_to_token[row] = token
        self._token_to_row[token] = row

        # 命中缓存时 load_thumb 会同步 emit → _on_thumb_loaded 已填好
        self._loader.load_thumb(path, token=token, size=128)  # TODO 解决硬编码
        return self._pixmaps.get(row)

    # noinspection PyBroadException
    def _cancel_inflight(self) -> None:
        for token in self._row_to_token.values():
            try:
                self._loader.cancel(token=token)
            except Exception:  # noqa: BLE001  —— 取消是 best-effort
                pass
        self._row_to_token.clear()
        self._token_to_row.clear()

    # ------------------------------------------------------------------ #
    # ImageLoader 信号
    # ------------------------------------------------------------------ #
    def _connect_loader(self) -> None:
        if self._connected:
            return
        self._loader.thumb_loaded.connect(self._on_thumb_loaded)
        self._loader.load_failed.connect(self._on_load_failed)
        self._connected = True

    def _disconnect_loader(self) -> None:
        if not self._connected:
            return
        for sig, slot in (
                (self._loader.thumb_loaded, self._on_thumb_loaded),
                (self._loader.load_failed, self._on_load_failed),
        ):
            try:
                sig.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        self._connected = False

    def _on_thumb_loaded(self, token: str, pixmap: QPixmap) -> None:
        row = self._token_to_row.pop(token, None)
        if row is None:
            # 过期 / 已被 reset 掉的请求
            return
        self._row_to_token.pop(row, None)

        if not 0 <= row < len(self._asset_ids):
            return

        self._pixmaps[row] = pixmap
        idx = self.index(row, 0)
        self.dataChanged.emit(idx, idx, [Qt.ItemDataRole.DecorationRole])

    def _on_load_failed(self, token: str, path: str) -> None:  # noqa: ARG002
        row = self._token_to_row.pop(token, None)
        if row is None:
            return
        self._row_to_token.pop(row, None)
        # 不做重试；视图保持空占位
