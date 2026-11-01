#
"""被动写命令层：写 ``ingest_source`` 表 + 发三个变更信号。

* :class:`SourceService` —— CRUD 与纯读转发，不持有运行时状态。

本层**不** import ``state`` / ``runtime`` / ``trigger`` 任何子包。
"""

from .service import SourceService

__all__ = ["SourceService"]
