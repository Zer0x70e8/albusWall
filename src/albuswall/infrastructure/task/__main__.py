#
"""python -m task_service 运行示例。"""

from __future__ import annotations

import logging
import random
import time

from albuswall.common.exceptions import BackpressureError
from albuswall.infrastructure.task import TaskService


def io_task(task_id, sleep_time):
    print(f"[Thread] Task {task_id} started (sleep {sleep_time:.2f}s)")
    time.sleep(sleep_time)
    print(f"[Thread] Task {task_id} completed")
    return f"thread-{task_id}"


def cpu_task(task_id, n):
    print(f"[Process] Task {task_id} started (compute {n} iterations)")
    total = 0
    for i in range(n):
        total += i * i
    print(f"[Process] Task {task_id} completed")
    return f"process-{task_id}"


def on_bp(evt: dict):
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
        try:
            futures.append(service.submit(
                io_task, idx, random.uniform(0.3, 1.0),
                executor="thread", priority=random.randint(0, 4)))
        except BackpressureError as e:
            print(f"submit rejected by backpressure: {e}")

        try:
            futures.append(service.submit(
                cpu_task, idx, random.randint(1_000_000, 5_000_000),
                executor="process", priority=random.randint(0, 4)))
        except BackpressureError as e:
            print(f"submit rejected by backpressure: {e}")

    for f in futures:
        try:
            f.result()
        except Exception as e:
            print("Task failed:", e)

    print("All tasks completed")
    print("Stats:", service.get_stats())
    service.shutdown()


if __name__ == "__main__":
    main()
