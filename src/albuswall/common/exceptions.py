#
""""""

class BackpressureError(RuntimeError):
    """
    任务因背压被拒绝。
    调用方应据此降速 (退避重试 / 拒绝上游请求 / 丢低优先级任务)。
    """

    def __init__(self, reason: str, queue_size: int, max_queue_size: int):
        super().__init__(
            f"Backpressure triggered: {reason} "
            f"(queue={queue_size}/{max_queue_size})"
        )
        self.reason = reason
        self.queue_size = queue_size
        self.max_queue_size = max_queue_size


class TaskServiceShutdownError(RuntimeError):
    """TaskService 已关闭（或正在关闭），这次的提交不会被执行。

    刻意继承 ``RuntimeError``：既有的 ``except RuntimeError`` 不需要改，
    需要区分"关闭"与其它运行时错误时再精确捕获本类。

    两条通道（与 ``BackpressureError`` 同构）：

    1. **同步抛出**：``submit()`` 时服务已关闭，立即抛出，没有 ``Future``；
    2. **由 Future 携带**：任务已入队、随后服务被 ``shutdown()`` 丢弃，
       错误在 ``future.result()`` / ``future.exception()`` 时感知。

    语义上「背压 = 拒绝」「shutdown = 丢弃」，两者都不保证任务被执行。
    """
