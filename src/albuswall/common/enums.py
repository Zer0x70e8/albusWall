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

class UpdateMode(str, Enum):
    SCHEDULED_TIME = "scheduled_time"
    INTERVAL_TIME = "interval_time"
    DEVICE_TRIGGER = "device_trigger"
    MANUAL = "manual"

    @classmethod
    def _missing_(cls, value):
        return cls.MANUAL
