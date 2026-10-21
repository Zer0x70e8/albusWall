#
"""缩略图 schema 常量：spec ↔ assets 列名映射。

这是 **schema 契约**，不是 DTO：仓储 / 服务 / 迁移脚本一律从这里
``import``，禁止就地重定义；依赖方向为 ``common ← (repo/service/ui)``，
本模块不得反向 ``import albuswall.dto``。
"""

from albuswall.common.enums.thumbnail import ThumbSpec

__all__ = [
    "ALL_SPECS",
    "SPEC_TO_COLUMN",
    "COLUMN_TO_SPEC",
    "BASE_COLUMN",
    "ALL_THUMB_COLUMNS",
]

# spec 全集，顺序即 UI / 仓储遍历顺序
ALL_SPECS: tuple[ThumbSpec, ...] = (
    ThumbSpec.SMALL, ThumbSpec.MEDIUM, ThumbSpec.LARGE,
)

# spec → assets 列名
SPEC_TO_COLUMN: dict[ThumbSpec, str] = {
    ThumbSpec.SMALL: "thumb_small_path",
    ThumbSpec.MEDIUM: "thumb_medium_path",
    ThumbSpec.LARGE: "thumb_large_path",
}

COLUMN_TO_SPEC: dict[str, ThumbSpec] = {v: k for k, v in SPEC_TO_COLUMN.items()}

# 缩略图主目录列，独立于 spec
BASE_COLUMN: str = "thumb_path"

# 所有缩略图列（含 base），顺序固定：base 在前，spec 按 ALL_SPECS 顺序
ALL_THUMB_COLUMNS: tuple[str, ...] = (BASE_COLUMN, *(SPEC_TO_COLUMN[s] for s in ALL_SPECS))
