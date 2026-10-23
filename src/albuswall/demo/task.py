#
"""任务服务的示例任务函数（模块级顶层函数）。

为什么单独成一个模块
--------------------
``ProcessPoolExecutor`` 用 spawn 启动子进程，子进程按
``fn.__module__ + fn.__qualname__`` 反查并重建函数对象。若这些函数定义在
``__main__``（例如 ``infrastructure/task/__main__.py`` 里），父进程侧
``pickle.dumps`` 是**能通过**的（它只记录 "模块 `__main__` + 名字"），
但子进程里的 ``__main__`` 是 ``__mp_main__``，于是 ``loads`` 时炸：

    AttributeError: module '__main__' has no attribute 'cpu_task'

放在一个普通可导入模块里，子进程 import 一次就拿到同一个函数对象。

约束
----
* 每个函数都必须是**模块级顶层**定义，不能包在函数/类里（否则
  ``__qualname__`` 带 ``<locals>``，父进程侧就会被 ``submit`` 拒掉）。
* 参数与返回值必须可 pickle。
* 本模块**只依赖标准库**，绝不要 import ``albuswall.infrastructure.task``：
  spawn 子进程会重复 import 这里，别把整个服务拖进子进程。
"""

from __future__ import annotations

import time

__all__ = ["cpu_task", "io_task"]


def io_task(task_id: int, sleep_time: float) -> str:
    """模拟 IO 任务：sleep 一段时间（线程执行器的典型负载）。"""
    print(f"[Thread] Task {task_id} started (sleep {sleep_time:.2f}s)")
    time.sleep(sleep_time)
    print(f"[Thread] Task {task_id} completed")
    return f"thread-{task_id}"


def cpu_task(task_id: int, n: int) -> str:
    """模拟 CPU 任务：纯计算（进程执行器的典型负载）。"""
    print(f"[Process] Task {task_id} started (compute {n} iterations)")
    total = 0
    for i in range(n):
        total += i * i
    print(f"[Process] Task {task_id} completed")
    return f"process-{task_id}"
