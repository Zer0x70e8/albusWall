#
"""缩略图磁盘操作：写 / 删。

- 写入走 infrastructure.fs.atomic_write，保证不出现半截文件。
- 写入前按目标格式规范化图片模式，JPEG 不支持 alpha 需合成白底。
- Pillow 的模式/格式不兼容错误包装成 ThumbnailFormatError，供调用方
  区分「永久性失败」与「暂时性 IO 故障」。
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


class ThumbnailFormatError(Exception):
    """图片模式/格式与目标输出不兼容，重试无意义。"""


# JPEG 直接支持的图片模式
_JPEG_NATIVE_MODES = frozenset({"RGB", "L", "CMYK"})


class ThumbnailStorage:
    """封装缩略图的磁盘读写。构造时注入 thumb_root / 格式 / 质量。"""

    def __init__(self, thumb_root: Path, fmt: str, quality: int):
        self._root = Path(thumb_root)
        # JPG 归一化为 JPEG（Pillow 的显式 format= 只认 "JPEG"）
        normalized = fmt.upper()
        self._fmt = "JPEG" if normalized in ("JPG", "JPEG") else normalized
        self._quality = quality
        self._save_kwargs: dict = (
            {"quality": quality}
            if self._fmt in ("JPEG", "WEBP")
            else {}
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

        异常约定：
          - ThumbnailFormatError：模式/格式不兼容，永久性失败，不要重试
          - OSError / 其它：暂时性失败，可重试
        """
        base_abs = self._root / base_rel
        for spec, img in imgs.items():
            rel = spec_rels.get(spec)
            if rel is None:
                continue
            self._save_one(img, base_abs / rel)
        return str(base_abs)

    def _save_one(self, img: "PILImage", dest: Path) -> None:
        try:
            prepared = self._prepare_for_save(img)
        except Exception as exc:
            raise ThumbnailFormatError(
                f"cannot convert mode {img.mode} for {self._fmt}: {exc}"
            ) from exc

        try:
            fmt = self._fmt
            kwargs = self._save_kwargs
            try:
                atomic_write(
                    dest,
                    lambda p: prepared.save(p, format=fmt, **kwargs),
                )
            except (OSError, ValueError) as exc:
                if self._is_format_error(exc):
                    raise ThumbnailFormatError(
                        f"cannot save mode {img.mode} as {fmt}: {exc}"
                    ) from exc
                raise
        finally:
            if prepared is not img:
                try:
                    prepared.close()
                except Exception:
                    pass

    def _prepare_for_save(self, img: "PILImage") -> "PILImage":
        """把图片模式规范化到目标格式支持的集合。

        - JPEG：alpha 通道会被合成到白底；P 模式带 transparency 会先展平。
        - 其它格式：原样返回。
        """
        if self._fmt != "JPEG":
            return img

        mode = img.mode
        if mode in _JPEG_NATIVE_MODES:
            return img

        # P 模式：带 transparency 的先展平到 RGBA，否则直接转 RGB
        if mode == "P":
            if "transparency" in img.info:
                img = img.convert("RGBA")
            else:
                return img.convert("RGB")

        if img.mode == "LA":
            img = img.convert("RGBA")

        if img.mode == "RGBA":
            from PIL import Image as PILImageModule
            background = PILImageModule.new("RGB", img.size, (255, 255, 255))
            background.paste(img, mask=img.getchannel("A"))
            return background

        # 其它模式（I;16、F 等）直接转 RGB
        return img.convert("RGB")

    @staticmethod
    def _is_format_error(exc: BaseException) -> bool:
        """Pillow 在模式/格式不兼容时抛的异常特征。"""
        msg = str(exc).lower()
        if "cannot write mode" in msg:
            return True
        if "cannot save mode" in msg:
            return True
        if "not supported" in msg and "mode" in msg:
            return True
        return False

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
