#
""""""

import logging

TRACE = 5
logging.addLevelName(TRACE, "TRACE")


# noinspection calling-non-callable,bad-return
def to_levelno(level: int | str) -> int:
    """把 ``int`` 或级别名字规范化为级别数值。

    ``logging.getLevelName(str)`` **不**适合做这件事：

    - Python 3.10 及更早**没有反向查找**，传 ``"WARNING"`` 会得到
      ``"Level WARNING"``（字符串）；
    - Python 3.11+ 才加了 str→int 反向分支。

    这里显式分两路：

    - 3.11+：``logging.getLevelNamesMapping()``
    - 3.10 及更早：``logging._nameToLevel``（与 ``getLevelNamesMapping``
      同源，含 ``addLevelName`` 注册的自定义级别）
    """
    if isinstance(level, int):
        return level
    if not isinstance(level, str):
        raise TypeError(f"level must be int or str, got {type(level).__name__}")

    mapping = getattr(logging, "getLevelNamesMapping", None)
    if mapping is not None:  # Python 3.11+
        levelno = mapping().get(level)
    else:  # Python 3.10 及更早
        levelno = getattr(logging, "_nameToLevel", {}).get(level)

    if levelno is None:
        raise ValueError(f"Unknown log level: {level!r}")
    return levelno
