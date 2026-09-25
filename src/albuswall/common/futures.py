#
""""""

import time
from concurrent.futures import wait, FIRST_COMPLETED, TimeoutError as FutureTimeoutError


def wait_with_timeout(futures, per_task_timeout=None, overall_timeout=None):
    """
    规避 as_completed 的两大坑：
      1. as_completed 只 yield 已完成的 future，配合 fut.result(timeout=) 是死代码；
      2. as_completed 在任务永久 pending 时永不 yield，主线程会卡死。

    返回 list[(future, outcome)]，outcome 是返回值或异常实例（含 TimeoutError）。
    """
    futures = list(futures)
    now = time.monotonic()
    pending = {
        f: (now + per_task_timeout) if per_task_timeout else None
        for f in futures
    }
    overall_deadline = (now + overall_timeout) if overall_timeout else None
    outcomes = []

    while pending:
        now = time.monotonic()
        candidates = [d for d in pending.values() if d is not None]
        if overall_deadline is not None:
            candidates.append(overall_deadline)
        next_wake = min(candidates) if candidates else None
        wait_timeout = max(0.0, next_wake - now) if next_wake else None

        done, _ = wait(pending.keys(),
                       timeout=wait_timeout,
                       return_when=FIRST_COMPLETED)

        for fut in done:
            pending.pop(fut, None)
            try:
                outcomes.append((fut, fut.result()))
            except BaseException as exc:
                outcomes.append((fut, exc))

        now = time.monotonic()

        # 单任务超时
        expired = [f for f, dl in pending.items() if dl is not None and dl <= now]
        for fut in expired:
            pending.pop(fut, None)
            fut.cancel()
            outcomes.append((fut, FutureTimeoutError(
                f"future exceeded per-task timeout of {per_task_timeout}s")))

        # 整体超时
        if overall_deadline is not None and now >= overall_deadline:
            for fut in list(pending):
                pending.pop(fut, None)
                fut.cancel()
                outcomes.append((fut, FutureTimeoutError("overall timeout exceeded")))
            break

    return outcomes
