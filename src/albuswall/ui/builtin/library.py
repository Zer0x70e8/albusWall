"""资产库操作（Qt 侧薄封装）：移动 / 删除 / 剪贴板 / 目标相册 / 撤销。

设计约定
--------
* 快捷键动作层（``actions/*``）只做「读上下文 → 调本模块 → 返回 bool」；
* 本模块是唯一知道「移动到哪、撤销什么」的地方，持有运行期状态；
* **写操作走既有的 repo / service**，不新开 SQL：
    - 软删 / 恢复 / 相册成员：``asset_repo``（AssetRepository）
    - 清空回收站：``trash_service``（TrashService）
    - 读：``view_service``（ViewService）
* 尚未落地的能力留空接口 + ``TODO``，由调用方 toast 提示（见 :func:`todo`）。

回收站规则
----------
* 回收站是系统相册（``view_service.get_scope(...) == "deleted"``）：
  不可重命名 / 不可删除 / 不可作为快捷槽位的目标；
* 「移动到回收站」= ``soft_delete``，撤销 = ``restore``；
* 在回收站里再按 Delete 只提示，不重复改语义。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from albuswall.log import getLogger
from albuswall.services.view import SCOPE_DELETED

__all__ = [
    "QUICK_SLOT_COUNT", "LibraryState", "can_be_slot_target", "copy_assets",
    "copy_path", "cut_assets", "delete_album", "empty_trash", "export_assets",
    "is_system_album", "is_trash_album", "move_to_album", "move_to_slot",
    "move_to_target", "move_to_trash", "new_album", "paste_assets",
    "pick_target_album", "redo", "rename_album", "rename_asset",
    "record_undo", "reveal_in_folder", "set_flag", "set_quick_slot", "set_rating",
    "set_target_album", "show_advanced_filter", "state", "todo", "undo",
]

_logger = getLogger(__name__)

#: 快捷槽位数量（``Shift+1..9``）。
QUICK_SLOT_COUNT = 9


# --------------------------------------------------------------------------- #
# 运行期状态
# --------------------------------------------------------------------------- #
@dataclass
class UndoEntry:
    """一条可撤销记录。

    kind:
        ``trash``       —— 软删（撤销 = 恢复）
        ``restore``     —— 恢复（撤销 = 重新软删）
        ``move``        —— 移入相册（撤销 = 从目标移除并放回来源）
        ``album_add``   —— 加入相册（撤销 = 从相册移除）
    """

    kind: str
    uuids: tuple[str, ...]
    album: str | None = None
    source_album: str | None = None


@dataclass
class LibraryState:
    """库操作的运行期状态（单进程单实例；不做持久化）。

    TODO: 目标相册 / 快捷槽位目前只在内存里，重启即丢；后续落
    ``preference.ini`` 的 ``[library]`` 段（target_album / slot_1..slot_9），
    并给设置页加对应的下拉选择器。
    """

    clipboard: list[str] = field(default_factory=list)
    clipboard_cut: bool = False
    #: 剪贴板内容的来源相册（``Ctrl+X`` 粘贴时从这里移除）
    clipboard_source: str | None = None
    #: ``M`` 的目标相册 uuid（空 = 未设置，按当前相册处理）
    target_album: str | None = None
    #: ``Shift+1..9`` 的快捷槽位 → 相册 uuid
    slots: dict[int, str] = field(default_factory=dict)
    undo_stack: list[UndoEntry] = field(default_factory=list)
    redo_stack: list[UndoEntry] = field(default_factory=list)

    def clear_undo(self) -> None:
        self.undo_stack.clear()
        self.redo_stack.clear()


_STATE = LibraryState()


def state() -> LibraryState:
    """全局库状态（测试可自行清空 / 替换字段）。"""
    return _STATE


def record_undo(
        kind: str,
        uuids,
        *,
        album: str | None = None,
        source_album: str | None = None,
) -> None:
    """登记一条可撤销记录（供 presenter 的按钮路径复用同一撤销栈）。"""
    targets = tuple(str(u) for u in uuids)
    if not targets:
        return
    _push_undo(UndoEntry(kind, targets, album=album, source_album=source_album))


# --------------------------------------------------------------------------- #
# 上下文小工具
# --------------------------------------------------------------------------- #
def _container(ctx) -> Any:
    return getattr(ctx, "container", None)


def _service(ctx, name: str) -> Any:
    container = _container(ctx)
    if container is None:
        return None
    try:
        return container.get(name, None)
    except Exception:  # noqa: BLE001 - 依赖缺失按「没有」处理
        _logger.exception("container.get(%r) failed", name)
        return None


def _presenters(ctx) -> Any:
    return getattr(ctx, "presenters", None)


def _window(ctx) -> Any:
    return getattr(ctx, "window", None)


def _view_service(ctx):
    return _service(ctx, "view_service")


def _asset_repo(ctx):
    return _service(ctx, "asset_repo")


def _trash_service(ctx):
    return _service(ctx, "trash_service")


def _toast(ctx, message: str) -> None:
    """尽量走窗口 toast；没有窗口时退到日志（不弹窗打扰）。"""
    window = _window(ctx)
    shower = getattr(window, "show_toast", None)
    if callable(shower):
        try:
            shower(message)
            return
        except Exception:  # noqa: BLE001
            _logger.exception("show_toast failed")
    _logger.info("%s", message)


def todo(ctx, feature: str) -> bool:
    """未落地能力的统一出口：提示 + 返回 False（不抛异常）。"""
    _toast(ctx, f"{feature} is not implemented yet (TODO)")
    _logger.info("TODO: %s", feature)
    return False


def _refresh(ctx, uuids: tuple[str, ...] = ()) -> None:
    presenters = _presenters(ctx)
    refresher = getattr(presenters, "refresh_assets", None)
    if callable(refresher):
        refresher(list(uuids))
        return
    detail = getattr(_window(ctx), "detail", None)
    notifier = getattr(detail, "notify_assets_changed", None)
    if callable(notifier):
        notifier(list(uuids))


def selected_uuids(ctx) -> tuple[str, ...]:
    """当前选择：详情页当前资产 / 网格光标项（多选未实现 → 单元素）。"""
    presenters = _presenters(ctx)
    getter = getattr(presenters, "selected_asset_uuids", None)
    if callable(getter):
        try:
            return tuple(str(u) for u in getter())
        except Exception:  # noqa: BLE001
            _logger.exception("selected_asset_uuids failed")
    return ()


def current_album_uuid(ctx) -> str | None:
    """当前相册 uuid（虚拟相册也返回；无相册时 None）。"""
    presenters = _presenters(ctx)
    getter = getattr(presenters, "current_album", None)
    album = getter() if callable(getter) else None
    album_uuid = getattr(album, "uuid", None)
    return str(album_uuid) if album_uuid else None


# --------------------------------------------------------------------------- #
# 系统相册判定 / 目标校验
# --------------------------------------------------------------------------- #
def is_system_album(ctx, album_uuid: Any) -> bool:
    """虚拟相册（All / Trash）→ True。"""
    view = _view_service(ctx)
    if view is None or not album_uuid:
        return False
    try:
        return view.get_scope(album_uuid) is not None
    except Exception:  # noqa: BLE001
        _logger.exception("get_scope failed for %r", album_uuid)
        return False


def is_trash_album(ctx, album_uuid: Any) -> bool:
    """是否回收站（``scope == "deleted"``）。"""
    view = _view_service(ctx)
    if view is None or not album_uuid:
        return False
    try:
        return view.get_scope(album_uuid) == SCOPE_DELETED
    except Exception:  # noqa: BLE001
        _logger.exception("get_scope failed for %r", album_uuid)
        return False


def _guard_target(ctx, album_uuid: str) -> bool:
    """目标相册合法性：不能是系统相册（回收站 / All）。"""
    if is_system_album(ctx, album_uuid):
        _toast(ctx, "System albums (All / Trash) cannot be a move target")
        return False
    return True


def can_be_slot_target(ctx, album_uuid: Any) -> bool:
    """快捷槽位目标校验：系统相册不能当槽位。"""
    target = str(album_uuid or "")
    return bool(target) and not is_system_album(ctx, target)


def rename_album(ctx, album_uuid: Any, title: str) -> bool:
    """重命名相册。

    · 系统相册（All / Trash）不可重命名；
    · TODO: 相册写入接口（AlbumRepository.update）尚未实现。
    """
    target = str(album_uuid or "")
    if not target or is_system_album(ctx, target):
        _toast(ctx, "System albums cannot be renamed")
        return False
    _ = title
    return todo(ctx, "rename album")


def delete_album(ctx, album_uuid: Any) -> bool:
    """删除相册（软删）。

    · 系统相册（All / Trash）不可删除；
    · TODO: 相册写入接口（AlbumRepository.soft_delete）尚未实现。
    """
    target = str(album_uuid or "")
    if not target or is_system_album(ctx, target):
        _toast(ctx, "System albums cannot be deleted")
        return False
    return todo(ctx, "delete album")


def in_trash_view(ctx) -> bool:
    """当前视图是否就是回收站。"""
    return is_trash_album(ctx, current_album_uuid(ctx))


# --------------------------------------------------------------------------- #
# 移动 / 删除
# --------------------------------------------------------------------------- #
def move_to_trash(ctx, uuids: tuple[str, ...] | None = None) -> bool:
    """`Delete`：把资产移入回收站（= 软删，记录可撤销）。"""
    if in_trash_view(ctx):
        _toast(ctx, "Already in Trash")
        return True
    targets = tuple(uuids) if uuids is not None else selected_uuids(ctx)
    if not targets:
        return False
    repo = _asset_repo(ctx)
    if repo is None:
        return False
    try:
        affected = repo.soft_delete_many(targets)
    except Exception:  # noqa: BLE001
        _logger.exception("soft_delete_many failed: %s", targets)
        _toast(ctx, "Move to Trash failed")
        return False
    if not affected:
        return False
    _push_undo(UndoEntry("trash", targets))
    _close_if_viewing(ctx, targets)
    _refresh(ctx, targets)
    return True


def restore_assets(ctx, uuids: tuple[str, ...] | None = None) -> bool:
    """恢复软删资产（供撤销 / 回收站内部调用）。"""
    targets = tuple(uuids) if uuids is not None else selected_uuids(ctx)
    if not targets:
        return False
    repo = _asset_repo(ctx)
    if repo is None:
        return False
    try:
        affected = repo.restore_many(targets)
    except Exception:  # noqa: BLE001
        _logger.exception("restore_many failed: %s", targets)
        return False
    if not affected:
        return False
    _push_undo(UndoEntry("restore", targets))
    _refresh(ctx, targets)
    return True


def empty_trash(ctx, *, confirm: bool = True) -> bool:
    """`Ctrl+Shift+Delete`：清空回收站（不可逆，先确认）。"""
    service = _trash_service(ctx)
    if service is None:
        return False
    try:
        count = int(service.count_deleted())
    except Exception:  # noqa: BLE001
        _logger.exception("count_deleted failed")
        count = 0
    if count <= 0:
        _toast(ctx, "Trash is already empty")
        return False
    if confirm and not _confirm(
            ctx,
            "Empty Trash",
            f"Permanently delete {count} asset(s) from the Trash?\n"
            "This cannot be undone.",
    ):
        return False
    try:
        report = service.empty_trash()
    except Exception:  # noqa: BLE001
        _logger.exception("empty_trash failed")
        _toast(ctx, "Empty Trash failed")
        return False
    state().clear_undo()
    _refresh(ctx)
    purged = len(getattr(report, "purged", []) or [])
    _toast(ctx, f"Trash emptied ({purged} removed)")
    return True


def _confirm(ctx, title: str, text: str) -> bool:
    """确认对话框；没有窗口 / 没有 Qt 时按「不执行」处理。"""
    window = _window(ctx)
    if window is None:
        return False
    try:
        from PySide6.QtWidgets import QMessageBox

        answer = QMessageBox.question(
            window, title, text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes
    except Exception:  # noqa: BLE001
        _logger.exception("confirm dialog failed")
        return False


def move_to_album(
        ctx,
        album_uuid: Any,
        uuids: tuple[str, ...] | None = None,
        *,
        source_album: str | None = None,
) -> bool:
    """把资产移入给定相册（相册成员关系，不复制文件）。"""
    target = str(album_uuid or "")
    if not target:
        _toast(ctx, "No target album given")
        return False
    if not _guard_target(ctx, target):
        return False
    targets = tuple(uuids) if uuids is not None else selected_uuids(ctx)
    if not targets:
        return False
    repo = _asset_repo(ctx)
    if repo is None:
        return False

    source = source_album if source_album is not None else current_album_uuid(ctx)
    # 来源是虚拟相册（All / Trash）时没有成员关系可移除
    if source and is_system_album(ctx, source):
        source = None
    if source == target:
        _toast(ctx, "Asset is already in this album")
        return False

    added = 0
    try:
        for asset_uuid in targets:
            added += repo.add_to_album(target, asset_uuid)
            if source:
                repo.remove_from_album(source, asset_uuid)
    except Exception:  # noqa: BLE001
        _logger.exception("move_to_album failed: %s -> %s", targets, target)
        _toast(ctx, "Move to album failed")
        return False
    if not added:
        _toast(ctx, "Nothing to move")
        return False
    _push_undo(UndoEntry("move", targets, album=target, source_album=source))
    _refresh(ctx, targets)
    return True


def move_to_target(ctx) -> bool:
    """`M`：移到当前目标相册；目标未设置时打开目标选择器。"""
    target = state().target_album
    if target:
        if target == current_album_uuid(ctx):
            _toast(ctx, "Current album is already the target")
            return False
        return move_to_album(ctx, target)
    return pick_target_album(ctx)


def move_to_slot(ctx, slot: int) -> bool:
    """`Shift+1..9`：移到快捷槽位相册。"""
    try:
        index = int(slot)
    except (TypeError, ValueError):
        return False
    if not 1 <= index <= QUICK_SLOT_COUNT:
        return False
    target = state().slots.get(index)
    if not target:
        return todo(
            ctx, f"quick slot {index} (Settings → target album is TODO)")
    return move_to_album(ctx, target)


def pick_target_album(ctx) -> bool:
    """打开目标相册选择器。

    TODO: 需要相册面板的「选择模式」——点选相册后把 uuid 写回
    ``state().target_album``。现在退化为打开相册浮层，不改变任何状态。
    """
    window = _window(ctx)
    overlay = getattr(window, "overlay", None)
    album_widget = getattr(window, "album", None)
    if overlay is None or album_widget is None:
        return False
    try:
        overlay.setCurrentWidget(album_widget)
    except Exception:  # noqa: BLE001
        _logger.exception("open album overlay failed")
        return False
    _toast(ctx, "Album picker is a TODO: choose an album manually")
    return True


def set_target_album(ctx, album_uuid: Any) -> bool:
    """设置 ``M`` 的目标相册（供设置 / 脚本调用；拒绝系统相册）。"""
    target = str(album_uuid or "")
    if not target:
        state().target_album = None
        return True
    if not _guard_target(ctx, target):
        return False
    state().target_album = target
    return True


def set_quick_slot(ctx, slot: int, album_uuid: Any) -> bool:
    """设置快捷槽位目标（TODO: 落配置 + 设置页下拉）。"""
    try:
        index = int(slot)
    except (TypeError, ValueError):
        return False
    if not 1 <= index <= QUICK_SLOT_COUNT:
        return False
    target = str(album_uuid or "")
    if target and not _guard_target(ctx, target):
        return False
    if target:
        state().slots[index] = target
    else:
        state().slots.pop(index, None)
    return True


# --------------------------------------------------------------------------- #
# 剪贴板
# --------------------------------------------------------------------------- #
def copy_assets(ctx, *, cut: bool = False) -> bool:
    """`Ctrl+C` / `Ctrl+X`：把当前选择放进内部剪贴板。"""
    targets = selected_uuids(ctx)
    if not targets:
        return False
    session = state()
    session.clipboard = list(targets)
    session.clipboard_cut = bool(cut)
    session.clipboard_source = current_album_uuid(ctx) if cut else None
    _toast(ctx, f"{len(targets)} asset(s) {'cut' if cut else 'copied'}")
    return True


def cut_assets(ctx) -> bool:
    return copy_assets(ctx, cut=True)


def paste_assets(ctx) -> bool:
    """`Ctrl+V`：粘贴到当前相册。

    · 复制 → 只加入当前相册（文件不重复复制）；
    · 剪切 → 加入后从来源相册移除。
    """
    session = state()
    if not session.clipboard:
        return False
    album_uuid = current_album_uuid(ctx)
    if not album_uuid or is_system_album(ctx, album_uuid):
        _toast(ctx, "Paste needs a physical album")
        return False
    source = session.clipboard_source if session.clipboard_cut else None
    ok = move_to_album(
        ctx, album_uuid, tuple(session.clipboard), source_album=source,
    )
    if ok:
        session.clipboard = []
        session.clipboard_cut = False
        session.clipboard_source = None
    return ok


# --------------------------------------------------------------------------- #
# 撤销 / 重做
# --------------------------------------------------------------------------- #
def _push_undo(entry: UndoEntry) -> None:
    session = state()
    session.undo_stack.append(entry)
    session.redo_stack.clear()


def undo(ctx) -> bool:
    """`Ctrl+Z`：撤销上一次移动 / 删除 / 恢复。"""
    session = state()
    if not session.undo_stack:
        return False
    entry = session.undo_stack.pop()
    if not _apply(ctx, entry, invert=True):
        session.undo_stack.append(entry)
        return False
    session.redo_stack.append(entry)
    _refresh(ctx, entry.uuids)
    return True


def redo(ctx) -> bool:
    """`Ctrl+Shift+Z`：重做。"""
    session = state()
    if not session.redo_stack:
        return False
    entry = session.redo_stack.pop()
    if not _apply(ctx, entry, invert=False):
        session.redo_stack.append(entry)
        return False
    session.undo_stack.append(entry)
    _refresh(ctx, entry.uuids)
    return True


def _apply(ctx, entry: UndoEntry, *, invert: bool) -> bool:
    repo = _asset_repo(ctx)
    if repo is None:
        return False
    try:
        if entry.kind in ("trash", "restore"):
            trash_like = entry.kind == "trash"
            if invert:
                trash_like = not trash_like
            if trash_like:
                repo.soft_delete_many(entry.uuids)
            else:
                repo.restore_many(entry.uuids)
            return True
        if entry.kind == "move":
            remove_from, add_to = entry.album, entry.source_album
            if invert:
                remove_from, add_to = add_to, remove_from
            for asset_uuid in entry.uuids:
                if remove_from:
                    repo.remove_from_album(remove_from, asset_uuid)
                if add_to:
                    repo.add_to_album(add_to, asset_uuid)
            return True
        if entry.kind == "album_add":
            for asset_uuid in entry.uuids:
                if not entry.album:
                    continue
                if invert:
                    repo.remove_from_album(entry.album, asset_uuid)
                else:
                    repo.add_to_album(entry.album, asset_uuid)
            return True
    except Exception:  # noqa: BLE001
        _logger.exception("undo/redo failed: %s", entry)
    return False


# --------------------------------------------------------------------------- #
# 复制 / 导出 / 元数据（TODO）
# --------------------------------------------------------------------------- #
def rename_asset(ctx) -> bool:
    """`F2`：重命名资产。

    TODO: AssetRepository 没有 rename / 改磁盘文件名的接口；需要「改库 +
    改文件」的事务语义，落地前保持空实现。
    """
    return todo(ctx, "rename asset")


def export_assets(ctx) -> bool:
    """`Ctrl+E`：导出资产。

    TODO: 需要导出对话框 + 复制策略（原图 / 缩略图 / 命名模板）。
    """
    return todo(ctx, "export assets")


def set_rating(ctx, rating: int) -> bool:
    """`0..5`：设置星级。

    TODO: assets 表没有 rating 列；需要 schema 迁移 + repo 写接口。
    """
    return todo(ctx, f"rating {int(rating)}")


def set_flag(ctx, flag: str) -> bool:
    """`P` / `X` / `U`：旗标 / 拒绝 / 取消旗标。

    TODO: assets 表没有 flag 列；需要 schema 迁移 + repo 写接口。
    """
    return todo(ctx, f"flag {flag}")


def new_album(ctx) -> bool:
    """`Ctrl+Shift+N`：新建相册。

    TODO: AlbumRepository 只有读接口；写入 DTO（AlbumCreate）尚未实现。
    """
    return todo(ctx, "create album")


def show_advanced_filter(ctx) -> bool:
    """`Ctrl+Shift+F`：高级筛选面板。

    TODO: 需要筛选条件模型 + 浮层；当前只有「标签过滤」一条通道。
    """
    return todo(ctx, "advanced filter")


# --------------------------------------------------------------------------- #
# 路径 / 文件（可直接落地）
# --------------------------------------------------------------------------- #
def _current_asset_path(ctx) -> str | None:
    targets = selected_uuids(ctx)
    if not targets:
        return None
    view = _view_service(ctx)
    if view is None:
        return None
    presenters = _presenters(ctx)
    include_deleted = bool(
        getattr(presenters, "is_quick_preview", False)
        or in_trash_view(ctx))
    try:
        return view.get_asset_full_path(
            targets[0], include_deleted=include_deleted)
    except Exception:  # noqa: BLE001
        _logger.exception("get_asset_full_path failed: %s", targets[0])
        return None


def copy_path(ctx) -> bool:
    """`Ctrl+Alt+C`：复制当前资产的磁盘路径。"""
    path = _current_asset_path(ctx)
    if not path:
        return False
    try:
        from PySide6.QtWidgets import QApplication

        clipboard = QApplication.clipboard()
        if clipboard is None:
            return False
        clipboard.setText(str(path))
    except Exception:  # noqa: BLE001
        _logger.exception("copy path failed")
        return False
    _toast(ctx, "Path copied")
    return True


def reveal_in_folder(ctx) -> bool:
    """`Ctrl+Alt+R`：在文件管理器中显示当前资产。"""
    path = _current_asset_path(ctx)
    if not path:
        return False
    try:
        from pathlib import Path

        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        target = Path(path)
        if target.is_dir():
            url = QUrl.fromLocalFile(str(target))
        else:
            url = QUrl.fromLocalFile(str(target.parent))
        QDesktopServices.openUrl(url)
    except Exception:  # noqa: BLE001
        _logger.exception("reveal in folder failed: %s", path)
        return False
    return True


def _close_if_viewing(ctx, uuids: tuple[str, ...]) -> None:
    """被移走的是正在查看的资产 → 收起查看层（避免留在已删资产上）。"""
    presenters = _presenters(ctx)
    if presenters is None or not getattr(presenters, "detail_visible", False):
        return
    active = getattr(presenters, "selected_asset_uuid", None)
    current = active() if callable(active) else None
    if current and current in uuids:
        closer = getattr(presenters, "close_detail", None)
        if callable(closer):
            closer()
