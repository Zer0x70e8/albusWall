#
"""缩略图磁盘操作：写 / 删。

- 写入走 infrastructure.fs.atomic_write，保证不出现半截文件。
- 删除是 best-effort：文件删不掉只 warning。
"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, TYPE_CHECKING

from albuswall.dto.thumbnail import ALL_SPECS, ThumbSpec, ThumbnailPaths
from albuswall.infrastructure.fs import atomic_write
from albuswall.log import getLogger

if TYPE_CHECKING:
    from PIL.Image import Image as PILImage

_logger = getLogger(__name__)


class ThumbnailStorage:
    """封装缩略图的磁盘读写。构造时注入 thumb_root / 格式 / 质量。"""

    def __init__(self, thumb_root: Path, fmt: str, quality: int):
        self._root = Path(thumb_root)
        self._fmt = fmt.upper()
        self._quality = quality
        self._save_kwargs: dict = (
            {"quality": quality} if self._fmt in ("JPEG", "JPG", "WEBP") else {}
        )

    # ------------------------------------------------------------------ #
    # 写
    # ------------------------------------------------------------------ #
    def write(
            self,
            base_rel: str,
            spec_rels: Mapping[ThumbSpec, str],
            imgs: Mapping[ThumbSpec, "PILImage"],
    ) -> str:
        """把多个 spec 的图片写到 thumb_root/base_rel/ 下。

        返回 base 的绝对路径（用于回填 assets.thumb_path）。
        任一 spec 写失败会向上抛，调用方按暂时性故障处理。
        """
        base_abs = self._root / base_rel
        for spec, img in imgs.items():
            rel = spec_rels.get(spec)
            if rel is None:
                continue
            self._save_one(img, base_abs / rel)
        return str(base_abs)

    def _save_one(self, img: "PILImage", dest: Path) -> None:
        fmt = self._fmt
        kwargs = self._save_kwargs
        atomic_write(dest, lambda p: img.save(p, format=fmt, **kwargs))

    # ------------------------------------------------------------------ #
    # 删
    # ------------------------------------------------------------------ #
    @staticmethod
    def delete(paths: ThumbnailPaths) -> None:
        """按 (绝对 base, 相对 spec) 删文件。best-effort，不抛异常。

        删完顺手 rmdir base 目录（目录不空则忽略）。
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

        try:
            base_path.rmdir()
        except OSError:
            pass
