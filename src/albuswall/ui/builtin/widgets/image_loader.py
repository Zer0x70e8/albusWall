# albuswall/ui/viewer/image_loader.py
"""异步图片加载：QThreadPool + QRunnable + 分级 PixmapCache。

线程模型
--------
- worker 线程只做 ``QImageReader -> QImage``，**绝不触碰** QPixmap / QCache；
- 所有 QPixmap 构造与缓存写入都被排队回 GUI 线程执行；
- 每个请求携带 ``token``，调用方切资产时 ``cancel(token)`` 即可丢弃过期结果。
"""

from __future__ import annotations

import threading
from typing import Optional

from PySide6.QtCore import (
    QObject,
    QRunnable,
    QSize,
    Qt,
    QThreadPool,
    Signal,
    Slot,
)
from PySide6.QtGui import QImage, QImageReader, QPixmap

from .virtual_scroll.lru_cache import LRUCache


def _pixmap_bytes(pm: QPixmap) -> int:
    return max(1, pm.width() * pm.height() * 4)


# --------------------------------------------------------------------------- #
# 分级 LRU 缓存
# --------------------------------------------------------------------------- #
class PixmapCache:
    def __init__(self, full_budget_mb: int = 256, thumb_budget_mb: int = 64) -> None:
        self._full = LRUCache[str, QPixmap](
            max_cost=full_budget_mb * 1024 * 1024,
            cost_of=_pixmap_bytes,
        )
        self._thumb = LRUCache[str, QPixmap](
            max_cost=thumb_budget_mb * 1024 * 1024,
            cost_of=_pixmap_bytes,
        )

    @staticmethod
    def _thumb_key(path: str, size: int) -> str:
        return f"{path}\x00{size}"

    def get_full(self, path: str) -> QPixmap | None:
        pm = self._full.get(path)
        return pm if pm is not None and not pm.isNull() else None

    def put_full(self, path: str, pixmap: QPixmap) -> None:
        if not pixmap.isNull():
            self._full[path] = QPixmap(pixmap)

    def get_thumb(self, path: str, size: int) -> QPixmap | None:
        pm = self._thumb.get(self._thumb_key(path, size))
        return pm if pm is not None and not pm.isNull() else None

    def put_thumb(self, path: str, size: int, pixmap: QPixmap) -> None:
        if not pixmap.isNull():
            self._thumb[self._thumb_key(path, size)] = QPixmap(pixmap)

    def clear(self) -> None:
        self._full.clear()
        self._thumb.clear()

    def remove_full(self, path: str) -> None:
        self._full.remove(path)


# --------------------------------------------------------------------------- #
# 加载任务
# --------------------------------------------------------------------------- #
class _ImageTask(QRunnable):
    KIND_FULL = "full"
    KIND_THUMB = "thumb"
    KIND_PREFETCH = "prefetch"

    __slots__ = ("_loader", "_path", "_token", "_kind", "_size", "_cancelled")

    def __init__(self, loader, path, token, kind, size=0):
        super().__init__()
        self._loader = loader
        self._path = path
        self._token = token
        self._kind = kind
        self._size = size
        self._cancelled = False
        self.setAutoDelete(True)

    @property
    def path(self):
        return self._path

    @property
    def token(self):
        return self._token

    @property
    def kind(self):
        return self._kind

    @property
    def cancelled(self):
        return self._cancelled

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        if self._cancelled:
            return
        reader = QImageReader(self._path)
        reader.setAutoTransform(True)
        if self._kind == self.KIND_THUMB and self._size > 0:
            src = reader.size()
            if src.isValid() and src.width() > 0 and src.height() > 0:
                if src.width() > self._size or src.height() > self._size:
                    reader.setScaledSize(src.scaled(
                        QSize(self._size, self._size),
                        Qt.AspectRatioMode.KeepAspectRatio,
                    ))
        image = reader.read()
        if self._cancelled:
            return
        if image.isNull():
            self._loader.emit_failed(self._token, self._path, self._kind)  # 已改为公开
        else:
            self._loader.emit_image(  # 已改为公开
                self._token, self._path, self._kind, self._size, image)


# --------------------------------------------------------------------------- #
# 加载器
# --------------------------------------------------------------------------- #
class ImageLoader(QObject):
    """基于 QThreadPool + QRunnable 的异步图片加载器。

    对外信号
    --------
    - ``full_loaded(token, QPixmap)``
    - ``thumb_loaded(token, QPixmap)``
    - ``load_failed(token, path)``

    对外方法
    --------
    - ``load_full(path, token)``
    - ``load_thumb(path, size, token)``
    - ``prefetch_full(path)``  只写缓存，不发信号
    - ``cancel(token)`` / ``cancel_all()``
    """

    full_loaded = Signal(str, QPixmap)  # token, pixmap
    thumb_loaded = Signal(str, QPixmap)  # token, pixmap
    load_failed = Signal(str, str)  # token, path

    # 内部通道：worker -> GUI。QImage 隐式共享，跨线程排队是安全的。
    _image_ready = Signal(str, str, str, int, object)
    _task_failed = Signal(str, str, str)

    DEFAULT_THREADS = 4

    def __init__(
            self,
            cache: Optional[PixmapCache] = None,
            parent: Optional[QObject] = None,
            thread_count: int = DEFAULT_THREADS,
    ) -> None:
        super().__init__(parent)

        self._cache = cache if cache is not None else PixmapCache()

        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(max(1, int(thread_count)))

        # token -> 进行中的任务；同 token 的新请求会取消旧任务
        self._tasks: dict[str, _ImageTask] = {}
        # 已经排队的 prefetch path，避免重复排队
        self._prefetching: set[str] = set()
        self._lock = threading.Lock()

        # 由 worker 线程 emit → 队列投递到 GUI 线程执行
        self._image_ready.connect(self._on_image_ready)
        self._task_failed.connect(self._on_task_failed)

    # ------------------------------------------------------------------ API
    # ---- 公开 emit 接口（供 _ImageTask 调用）----
    def emit_image(self, token, path, kind, size, image):
        self._image_ready.emit(token, path, kind, size, image)

    def emit_failed(self, token, path, kind):
        self._task_failed.emit(token, path, kind)

    @property
    def cache(self) -> PixmapCache:
        return self._cache

    def load_full(self, path: str, token: str) -> None:
        """异步加载原图。缓存命中时**同步** emit ``full_loaded``。"""
        if not path:
            self.load_failed.emit(token, "")
            return

        cached = self._cache.get_full(path)
        if cached is not None:
            self.full_loaded.emit(token, cached)
            return

        self._submit(_ImageTask(
            self, path, token, _ImageTask.KIND_FULL))

    def load_thumb(self, path: str, size: int, token: str) -> None:
        """异步加载缩略图。缓存命中时**同步** emit ``thumb_loaded``。"""
        if not path:
            self.load_failed.emit(token, "")
            return

        cached = self._cache.get_thumb(path, size)
        if cached is not None:
            self.thumb_loaded.emit(token, cached)
            return

        self._submit(_ImageTask(
            self, path, token, _ImageTask.KIND_THUMB, size))

    def prefetch_full(self, path: str) -> None:
        """后台预热原图缓存，不发任何信号。"""
        if not path:
            return
        if self._cache.get_full(path) is not None:
            return

        token = f"__prefetch__:{path}"
        with self._lock:
            if path in self._prefetching:
                return
            self._prefetching.add(path)

        task = _ImageTask(self, path, token, _ImageTask.KIND_PREFETCH)
        with self._lock:
            self._tasks[token] = task
        self._pool.start(task)

    def cancel(self, token: str) -> None:
        """取消指定 token 的在途任务（best-effort）。"""
        with self._lock:
            task = self._tasks.pop(token, None)
        if task is not None:
            task.cancel()

    def cancel_all(self) -> None:
        with self._lock:
            tasks = list(self._tasks.values())
            self._tasks.clear()
        for task in tasks:
            task.cancel()
        # 清掉还没被 worker 领走的排队 runnable
        self._pool.clear()

    def shutdown(self, wait_ms: int = 3000) -> None:
        """退出前调用：取消全部在途任务并等待 worker 回收。"""
        self.cancel_all()
        self._pool.waitForDone(wait_ms)

    # -------------------------------------------------------------- 内部

    def _submit(self, task: _ImageTask) -> None:
        # 同一 token 的旧任务作废（例如同一位置快速触发多次）
        self.cancel(task.token)
        with self._lock:
            self._tasks[task.token] = task
        self._pool.start(task)

    # ---- worker 线程侧：只发信号 ------------------------------------------

    def _emit_image(
            self,
            token: str,
            path: str,
            kind: str,
            size: int,
            image: QImage,
    ) -> None:
        self._image_ready.emit(token, path, kind, size, image)

    def _emit_failed(self, token: str, path: str, kind: str) -> None:
        self._task_failed.emit(token, path, kind)

    # ---- GUI 线程侧 -------------------------------------------------------

    @Slot(str, str, str, int, object)
    def _on_image_ready(
            self,
            token: str,
            path: str,
            kind: str,
            size: int,
            image: QImage,
    ) -> None:
        with self._lock:
            task = self._tasks.pop(token, None)

        is_prefetch = kind == _ImageTask.KIND_PREFETCH
        if is_prefetch:
            self._finish_prefetch(path)

        if task is None or task.cancelled:
            # 已被取消 / 过期结果，直接丢弃
            return

        pixmap = QPixmap.fromImage(image)
        if pixmap.isNull():
            if not is_prefetch:
                self.load_failed.emit(token, path)
            return

        if kind == _ImageTask.KIND_THUMB:
            self._cache.put_thumb(path, size, pixmap)
            self.thumb_loaded.emit(token, pixmap)
        else:
            # FULL 与 PREFETCH 共用原图缓存
            self._cache.put_full(path, pixmap)
            if kind == _ImageTask.KIND_FULL:
                self.full_loaded.emit(token, pixmap)

    @Slot(str, str, str)
    def _on_task_failed(self, token: str, path: str, kind: str) -> None:
        if kind == _ImageTask.KIND_PREFETCH:
            self._finish_prefetch(path)
            # prefetch 失败静默
            with self._lock:
                self._tasks.pop(token, None)
            return

        with self._lock:
            task = self._tasks.pop(token, None)
        if task is None or task.cancelled:
            return
        self.load_failed.emit(token, path)

    def _finish_prefetch(self, path: str) -> None:
        with self._lock:
            self._prefetching.discard(path)


_loader: ImageLoader | None = None
_cache: PixmapCache | None = None


def get_loader() -> ImageLoader | None:
    global _loader, _cache
    if _loader is None:
        _cache = PixmapCache(full_budget_mb=256, thumb_budget_mb=64)
        _loader = ImageLoader(cache=_cache, thread_count=4)
    return _loader


def get_cache() -> PixmapCache:
    global _cache
    get_loader()
    assert _cache is not None
    return _cache
