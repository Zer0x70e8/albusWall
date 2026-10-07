#
"""对账器：把「外部资源 ↔ db 记录」的清理统一收口。

对外公开：
    - ``Reconciler``           —— 唯一入口
    - ``register_reconciler``  —— 容器注册 + 生命周期钩子
    - ``IncrementalFn / FullFn / ShouldRunFn`` —— 回调签名别名
"""

from .service import (
    Reconciler,
    IncrementalFn,
    FullFn,
    ShouldRunFn,
)
from .bootstrap import register_reconciler

__all__ = [
    "Reconciler",
    "register_reconciler",
    "IncrementalFn",
    "FullFn",
    "ShouldRunFn",
]