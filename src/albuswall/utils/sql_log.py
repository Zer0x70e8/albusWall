#
"""SQL 日志格式化工具，只处理 SQL 语句和参数。"""

import pprint
from typing import Any

_PPRINT_KW = dict(
    indent=2,
    width=120,
    sort_dicts=False,
    compact=False,
)


def format_query(query: str) -> str:
    """把 SQL 语句整理成适合日志输出的多行字符串。

    去掉首尾空白，保留内部换行和缩进。
    """
    if not query:
        return ""
    return query.strip()


def format_params(params: Any) -> str:
    """把 SQL 参数格式化成多行字符串。

    - None -> "()"
    - tuple/list/dict -> pprint 多行
    - 其它 -> str(params)
    """
    if params is None:
        return "()"

    if isinstance(params, (tuple, list, dict, set, frozenset)):
        return pprint.pformat(params, **_PPRINT_KW)

    return str(params)
