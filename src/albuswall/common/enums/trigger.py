#
"""trigger 域枚举：导入源触发方式。"""

from enum import Enum


class UpdateMode(str, Enum):
    """导入源触发方式（纯值域定义，不做容错）。

    未知值降级只在 DTO 边界由 ``dto.utils.coerce_enum`` 负责；本枚举
    **不得**定义 ``_missing_``，否则写路径永远拿不到 ``ValueError``。
    读路径的兜底（未知 / NULL → ``MANUAL``）由调用方的 ``coerce_enum``
    显式给出默认值。
    """

    SCHEDULED_TIME = "scheduled_time"
    INTERVAL_TIME = "interval_time"
    DEVICE_TRIGGER = "device_trigger"
    MANUAL = "manual"
