#
""""""

from enum import Enum


class FileTypeCheckMode(Enum):
    SUFFIX = "suffix"
    MAGIC = "magic"
    NONE = None

    @classmethod
    def _missing_(cls, value):
        # 将未知值视为 NONE
        return cls.NONE
