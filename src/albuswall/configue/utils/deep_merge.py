from typing import Any, Dict
from .namespace import Namespace, FrozenNamespace  # 根据实际模块路径导入

def deep_merge_dicts(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """
    深度合并两个字典，override 的优先级更高。

    如果 base 和 override 中同一个键的值都是字典，则递归合并；
    否则直接用 override 中的值覆盖。
    """
    merged = base.copy()
    for key, value in override.items():
        if (
            key in merged
            and isinstance(merged[key], dict)
            and isinstance(value, dict)
        ):
            merged[key] = deep_merge_dicts(merged[key], value)
        else:
            merged[key] = value
    return merged


def merge_namespace_with_dict(namespace: Namespace, data: Dict[str, Any]) -> Namespace:
    """
    将普通字典 data 深度合并到已有的 Namespace 中，data 的值优先。

    该函数不会修改原始 namespace，而是返回一个新的 Namespace 实例。
    如果原始 namespace 是 FrozenNamespace，返回的也将是 FrozenNamespace。

    Args:
        namespace: 要合并的源 Namespace（可能多层嵌套）。
        data: 要覆盖/合并的字典，键必须为字符串。

    Returns:
        合并后的新 Namespace 实例（类型与输入 namespace 保持一致）。
    """
    # 1. 将原 Namespace 转为纯字典
    ns_dict = namespace.to_dict()

    # 2. 深度合并 data 到 ns_dict
    merged_dict = deep_merge_dicts(ns_dict, data)

    # 3. 根据原 Namespace 类型构造新对象
    if isinstance(namespace, FrozenNamespace):
        return FrozenNamespace(**merged_dict)
    else:
        return Namespace(**merged_dict)

def dict_to_namespace(data: Dict[str, Any], frozen: bool = False) -> Namespace:
    """
    将普通字典递归转换为 Namespace（或 FrozenNamespace）。

    Args:
        data: 要转换的字典，键必须为字符串。
        frozen: 若为 True，则返回不可变的 FrozenNamespace 实例；
                否则返回普通的 Namespace 实例。

    Returns:
        转换后的 Namespace / FrozenNamespace 对象，其中嵌套的字典
        也会被递归转换为对应的 Namespace 类型。
    """
    cls = FrozenNamespace if frozen else Namespace
    return cls(**data)
