#
"""缩略图目录布局（两种模式同构）。

布局契约 —— 私有模式与 XDG 模式**完全一致**，唯一差异是由调用方注入的
根目录常量 ``root``：

    <root>/<xdg-spec>/<md5(file_uri(source_abs))>.png

其中 ``<xdg-spec>`` 来自 spec 映射（默认 SMALL→normal、
MEDIUM→x-large、LARGE→xx-large）。

返回值的两段契约（与 ``assets`` 表列一致）：
    base        —— ``root`` 的绝对路径，落 ``assets.thumb_path``
    spec_rels   —— ``"<xdg-spec>/<md5>.png"``，**相对 base**，落
                   ``assets.thumb_<spec>_path``
完整路径 = ``base.rstrip('/') + '/' + spec_rels[spec]``，与旧私有布局
（``base/rel``）的读取拼接口径完全相同，因此读取侧零改动。

为什么文件名是源文件 md5 而不是 asset uuid：XDG 协议要求缩略图与
**源文件**一一对应（同一个源文件被多条 asset 行引用时共享同一张图）。
私有模式跟随时也一并放弃 uuid/version 目录层级 —— 布局同构是本次
重构的核心目标，否则清理器就得写两套解析逻辑。
"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

from albuswall.common.enums import ThumbSpec
from albuswall.utils.xdg_thumbnail import thumbnail_filename, xdg_thumbnails_root

# 像素语义对齐（与 config.resolve_xdg_spec_map 的默认值一致）：
#   SMALL(128)  → normal(128)
#   MEDIUM(512) → x-large(512)
#   LARGE(1024) → xx-large(1024)
_DEFAULT_SPEC_MAP: dict[ThumbSpec, str] = {
    ThumbSpec.SMALL: "normal",
    ThumbSpec.MEDIUM: "x-large",
    ThumbSpec.LARGE: "xx-large",
}


def build_thumb_paths(
        source_abs: str | Path,
        root: str | Path,
        spec_map: Mapping[ThumbSpec, str] | None = None,
) -> tuple[str, dict[ThumbSpec, str]]:
    """统一布局：base = root；rel = ``"<xdg-spec>/<md5>.png"``。

    ``root`` 由调用方注入（私有模式 = thumb_root，XDG 模式 =
    xdg_thumbnails_root()），本函数不碰任何全局配置，便于测试直接传
    临时目录。

    ``spec_map`` 缺省用内置默认映射；给定映射时**只产出映射里出现的
    spec**（与旧 build_xdg_thumb_paths 语义一致）。
    """
    base = Path(root)
    name = thumbnail_filename(source_abs)
    smap: Mapping[ThumbSpec, str] = spec_map or _DEFAULT_SPEC_MAP
    rels: dict[ThumbSpec, str] = {
        spec: f"{smap[spec]}/{name}" for spec in smap
    }
    return str(base), rels


def build_xdg_thumb_paths(
        source_abs: str | Path,
        spec_map: Mapping[ThumbSpec, str] | None = None,
) -> tuple[str, dict[ThumbSpec, str]]:
    """[薄封装] 根固定为 XDG 缓存根；仅为兼容旧调用点保留。

    新代码请直接用 :func:`build_thumb_paths` 并显式传 root —— 两种模式
    共用同一套布局后，这里不再有"XDG 专属"的路径逻辑。
    """
    return build_thumb_paths(
        source_abs, xdg_thumbnails_root(), spec_map,
    )
