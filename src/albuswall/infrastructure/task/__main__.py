#
"""python -m albuswall.infrastructure.task 运行示例。

示例任务函数定义在 :mod:`albuswall.demo.demo`（模块级顶层函数）。
**不要**把它们搬回本文件：进程任务用 spawn 执行，定义在 ``__main__``
里的函数在子进程反序列化时必然失败
（``AttributeError: module '__main__' has no attribute ...``）。
"""

from __future__ import annotations

import logging
import random

from albuswall.common.exceptions import BackpressureError
from albuswall.dto.task import BackpressureEvent, TaskSpec
from albuswall.infrastructure.task import TaskService
from albuswall.demo.task import cpu_task, io_task


def on_bp(evt: BackpressureEvent):
    print(f"[ALERT] backpressure triggered: {evt}")


def main():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    )

    service = TaskService(
        initial_max_concurrency=6,
        min_concurrency=2,
        max_concurrency=32,
        process_workers=4,
        check_interval=3,
        scheduler_threads=2,
        low_load_streak=3,
        max_queue_size=500,
        submit_timeout=5.0,
        wait_warn_threshold=1.0,
    )
    service.set_backpressure_callback(on_bp)

    futures = []
    for idx in range(10):
        specs = (
            TaskSpec(
                fn=io_task,
                args=(idx, random.uniform(0.3, 1.0)),
                executor="thread",
                priority=random.randint(0, 4),
            ),
            TaskSpec(
                fn=cpu_task,
                args=(idx, random.randint(1_000_000, 5_000_000)),
                executor="process",
                priority=random.randint(0, 4),
            ),
        )
        for spec in specs:
            try:
                futures.append(service.submit_spec(spec))
            except BackpressureError as e:
                print(f"submit rejected by backpressure: {e}")

    for f in futures:
        try:
            f.result()
        except Exception as e:
            print("Task failed:", e)

    print("All demo completed")
    print("Stats:", service.get_stats())
    service.shutdown()


if __name__ == "__main__":
    main()
