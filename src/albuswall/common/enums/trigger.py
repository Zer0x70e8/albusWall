#
""""""

from enum import Enum


class UpdateMode(str, Enum):
    SCHEDULED_TIME = "scheduled_time"
    INTERVAL_TIME = "interval_time"
    DEVICE_TRIGGER = "device_trigger"
    MANUAL = "manual"

    @classmethod
    def _missing_(cls, value):
        return cls.MANUAL
