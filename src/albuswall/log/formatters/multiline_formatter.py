#
""""""

import logging

class MultilineFormatter(logging.Formatter):
    """
    让多行日志的每一行都带上相同的日志头（时间、logger、级别等）。
    """

    def __init__(self, fmt=None, datefmt=None, style='%', header_fmt=None):
        super().__init__(fmt=fmt, datefmt=datefmt, style=style)
        # 如果未单独指定 header_fmt，就从完整 fmt 中去掉 %(message)s 部分
        if header_fmt is None:
            header_fmt = (fmt or '').replace('%(message)s', '')
        self.header_formatter = logging.Formatter(header_fmt, datefmt=datefmt, style=style)

    def format(self, record):
        # 1. 生成头部字符串（不包含消息内容）
        header = self.header_formatter.format(record)

        # 2. 获取最终的日志消息文本（参数已经替换）
        message = record.getMessage()

        # 3. 如果消息为空，直接返回头部
        if not message:
            return header

        # 4. 按行拆分消息，每一行都加上头部
        #    使用 split('\n') 保留末尾可能的空行
        lines = message.split('\n')
        formatted_lines = [header + line for line in lines]

        # 5. 处理异常堆栈（如果有），也为其添加头部
        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            # 异常堆栈可能也是多行的，同样逐行加头部
            exc_lines = record.exc_text.split('\n')
            formatted_lines.extend(header + line for line in exc_lines)

        # 6. 用换行连接所有行
        return '\n'.join(formatted_lines)
