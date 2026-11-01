#
"""缩略图路径计算：``base/rel`` 布局的唯一来源。

布局契约（两种模式完全同构，唯一差异是注入的 ``root``）
------------------------------------------------------
::

    <root>/<xdg-spec>/<md5(file_uri(source_abs))>.png

其中 ``<xdg-spec>`` 来自 spec 映射（默认 ``SMALL→normal`` / ``MEDIUM→x-large``
/ ``LARGE→xx-large``）。

返回值的两段契约（与 ``assets`` 表的列一致）：

* ``base``      —— ``root`` 的绝对路径，落 ``assets.thumb_path``
* ``spec_rels`` —— ``"<xdg-spec>/<md5>.png"``，**相对 base**，落
  ``assets.thumb_<spec>_path``

``root`` 由调用方注入（私有模式 = ``thumb_root``，XDG 模式 =
``xdg_thumbnails_root()``），本模块**不碰任何全局配置**，测试可直接传临时目录。
``spec_map`` 缺省用内置默认映射；给定映射时**只产出映射里出现的 spec**。
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Mapping

from albuswall.common.enums import ThumbSpec
from albuswall.utils.xdg_thumbnail import thumbnail_filename, xdg_thumbnails_root

__all__ = ["build_thumb_paths", "build_xdg_thumb_paths"]

# 像素语义对齐（与 config.resolve_xdg_spec_map 的默认值一致）：
#   SMALL(128)  → normal(128)
#   MEDIUM(512) → x-large(512)
#   LARGE(1024) → xx-large(1024)
_DEFAULT_SPEC_MAP: Dict[ThumbSpec, str] = {
    ThumbSpec.SMALL: "normal",
    ThumbSpec.MEDIUM: "x-large",
    ThumbSpec.LARGE: "xx-large",
}


def build_thumb_paths(
        source_abs: str | Path,
        root: str | Path,
        spec_map: Mapping[ThumbSpec, str] | None = None,
) -> tuple[str, Dict[ThumbSpec, str]]:
    """统一布局：base = root；rel = ``"<xdg-spec>/<md5>.png"``。

    ``root`` 由调用方注入，本函数不碰全局配置，便于测试传临时目录。
    """
    base = Path(root)
    name = thumbnail_filename(source_abs)
    smap: Mapping[ThumbSpec, str] = spec_map or _DEFAULT_SPEC_MAP
    rels: Dict[ThumbSpec, str] = {
        spec: f"{smap[spec]}/{name}" for spec in smap
    }
    return str(base), rels


def build_xdg_thumb_paths(
        source_abs: str | Path,
        spec_map: Mapping[ThumbSpec, str] | None = None,
) -> tuple[str, Dict[ThumbSpec, str]]:
    """[薄封装] 根固定为 XDG 缓存根；仅为兼容旧调用点保留。

    新代码请直接用 :func:`build_thumb_paths` 并显式传 root —— 两种模式共用
    同一套布局后，这里不再有"XDG 专属"的路径逻辑。
    """
    return build_thumb_paths(
        source_abs, xdg_thumbnails_root(), spec_map,
    )
