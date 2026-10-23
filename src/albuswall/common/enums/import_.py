#
"""import 域枚举：文件类型检查方式。"""

from enum import Enum


class FileTypeCheckMode(Enum):
    """文件类型检查方式（纯值域定义，不做容错）。

    未知值降级只在 DTO 边界由 ``dto.utils.coerce_enum`` 负责；本枚举
    **不得**定义 ``_missing_``，否则写路径永远拿不到 ``ValueError``。

    ``NONE`` 是 Python 侧哨兵，schema 的
    ``CHECK (file_type_check IN ('suffix', 'magic', 'off', 'magic_lax'))``
    不允许它落库。

    两种检查强度的分工（Stage 1 已由 glob 粗筛，这里只管"要不要验证"）：

    * ``OFF``       —— 不校验。glob 命中即接受，mime 用 ``mimetypes`` 猜。
      与 ``SUFFIX`` 当前行为一致，但语义不同：``OFF`` 表达"这个归档目录
      全都要"，``SUFFIX`` 表达"我信任后缀"。保留两者是为了将来给
      ``SUFFIX`` 加更严格的校验（如后缀白名单命中后再查 mime 前缀）时，
      ``OFF`` 的行为不必跟着变。
    * ``SUFFIX``    —— 只信后缀（Stage 1 的 glob 已经保证了这一点）。
    * ``MAGIC``     —— 读文件头；识别不出 → 跳过（严格）。
    * ``MAGIC_LAX`` —— 读文件头；识别不出 → 接受但降级为
      ``application/octet-stream``（宽松，适合混合归档）。
    """

    SUFFIX = "suffix"
    MAGIC = "magic"
    OFF = "off"
    MAGIC_LAX = "magic_lax"
    NONE = None
