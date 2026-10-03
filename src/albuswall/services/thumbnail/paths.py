#
"""缩略图目录布局。

base_rel 是**相对 thumb_root** 的目录路径；
spec_rels 是**相对 base** 的文件名。
完整路径 = thumb_root / base_rel / spec_rels[spec]
"""

from __future__ import annotations

from albuswall.dto.thumbnail import ThumbSpec

# 输出格式 → 文件扩展名
_EXT_MAP: dict[str, str] = {
    "JPEG": "jpg",
    "JPG": "jpg",
    "PNG": "png",
    "WEBP": "webp",
}


def _ext_for(fmt: str) -> str:
    return _EXT_MAP.get(fmt.upper(), fmt.lower())


def build_thumb_paths(
        uuid: str, version: str, fmt: str,
) -> tuple[str, dict[ThumbSpec, str]]:
    """构造 (base_rel, {spec: rel})。

    布局：
        base_rel  = "{version}/{uuid[:2]}/{uuid}"
        spec_rels = {"small": "small.jpg", "medium": "medium.jpg", ...}

    按 uuid 前两位分片，避免单目录下文件过多。
    version 进路径，使升级策略后旧资产仍指向旧目录（不覆盖，可回滚）。
    """
    ext = _ext_for(fmt)
    base_rel = f"{version}/{uuid[:2]}/{uuid}"
    spec_rels: dict[ThumbSpec, str] = {
        ThumbSpec.SMALL: f"small.{ext}",
        ThumbSpec.MEDIUM: f"medium.{ext}",
        ThumbSpec.LARGE: f"large.{ext}",
    }
    return base_rel, spec_rels
