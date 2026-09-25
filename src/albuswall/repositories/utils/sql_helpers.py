#
"""
SQL 辅助工具函数，纯函数、无状态。
适合放置在 BaseRepository 所在目录的 utils/ 子文件夹中。
"""


def placeholders(count: int) -> str:
    if not isinstance(count, int):
        raise TypeError(f"count must be int, got {type(count).__name__}")
    if count <= 0:
        # 空 IN 子句在 SQL 里是非法的，调用方必须自己短路。
        # 这里主动抛错，避免生成 "IN ()" 这种脏 SQL。
        raise ValueError("placeholders(count) requires count >= 1")
    return ", ".join("?" for _ in range(count))


def filter_dict(data: dict, allowed_keys: set) -> dict:
    if not isinstance(data, dict):
        raise TypeError(f"data must be dict, got {type(data).__name__}")
    if not isinstance(allowed_keys, (set, frozenset)):
        raise TypeError(
            f"allowed_keys must be set/frozenset, got {type(allowed_keys).__name__}"
        )
    return {k: v for k, v in data.items() if k in allowed_keys}


def build_set_clause(updates: dict):
    if not isinstance(updates, dict):
        raise TypeError(f"updates must be dict, got {type(updates).__name__}")
    if not updates:
        # 返回空串 + 空 list，让调用方决定是短路还是报错；
        # 比抛异常更中性，也更方便拼 SELECT。
        return "", []
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values())
    return set_clause, values
