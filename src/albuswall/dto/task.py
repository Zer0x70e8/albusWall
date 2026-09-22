#
""""""

# from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Callable, Literal  # , Optional

ExecutorType = Literal["thread", "process"]


@dataclass(order=True)
class Task:
    """优先级队列中的任务项"""
    priority: int
    seq: int
    fn: Callable = field(compare=False)
    args: tuple = field(compare=False)
    kwargs: dict = field(compare=False)
    executor: ExecutorType = field(compare=False)
    # future: Optional[Future] = field(compare=False)
    #
    # def is_void(self):
    #     if self.future is None:
    #         return True
    #     return False
