#
""""""

import sys
import logging
import logging.config
from logging.config import fileConfig
from logging.handlers import BufferingHandler
from pathlib import Path


class MemoryCacheHandler(BufferingHandler):
    def __init__(self, target=None, capacity=1024):
        super().__init__(capacity)
        self.target = target or logging.StreamHandler(sys.stdout)  # 默认输出到控制台

    def shouldFlush(self, record):
        return True

    def flush(self):
        """将缓冲区中的所有记录发送到目标处理器，然后清空缓冲区"""
        if not self.buffer:
            return
        for record in self.buffer:
            self.target.handle(record)
        self.buffer.clear()
        # 确保目标处理器自己的缓冲被刷新
        if hasattr(self.target, 'flush'):
            self.target.flush()

logging.getLogger().setLevel(10)

logger = logging.getLogger("myapp")
logger.addHandler(MemoryCacheHandler())

file = Path("/home/skyline/.config/albuswall/log_config.ini")

logger.info("start")

fileConfig(str(file), disable_existing_loggers=False)

logger.info("running...")
logger.handlers[0].flush()

sys.stdout.flush()           # 确保 stdout 内容立即输出
sys.stderr.flush()
logging.shutdown()

print(file.read_text())
print(f"_logger.level = {logger.level}")
print(f"_logger.propagate = {logger.propagate}")
print(f"root handlers = {logging.getLogger().handlers}")
print(f"myapp handlers = {logging.getLogger('myapp').handlers}")
