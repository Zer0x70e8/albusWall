#
""""""

import sys
from logging.handlers import BufferingHandler


class MemoryCacheHandler(BufferingHandler):
    def __init__(self, stream=None, capacity=1024):
        super().__init__(capacity)
        self.stream = stream or sys.stdout  # 直接保存流对象，而不是 StreamHandler

    def flush(self):
        # no-op：重放由 setup_log 显式完成。
        # 不要在这里 clear()，因为 logging.shutdown()（由
        # logging._config.fileConfig 触发）会在重放之前调用它，
        # 把启动期的日志全部吞掉。
        pass

    def close(self):
        # 同上，shutdown() 会先 flush 再 close。
        # 这里不要动 buffer，交给 setup_log 的显式重放。
        pass

    def emit(self, record):
        if len(self.buffer) >= self.capacity:
            # 溢出时不要再交给 flush()，直接丢掉最旧的
            self.buffer.pop(0)
        self.buffer.append(record)
