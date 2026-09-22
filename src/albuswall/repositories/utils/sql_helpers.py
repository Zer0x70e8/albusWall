#
"""
SQL 辅助工具函数，纯函数、无状态。
适合放置在 BaseRepository 所在目录的 utils/ 子文件夹中。
"""


def placeholders(count: int) -> str:
    """生成 ? 占位符字符串，用于 IN 子句，如: placeholders(3) -> '?, ?, ?'。"""
    return ", ".join("?" for _ in range(count))


def filter_dict(data: dict, allowed_keys: set) -> dict:
    """从字典中仅保留允许的键，防止非法字段注入。"""
    return {k: v for k, v in data.items() if k in allowed_keys}


def build_set_clause(updates: dict):
    """
    根据更新字典构造 SET 子句和值列表。
    返回: (set_clause: str, values: list)
    示例:
        clause, vals = build_set_clause({'name': 'foo', 'age': 30})
        # clause: "name = ?, age = ?", vals: ['foo', 30]
    """
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values())
    return set_clause, values
