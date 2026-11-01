#
"""Unified Task Service package.

对外公开（稳定）：
    - ``TaskService``         —— 唯一推荐的实现（service.py）
    - ``TaskServiceProtocol`` —— 使用者应当面向的接口契约（protocol.py）
    - ``TaskSpec``            —— 结构化提交的写模型（转发自 dto.task）

内部件（`_` 前缀，不保证兼容）：
    - ``_semaphore`` : DynamicSemaphore
    - ``_task``      : _Task 数据类
"""

from .protocol import (
    BackpressureEvent,
    ExecutorArg,
    TaskServiceProtocol,
    TaskServiceStats,
    TaskSpec,
)
from .service import TaskService
from .bootstrap import register_task_service

__all__ = [
    "TaskService",
    "TaskServiceProtocol",
    "BackpressureEvent",
    "TaskServiceStats",
    "TaskSpec",
    "ExecutorArg",
    "register_task_service",
]
