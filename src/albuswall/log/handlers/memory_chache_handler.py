#
""""""

import sys
from logging.handlers import BufferingHandler

class MemoryCacheHandler(BufferingHandler):
    def __init__(self, stream=None, capacity=1024):
        super().__init__(capacity)
        self.stream = stream or sys.stdout  # 直接保存流对象，而不是 StreamHandler

    def flush(self):
        pass
