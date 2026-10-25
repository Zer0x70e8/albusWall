"""移动 / 删除 / 剪贴板 / 撤销 / 评分旗标：库级动作。

按键语义（与 ``docs/keymap.md`` 对齐）：

===========================  =====================================
``Delete``                   移到系统相册《回收站》
``Ctrl+Shift+Delete``        清空回收站（永久删除，需确认）
``Shift+1..9``               移到快捷槽位相册（槽位目标在设置里指定）
``M``                        移到当前目标相册 / 打开目标选择器
``Ctrl+Shift+M``             移到相册（总是弹选择器）
``Ctrl+Z`` / ``Ctrl+Shift+Z`` 撤销 / 重做
``Ctrl+C`` / ``Ctrl+X`` / ``Ctrl+V``  复制 / 剪切 / 粘贴
``F2``                       重命名（TODO：接口留空）
``Ctrl+E``                   导出（TODO：接口留空）
``0..5``                     评分（TODO：无 rating 列）
``P`` / ``X`` / ``U``        旗标 / 拒绝 / 取消旗标（TODO：无 flag 列）
``Ctrl+Alt+R`` / ``Ctrl+Alt+C``  在文件夹中显示 / 复制路径
``Ctrl+Shift+N``             新建相册（TODO：无写接口）
"""
from __future__ import annotations

from albuswall.ui.builtin import library

from ..keymap import shortcut

__all__ = ["LIBRARY_ACTION_IDS"]

LIBRARY_ACTION_IDS: tuple[str, ...] = (
    "asset.trash", "asset.empty_trash", "asset.move_target",
    "asset.move_to_slot", "asset.move_to_album", "asset.move_to_album_picker",
    "asset.undo", "asset.redo", "asset.copy", "asset.cut", "asset.paste",
    "asset.rename", "asset.export", "asset.rating", "asset.flag",
    "asset.reject", "asset.unflag", "asset.reveal", "asset.copy_path",
    "asset.new_album",
)


# ---------------------------------------------------------------- 移动 / 删除
@shortcut("asset.trash", label="Move to Trash",
          description="Move the current asset to the system Trash album "
                      "(same model as moving to an album, not a hard delete).",
          keys="Delete", component="window.library", order=100)
def _trash(ctx) -> bool:
    return library.move_to_trash(ctx)


@shortcut("asset.empty_trash", label="Empty Trash",
          description="Permanently delete everything in the Trash. "
                      "Irreversible, asks for confirmation.",
          keys="Ctrl+Shift+Delete", component="window.library", order=105)
def _empty_trash(ctx) -> bool:
    return library.empty_trash(ctx)


@shortcut("asset.move_target", label="Move to target album",
          description="Move the selection to the current target album; "
                      "when no target is set, open the target picker.",
          keys="M", component="window.library", order=110)
def _move_target(ctx) -> bool:
    return library.move_to_target(ctx)


@shortcut("asset.move_to_slot", label="Move to quick slot",
          description="Move the selection to one of the nine quick-slot "
                      "albums configured in Settings.",
          keys={f"Shift+{index}": {"slot": index}
                for index in range(1, library.QUICK_SLOT_COUNT + 1)},
          component="window.library", order=115,
          arguments={"slot": 1})
def _move_to_slot(ctx, slot: int = 1) -> bool:
    return library.move_to_slot(ctx, slot)


@shortcut("asset.move_to_album", label="Move to a specific album",
          description="Move the selection to the album given by the album "
                      "argument. Bindable to any key; the key must include "
                      "Ctrl / Alt / Meta when bound manually.",
          component="window.library", order=120, arguments={"album": ""})
def _move_to_album(ctx, album: str = "") -> bool:
    return library.move_to_album(ctx, album)


@shortcut("asset.move_to_album_picker", label="Move to album…",
          description="Open the album picker and move the selection.",
          keys="Ctrl+Shift+M", component="window.library", order=125)
def _move_to_album_picker(ctx) -> bool:
    return library.pick_target_album(ctx)


# ---------------------------------------------------------------- 撤销
@shortcut("asset.undo", label="Undo",
          description="Undo the last move / delete / restore.",
          keys="Ctrl+Z", component="window.library", order=130)
def _undo(ctx) -> bool:
    return library.undo(ctx)


@shortcut("asset.redo", label="Redo",
          description="Redo the last undone operation.",
          keys="Ctrl+Shift+Z", component="window.library", order=135)
def _redo(ctx) -> bool:
    return library.redo(ctx)


# ---------------------------------------------------------------- 剪贴板
@shortcut("asset.copy", label="Copy assets",
          description="Copy the current selection to the internal clipboard.",
          keys="Ctrl+C", component="window.library", order=140)
def _copy(ctx) -> bool:
    return library.copy_assets(ctx)


@shortcut("asset.cut", label="Cut assets",
          description="Cut the current selection to the internal clipboard.",
          keys="Ctrl+X", component="window.library", order=145)
def _cut(ctx) -> bool:
    return library.cut_assets(ctx)


@shortcut("asset.paste", label="Paste assets",
          description="Paste the clipboard into the current album.",
          keys="Ctrl+V", component="window.library", order=150)
def _paste(ctx) -> bool:
    return library.paste_assets(ctx)


# ---------------------------------------------------------------- TODO 能力
@shortcut("asset.rename", label="Rename asset",
          description="Rename the current asset. TODO: no rename API yet.",
          keys="F2", component="window.library", order=155)
def _rename(ctx) -> bool:
    return library.rename_asset(ctx)


@shortcut("asset.export", label="Export assets",
          description="Export the current selection. TODO: no export dialog yet.",
          keys="Ctrl+E", component="window.library", order=160)
def _export(ctx) -> bool:
    return library.export_assets(ctx)


@shortcut("asset.rating", label="Rate asset",
          description="Set the star rating. TODO: the assets table has no "
                      "rating column yet.",
          keys={str(value): {"rating": value} for value in range(6)},
          component="window.library", order=165, arguments={"rating": 0})
def _rating(ctx, rating: int = 0) -> bool:
    return library.set_rating(ctx, rating)


@shortcut("asset.flag", label="Flag asset as picked",
          description="Mark the asset as picked. TODO: the assets table has "
                      "no flag column yet.",
          keys="P", component="window.library", order=170, arguments={"flag": "pick"})
def _flag(ctx, flag: str = "pick") -> bool:
    return library.set_flag(ctx, flag)


@shortcut("asset.reject", label="Reject asset",
          description="Mark the asset as rejected. TODO: no flag column yet.",
          keys="X", component="window.library", order=175,
          arguments={"flag": "reject"})
def _reject(ctx, flag: str = "reject") -> bool:
    return library.set_flag(ctx, flag)


@shortcut("asset.unflag", label="Clear asset flag",
          description="Clear the asset flag. TODO: no flag column yet.",
          keys="U", component="window.library", order=180,
          arguments={"flag": "none"})
def _unflag(ctx, flag: str = "none") -> bool:
    return library.set_flag(ctx, flag)


# ---------------------------------------------------------------- 文件
@shortcut("asset.reveal", label="Reveal in file manager",
          description="Open the folder that contains the current asset.",
          keys="Ctrl+Alt+R", component="window.library", order=185)
def _reveal(ctx) -> bool:
    return library.reveal_in_folder(ctx)


@shortcut("asset.copy_path", label="Copy file path",
          description="Copy the full path of the current asset.",
          keys="Ctrl+Alt+C", component="window.library", order=190)
def _copy_path(ctx) -> bool:
    return library.copy_path(ctx)


@shortcut("asset.new_album", label="New album",
          description="Create a new album. TODO: the album write API is "
                      "not implemented yet.",
          keys="Ctrl+Shift+N", component="window.album", order=10)
def _new_album(ctx) -> bool:
    return library.new_album(ctx)
