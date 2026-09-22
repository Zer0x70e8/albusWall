# -*- coding: utf-8 -*-
"""
qt_objectname_utils.py

Qt 控件 objectName 自动命名工具。

主要功能：
    遍历一个 QWidget 实例下的全部子组件，并按 "类名 + 属性名" 的方式
    自动设置 objectName。

典型用法：
    from utils.qt_objectname_utils import auto_set_object_names

    # 使用控件自身的类名作为前缀（默认行为）
    names = auto_set_object_names(self)

    # 使用指定实例（比如 self）的类名作为所有控件的前缀
    names = auto_set_object_names(self, class_name_source=self)

    # 关闭驼峰转换，保留 snake_case 属性名
    names = auto_set_object_names(self, camel_case=False)

    print(names)
"""

from __future__ import annotations

from typing import Dict, Iterator, Optional, Set, Tuple

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget

__all__ = ["auto_set_object_names"]


def _to_camel_case(name: str) -> str:
    """
    把 snake_case 转成 CamelCase。
    """
    parts = [p for p in name.split("_") if p]
    if not parts:
        return name
    return "".join(p[:1].upper() + p[1:] for p in parts)


def _resolve_class_name(obj, use_meta_class_name: bool) -> str:
    """获取 obj 的类名，优先使用 Qt 元对象类名。"""
    if use_meta_class_name and hasattr(obj, "metaObject"):
        return obj.metaObject().className()
    return type(obj).__name__


def _iter_widget_attrs(
        obj,
        depth: int = 0,
        max_depth: int = 10,
        visited: Optional[Set[int]] = None,
) -> Iterator[Tuple[QWidget, str]]:
    """
    递归遍历 obj 的 __dict__，产出 (QWidget 实例, 它所在的属性名) 对。

    例如：
        self.okButton = QPushButton()
        -> (okButton 控件, "okButton")
    """
    if visited is None:
        visited = set()

    if id(obj) in visited or depth > max_depth:
        return
    visited.add(id(obj))

    try:
        members = vars(obj)
    except TypeError:
        # C 扩展对象 / 使用了 __slots__，没有 __dict__
        return

    for attr_name, value in list(members.items()):
        if attr_name.startswith("__"):
            continue

        if isinstance(value, QWidget):
            yield value, attr_name
            yield from _iter_widget_attrs(value, depth + 1, max_depth, visited)

        elif isinstance(value, (list, tuple)):
            for item in value:
                if isinstance(item, QWidget):
                    yield item, attr_name

        elif isinstance(value, dict):
            for item in value.values():
                if isinstance(item, QWidget):
                    yield item, attr_name


def auto_set_object_names(
        root: QWidget,
        *,
        class_name_source: Optional[object] = None,
        separator: str = "_",
        overwrite: bool = False,
        include_self: bool = False,
        recursive: bool = True,
        only_named: bool = False,
        use_meta_class_name: bool = True,
        camel_case: bool = True,
) -> Dict[QWidget, str]:
    """
    自动为 root 下的所有子组件设置 objectName。

    :param root:               根控件
    :param class_name_source:  指定用于生成"类名前缀"的实例。
                               为 None 时使用控件自身的类名（默认）；
                               不为 None 时，所有控件都使用该实例的类名作为前缀。
                               例如传入 self（主窗口），则所有子控件都以
                               MainWindow 之类的前缀命名。
    :param separator:          类名与属性名之间的分隔符，默认 "_"
    :param overwrite:          是否覆盖已有的 objectName，默认 False
    :param include_self:       是否也处理 root 自身，默认 False
    :param recursive:          是否递归查找所有后代，默认 True；
                               False 时只处理直接子级
    :param only_named:         只处理能在 __dict__ 中找到变量名的控件，默认 False
    :param use_meta_class_name: 使用 Qt 元对象类名（如 QPushButton），
                               而不是 Python 类名，默认 True
    :param camel_case:         是否把属性名从 snake_case 转成 camelCase，默认 True
    :return:                   {控件: 最终设置的 objectName}
    """
    # 1. 收集 变量名映射：id(控件) -> 属性名
    attr_map: Dict[int, str] = {}
    for widget, attr in _iter_widget_attrs(root):
        attr_map.setdefault(id(widget), attr)

    # 2. 收集所有目标控件
    options = Qt.FindChildOption.FindChildrenRecursively \
        if recursive else Qt.FindChildOption.FindDirectChildrenOnly
    targets = root.findChildren(QWidget, options=options)

    if include_self:
        targets = [root, *targets]

    # 3. 决定"固定前缀"：如果指定了 class_name_source，则使用它的类名
    fixed_prefix: Optional[str] = (
        _resolve_class_name(class_name_source, use_meta_class_name)
        if class_name_source is not None
        else None
    )

    used: Set[str] = set()
    result: Dict[QWidget, str] = {}

    for child in targets:
        # 保留已有名字
        existing = child.objectName()
        if existing and not overwrite:
            used.add(existing)
            result[child] = existing
            continue

        attr = attr_map.get(id(child))
        if only_named and not attr:
            continue

        # 蛇形 -> 驼峰
        if attr and camel_case:
            attr = _to_camel_case(attr)

        # 类名前缀：优先使用 class_name_source，否则使用控件自身类名
        cls_name = (
            fixed_prefix
            if fixed_prefix is not None
            else _resolve_class_name(child, use_meta_class_name)
        )

        base = f"{cls_name}{separator}{attr}" if attr else cls_name

        # 自动去重：如果名字已存在，则追加 _2、_3 ...
        name, index = base, 1
        while name in used:
            index += 1
            name = f"{base}{separator}{index}"

        used.add(name)
        child.setObjectName(name)
        # print(name)
        result[child] = name

    # print(*(i.objectName() for i in result.keys()))
    return result
