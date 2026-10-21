#
"""thumbnail 域枚举：缩略图规格。"""

from enum import Enum


class ThumbSpec(str, Enum):
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"
