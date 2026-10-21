#
"""import 域枚举：文件类型检查方式。"""

from enum import Enum


class FileTypeCheckMode(Enum):
    """文件类型检查方式（纯值域定义，不做容错）。

    未知值降级只在 DTO 边界由 ``dto.utils.coerce_enum`` 负责；本枚举
    **不得**定义 ``_missing_``，否则写路径永远拿不到 ``ValueError``。

    ``NONE`` 是 Python 侧哨兵，schema 的
    ``CHECK (file_type_check IN ('suffix', 'magic'))`` 不允许它落库。
    """

    SUFFIX = "suffix"
    MAGIC = "magic"
    NONE = None
