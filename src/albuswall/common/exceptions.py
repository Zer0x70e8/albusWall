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
