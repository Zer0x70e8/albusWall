#
"""缩略图磁盘操作：写 / 删（两种模式同构）。

布局契约由 :mod:`albuswall.services.thumbnail.paths` 决定（``base/rel``），
本模块只负责"把 PIL.Image 落到 rel 指定的位置"，并保证：

- 写入走 ``infrastructure.fs.atomic_write``，绝不出现半截文件；
- 输出**强制 PNG**：PNG 内嵌 ``Thumb::URI`` / ``Thumb::MTime`` /
  ``Thumb::Size`` / ``Software`` 文本块（XDG Thumbnail Managing Standard
  规定的元数据位置就是 PNG tEXt，JPEG 没有对等位置，因此两种模式统一
  PNG，放弃 image_format / quality 可配）；
- 目录 0700、文件 0600（协议要求的隐私位）；
- 永久失败写 ``<root>/fail/<md5>.png`` 跨进程标记；
- Pillow 无法表达的图像模式包装成 :class:`ThumbnailFormatError`，供
  调用方区分「永久性失败」与「暂时性 IO 故障」。

存储对象只持有一个 **root**：私有模式传 thumb_root，XDG 模式传
xdg_thumbnails_root()，除此之外没有任何行为分叉。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, TYPE_CHECKING

from albuswall.common.constants.thumbnail import ALL_SPECS
from albuswall.common.enums import ThumbSpec
from albuswall.dto.thumbnail import ThumbnailPaths
from albuswall.infrastructure.fs import atomic_write
from albuswall.log import getLogger
from albuswall.utils.xdg_thumbnail import (
    XDG_DIR_MODE, XDG_FILE_MODE,
    file_uri, xdg_fail_path,
)

if TYPE_CHECKING:
    from PIL.Image import Image as PILImage

_logger = getLogger(__name__)


class ThumbnailFormatError(Exception):
    """图片模式/格式与目标输出不兼容，重试无意义。"""


def _chmod_dir(p: Path) -> None:
    try:
        os.chmod(p, XDG_DIR_MODE)
    except OSError:
        pass


def _chmod_file(p: Path) -> None:
    try:
        os.chmod(p, XDG_FILE_MODE)
    except OSError:
        pass


def _to_rgba(img: "PILImage") -> "PILImage":
    """强制 PNG 输出；统一转 RGBA（PNG 原生支持 alpha）。

    PNG 不存在 JPEG 的"模式白名单"问题，统一 RGBA 即可覆盖 P / LA /
    I;16 / F 等所有输入模式，省掉原私有模式的 _prepare_for_save 分支。
    """
    if img.mode == "RGBA":
        return img
    return img.convert("RGBA")


class ThumbnailStorage:
    """封装缩略图的磁盘读写。构造时只注入 root。

    私有模式与 XDG 模式共用本类；根目录是唯一差异。
    """

    def __init__(self, root: Path):
        self._root = Path(root)

    # ------------------------------------------------------------------ #
    # 写
    # ------------------------------------------------------------------ #
    def write(
            self,
            source_abs: str | Path,
            spec_rels: Mapping[ThumbSpec, str],
            imgs: Mapping[ThumbSpec, "PILImage"],
    ) -> str:
        """把多个 spec 的图片按 ``spec_rels`` 写到 ``root`` 下。

        返回 ``root`` 的绝对路径（用于回填 ``assets.thumb_path``）：
        两种模式下 base 都是"共享根"，具体文件由 spec_rel 定位。

        异常约定：
          - ThumbnailFormatError：图像模式无法编码为 PNG，永久性失败
          - OSError / 其它：暂时性失败（源被删、磁盘满、权限），可重试
        """
        from PIL import PngImagePlugin

        source = Path(source_abs)
        st = os.stat(source)  # 失败 → OSError → 走重试/永久失败
        uri = file_uri(source)
        mtime = int(st.st_mtime)
        size = st.st_size

        for spec, img in imgs.items():
            rel = spec_rels.get(spec)
            if rel is None:
                continue
            dest = self._root / rel
            dest.parent.mkdir(
                parents=True, exist_ok=True, mode=XDG_DIR_MODE,
            )
            _chmod_dir(dest.parent)

            pnginfo = PngImagePlugin.PngInfo()
            pnginfo.add_text("Thumb::URI", uri)
            pnginfo.add_text("Thumb::MTime", str(mtime))
            pnginfo.add_text("Thumb::Size", str(size))
            pnginfo.add_text("Software", "albuswall")

            try:
                prepared = _to_rgba(img)
            except Exception as exc:
                raise ThumbnailFormatError(
                    f"cannot convert mode {img.mode} to RGBA for PNG: {exc}"
                ) from exc

            try:
                atomic_write(
                    dest,
                    lambda p: prepared.save(
                        p, format="PNG", pnginfo=pnginfo,
                    ),
                )
            finally:
                if prepared is not img:
                    try:
                        prepared.close()
                    except Exception:
                        pass
            _chmod_file(dest)  # rename 后重新 chmod
        return str(self._root)

    def write_fail_marker(self, source_abs: str | Path) -> None:
        """永久失败标记：``<root>/fail/<md5>.png``，空文件即可（协议允许）。

        best-effort：写不进去只 warning，不能因为标记失败影响主流程。
        """
        p = xdg_fail_path(source_abs, root=self._root)
        try:
            p.parent.mkdir(
                parents=True, exist_ok=True, mode=XDG_DIR_MODE,
            )
            _chmod_dir(p.parent)
            p.touch(exist_ok=True)
            _chmod_file(p)
        except OSError as exc:
            _logger.warning("thumb fail marker write failed %s: %s", p, exc)

    # ------------------------------------------------------------------ #
    # 删（仅显式清理 / 迁移用；常规 purge 只清 DB）
    # ------------------------------------------------------------------ #
    @staticmethod
    def delete(paths: ThumbnailPaths) -> None:
        """按 (绝对 base, 相对 spec) 删文件。best-effort，不抛异常。

        **常规 purge 不再调用本方法**：新布局下文件名是源文件 md5，
        可能被多条 asset 行共享，直接 unlink 会波及别的引用；孤儿一律
        交给与根目录配套的 ThumbnailCleaner 按 ``Thumb::URI`` 兜底。

        这里保留给迁移 / 显式清理场景；只 unlink 文件，不再 rmdir
        base —— base 现在是所有资产共享的根目录，删空它没有意义。
        """
        base = paths.base
        if not base:
            return
        base_path = Path(base)

        for spec in ALL_SPECS:
            rel = paths.for_spec(spec)
            if not rel:
                continue
            fp = Path(rel) if Path(rel).is_absolute() else base_path / rel
            try:
                fp.unlink(missing_ok=True)
            except OSError as exc:
                _logger.warning("thumbnail delete failed %s: %s", fp, exc)
