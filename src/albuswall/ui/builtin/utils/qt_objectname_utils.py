# -*- coding: utf-8 -*-
"""
qt_objectname_utils.py

Qt 控件 objectName 自动命名工具 + CLI。

命名规则（优先级由高到低）：

    1) 【属性归属命名】若某个控件被它的某个祖先控件（父控件 / 祖父控件 …）
       作为 Python 属性持有，则：
           objectName = PascalCase(持有该属性的类名) + PascalCase(属性名)
       例：
           class Content:
               buttons: QWidget
               edit_button: QPushButton
           -> buttons.objectName()    == "ContentButtons"
           -> edit_button.objectName() == "ContentEditButton"

    2) 【兜底：基于 Qt 组件树命名】
           objectName = 父控件类名 + separator + 自身类名
       根控件：objectName = 自身类名

    3) 同名时追加序号：base + separator + 2 / 3 / ...

CLI 用法（无需启动整个项目）：
    python -m utils.qt_objectname_utils path/to/window.py
    python -m utils.qt_objectname_utils path/to/window.py --class MyWindow
    python -m utils.qt_objectname_utils path/to/window.py --style table
    python -m utils.qt_objectname_utils path/to/window.py --no-attr   # 关闭属性归属命名
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QWidget

__all__ = [
    "resolve_class_name",
    "set_widget_object_name",
    "auto_set_object_names",
    "format_object_names",
    "cli_main",
]


# --------------------------------------------------------------------------- #
# 基础工具：字符串与类名
# --------------------------------------------------------------------------- #

def _to_pascal(name: str) -> str:
    """
    把 snake_case / camelCase / 混合风格统一转为 PascalCase。

    例：
        "edit_button" -> "EditButton"
        "buttons"     -> "Buttons"
        "Content"     -> "Content"
        "myButton"    -> "MyButton"
    """
    if not name:
        return name
    if "_" in name:
        parts = [p for p in name.split("_") if p]
        if parts:
            return "".join(p[:1].upper() + p[1:] for p in parts)
    if name[:1].islower():
        return name[:1].upper() + name[1:]
    return name


def resolve_class_name(obj: object, use_meta_class_name: bool = True) -> str:
    """获取对象的类名，优先使用 Qt 元对象类名（如 QPushButton）。"""
    if use_meta_class_name and hasattr(obj, "metaObject"):
        meta = obj.metaObject()  # type: ignore[attr-defined]
        if meta is not None:
            name = meta.className()
            if name:
                return name
    return type(obj).__name__


# --------------------------------------------------------------------------- #
# 属性归属扫描：谁把这个控件当属性持有？
# --------------------------------------------------------------------------- #

def _build_owner_map(root: QWidget) -> Dict[int, Tuple[str, str]]:
    """
    从 root 出发，扫描整棵 Qt 控件树里每个控件的 Python 实例属性 / 类属性，
    找出「某控件被某祖先以某属性名持有」的关系。

    返回：
        { id(widget) : (持有它的 Python 类名, 属性名) }

    说明：
        * 同一控件若被多个属性引用，取最先遇到的（即层级更浅的那个）。
        * 只扫描 Qt 组件树中的控件；树外对象不参与。
        * 实例属性优先于类属性（因为实例属性更贴近真实布局）。
    """
    owner_map: Dict[int, Tuple[str, str]] = {}

    def record(owner_widget: QWidget, attr_name: str, value: object) -> None:
        if isinstance(value, QWidget):
            owner_map.setdefault(
                id(value), (type(owner_widget).__name__, attr_name)
            )

    def scan(w: QWidget) -> None:
        # 1) 实例属性
        try:
            inst_items = list(vars(w).items())
        except TypeError:
            inst_items = []
        for attr_name, value in inst_items:
            record(w, attr_name, value)

        # 2) 类属性（有些项目会把子控件声明为类属性）
        try:
            cls_items = list(vars(type(w)).items())
        except TypeError:
            cls_items = []
        for attr_name, value in cls_items:
            record(w, attr_name, value)

    def walk(w: QWidget) -> None:
        scan(w)
        for child in w.findChildren(
                QWidget, options=Qt.FindChildOption.FindDirectChildrenOnly
        ):
            walk(child)

    walk(root)
    return owner_map


# --------------------------------------------------------------------------- #
# 基础工具：单个控件（保留原 API）
# --------------------------------------------------------------------------- #

def set_widget_object_name(
        widget: QWidget,
        parent_class_name: Optional[str] = None,
        *,
        separator: str = "",
        overwrite: bool = False,
        used_names: Optional[Set[str]] = None,
        use_meta_class_name: bool = True,
) -> str:
    """
    为 **单个** 控件设置 objectName（基于“父类名 + 自身类名”的旧规则）。

    若要使用属性归属命名，请使用 auto_set_object_names()。
    """
    if used_names is None:
        used_names = set()

    existing = widget.objectName()
    if existing and not overwrite:
        used_names.add(existing)
        return existing

    own_class = resolve_class_name(widget, use_meta_class_name)
    base = (
        f"{parent_class_name}{separator}{own_class}"
        if parent_class_name
        else own_class
    )

    name, index = base, 1
    while name in used_names:
        index += 1
        name = f"{base}{separator}{index}"

    widget.setObjectName(name)
    used_names.add(name)
    return name


# --------------------------------------------------------------------------- #
# 超级工具：整棵控件树
# --------------------------------------------------------------------------- #

def auto_set_object_names(
        root: QWidget,
        *,
        separator: str = "",
        overwrite: bool = False,
        include_self: bool = True,
        recursive: bool = True,
        use_meta_class_name: bool = True,
        use_attr_names: bool = True,
) -> Dict[QWidget, str]:
    """
    为 root 及其全部后代自动设置 objectName，并返回 {控件: objectName}。

    命名策略：
        * 先用「属性归属」命名：如果某控件被某个祖先以属性持有，
          名字 = 持有它的类名 + PascalCase(属性名)
        * 否则回退到「父类名 + separator + 自身类名」

    参数：
        use_attr_names: 是否启用属性归属命名（默认为 True）。
                        设为 False 时行为与旧版本完全一致。

    每个控件只被访问一次，不会反复覆盖。
    """
    used: Set[str] = set()
    result: Dict[QWidget, str] = {}

    owner_map: Dict[int, Tuple[str, str]] = (
        _build_owner_map(root) if use_attr_names else {}
    )

    def make_unique(base: str) -> str:
        if base not in used:
            return base
        index = 2
        while f"{base}{separator}{index}" in used:
            index += 1
        return f"{base}{separator}{index}"

    # noinspection shadowing-names
    def visit(node: QWidget, parent_class_name: Optional[str]) -> None:
        own_class = resolve_class_name(node, use_meta_class_name)

        # ---- 计算 base 名 ----
        if node is root:
            # 根控件：自身类名
            base = own_class
        else:
            owner = owner_map.get(id(node))
            if owner is not None:
                owner_cls, attr_name = owner
                base = f"{owner_cls}{_to_pascal(attr_name)}"
            else:
                # 兜底：父类名 + separator + 自身类名
                base = (
                    f"{parent_class_name}{separator}{own_class}"
                    if parent_class_name
                    else own_class
                )

        # ---- 应用 base 名（保留已存在 / 覆盖） ----
        existing = node.objectName()
        if existing and not overwrite:
            used.add(existing)
            result[node] = existing
        else:
            name = make_unique(base)
            used.add(name)
            node.setObjectName(name)
            result[node] = name

        if not recursive:
            return

        for child_widget in node.findChildren(
                QWidget,
                options=Qt.FindChildOption.FindDirectChildrenOnly,
        ):
            visit(child_widget, own_class)

    if include_self:
        visit(root, None)
    else:
        root_class = resolve_class_name(root, use_meta_class_name)
        for child_widget in root.findChildren(
                QWidget,
                options=Qt.FindChildOption.FindDirectChildrenOnly,
        ):
            visit(child_widget, root_class)

    return result


# --------------------------------------------------------------------------- #
# 结果格式化
# --------------------------------------------------------------------------- #

def format_object_names(
        result: Dict[QWidget, str],
        *,
        style: str = "tree",
        show_class: bool = True,
        indent: str = "  ",
        sort: bool = False,
) -> str:
    """
    把 auto_set_object_names 的结果格式化为字符串。

    :param result:    {控件: objectName}
    :param style:     "tree"（层级树）/ "table"（表格）/ "flat"（平铺）
    :param show_class: 是否显示控件类名
    :param indent:    tree 风格的缩进字符串
    :param sort:      是否按 objectName 排序（table / flat 风格）
    :return:          格式化后的多行字符串
    """
    if not result:
        return "(empty)"

    style = style.lower()
    if style == "flat":
        return _format_flat(result, show_class=show_class, sort=sort)
    if style == "table":
        return _format_table(result, show_class=show_class, sort=sort)
    return _format_tree(result, show_class=show_class, indent=indent)


def _format_flat(
        result: Dict[QWidget, str],
        *,
        show_class: bool,
        sort: bool,
) -> str:
    items = list(result.items())
    if sort:
        items.sort(key=lambda kv: kv[1])
    lines: List[str] = []
    for widget, name in items:
        if show_class:
            lines.append(f"{name}  <{resolve_class_name(widget)}>")
        else:
            lines.append(name)
    return "\n".join(lines)


def _format_table(
        result: Dict[QWidget, str],
        *,
        show_class: bool,
        sort: bool,
) -> str:
    rows: List[Tuple[str, str]] = []
    for widget, name in result.items():
        rows.append((name, resolve_class_name(widget) if show_class else ""))
    if sort:
        rows.sort(key=lambda r: r[0])

    name_width = max([len(r[0]) for r in rows] + [len("objectName")])
    lines: List[str] = []
    if show_class:
        cls_width = max([len(r[1]) for r in rows] + [len("class")])
        header = f"{'objectName':<{name_width}}  {'class':<{cls_width}}"
        lines.append(header)
        lines.append("-" * len(header))
        for name, cls in rows:
            lines.append(f"{name:<{name_width}}  {cls:<{cls_width}}")
    else:
        lines.append(f"{'objectName':<{name_width}}")
        lines.append("-" * name_width)
        for name, _ in rows:
            lines.append(f"{name:<{name_width}}")
    return "\n".join(lines)


def _format_tree(
        result: Dict[QWidget, str],
        *,
        show_class: bool,
        indent: str,
) -> str:
    """
    按父子关系渲染层级树。

    注意：只有"同时出现在 result 中"的父控件才会被当作父节点，
    否则该控件会被当作根节点。这样即使只对子树命名，也能正确渲染。
    """
    ids_in_result = {id(w) for w in result}
    children_map: Dict[int, List[QWidget]] = {}
    roots: List[QWidget] = []

    # 保持插入顺序（与 auto_set_object_names 的遍历顺序一致）
    for node in result:
        parent = node.parentWidget()
        if parent is not None and id(parent) in ids_in_result:
            children_map.setdefault(id(parent), []).append(node)
        else:
            roots.append(node)

    lines: List[str] = []

    # noinspection shadowing-names
    def render(node: QWidget, depth: int) -> None:
        name = result[node]
        prefix = indent * depth
        if show_class:
            lines.append(f"{prefix}{name}  <{resolve_class_name(node)}>")
        else:
            lines.append(f"{prefix}{name}")
        for child_node in children_map.get(id(node), []):
            render(child_node, depth + 1)

    for root_node in roots:
        render(root_node, 0)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI：对指定模块文件运行，无需启动整个项目
# --------------------------------------------------------------------------- #

def _find_package_root(path: Path) -> Tuple[Optional[Path], Optional[str]]:
    """
    从目标文件向上找包根目录。

    返回 (package_root, module_name)：
        package_root: 应加入 sys.path 的目录（包的上一级）
        module_name : 点分模块名，如 "albuswall.ui.builtin.window.window"
    如果目标文件不在任何包里，返回 (None, None)。
    """
    parts = [path.stem]
    parent = path.parent
    while (parent / "__init__.py").is_file():
        parts.append(parent.name)
        parent = parent.parent
    if len(parts) == 1:
        return None, None
    parts.reverse()
    return parent, ".".join(parts)


def _load_module_from_path(path: Path):
    """
    从一个 .py 文件路径动态加载模块。

    优先按“包内模块”加载（支持 from .x / from ..pkg 相对导入）；
    若目标文件不在包内，则退回到裸文件加载。
    """
    package_root, module_name = _find_package_root(path)
    fallback_error: Optional[Exception] = None

    if module_name and package_root is not None:
        inserted = False
        if str(package_root) not in sys.path:
            sys.path.insert(0, str(package_root))
            inserted = True
        try:
            return importlib.import_module(module_name)
        except Exception as exc:  # noqa: BLE001
            fallback_error = exc
            if inserted:
                try:
                    sys.path.remove(str(package_root))
                except ValueError:
                    pass

    module_key = f"_qt_objectname_target_{abs(hash(str(path)))}"
    spec = importlib.util.spec_from_file_location(module_key, str(path))
    if spec is None or spec.loader is None:
        raise ImportError(f"无法为 {path} 创建模块 spec")

    loader = spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_key] = module
    try:
        loader.exec_module(module)
    except Exception:  # noqa: BLE001
        if fallback_error is not None:
            raise fallback_error from None
        raise
    return module


def _find_widget_targets(
        module,
) -> Tuple[List[Tuple[str, QWidget]], List[Tuple[str, type]]]:
    """在模块里找出已有的 QWidget 实例和 QWidget 子类。"""
    instances: List[Tuple[str, QWidget]] = []
    classes: List[Tuple[str, type]] = []
    for name, obj in vars(module).items():
        if name.startswith("_"):
            continue
        if isinstance(obj, QWidget):
            instances.append((name, obj))
        elif isinstance(obj, type) and issubclass(obj, QWidget) and obj is not QWidget:
            classes.append((name, obj))
    return instances, classes


def _pick_instance(
        instances: List[Tuple[str, QWidget]],
) -> Tuple[Optional[str], Optional[QWidget]]:
    """优先选顶层窗口实例（没有 parentWidget）。"""
    for name, inst in instances:
        if inst.parentWidget() is None:
            return name, inst
    if instances:
        return instances[0]
    return None, None


# noinspection broad-exception
def _try_instantiate(
        classes: List[Tuple[str, type]],
) -> Tuple[Optional[str], Optional[QWidget]]:
    """尝试实例化某个 QWidget 子类；按名字长度排序，主窗口通常较短。"""
    for name, cls in sorted(classes, key=lambda kv: len(kv[0])):
        try:
            instance = cls()
        except Exception:  # noqa: BLE001
            continue
        if isinstance(instance, QWidget):
            return name, instance
    return None, None


def cli_main(argv: Optional[List[str]] = None) -> int:
    """
    命令行入口：

        python -m utils.qt_objectname_utils path/to/window.py
        python -m utils.qt_objectname_utils path/to/window.py --class MyWindow
        python -m utils.qt_objectname_utils path/to/window.py --style table
        python -m utils.qt_objectname_utils path/to/window.py --no-attr
    """
    parser = argparse.ArgumentParser(
        prog="qt-objectname",
        description="对指定模块文件中的 QWidget 自动命名，并格式化输出。",
    )
    parser.add_argument("module_file", help="要加载的 Python 模块文件路径")
    parser.add_argument(
        "--class", dest="class_name", default=None,
        help="指定 QWidget 子类名（不指定则自动挑选实例或子类）",
    )
    parser.add_argument(
        "--style", default="tree", choices=["tree", "table", "flat"],
        help="输出风格，默认 tree",
    )
    parser.add_argument(
        "--separator", default="",
        help="类名之间的分隔符，默认空串",
    )
    parser.add_argument(
        "--overwrite", action="store_true",
        help="覆盖已有的 objectName（默认保留）",
    )
    parser.add_argument(
        "--no-self", dest="include_self", action="store_false",
        help="不处理根控件自身",
    )
    parser.add_argument(
        "--no-class", dest="show_class", action="store_false",
        help="不显示控件类名",
    )
    parser.add_argument(
        "--sort", action="store_true",
        help="table / flat 风格下按 objectName 排序",
    )
    parser.add_argument(
        "--no-attr", dest="use_attr_names", action="store_false",
        help="关闭属性归属命名，仅按父类名 + 自身类名命名",
    )
    parser.set_defaults(include_self=True, show_class=True, use_attr_names=True)
    args = parser.parse_args(argv)

    path = Path(args.module_file).resolve()
    if not path.is_file():
        print(f"[error] 文件不存在: {path}", file=sys.stderr)
        return 2

    # 需要 QApplication 才能实例化 QWidget
    if QApplication.instance() is None:
        QApplication(sys.argv[:1])

    # 加载目标模块
    try:
        module = _load_module_from_path(path)
    except Exception as exc:  # noqa: BLE001
        print(f"[error] 加载模块失败: {exc}", file=sys.stderr)
        return 1

    # 选择目标控件
    if args.class_name:
        cls = getattr(module, args.class_name, None)
        if cls is None or not isinstance(cls, type) or not issubclass(cls, QWidget) or cls is QWidget:
            found = [
                n for n, o in vars(module).items()
                if isinstance(o, type) and issubclass(o, QWidget) and o is not QWidget
            ]
            print(f"[error] 模块中找不到 QWidget 子类: {args.class_name}", file=sys.stderr)
            if found:
                print("[hint] 该模块中可用的 QWidget 子类:", file=sys.stderr)
                for n in found:
                    print(f"    - {n}", file=sys.stderr)
            else:
                print("[hint] 该模块中没有 QWidget 子类。", file=sys.stderr)
                visible = [n for n in vars(module) if not n.startswith("_")]
                print(f"[hint] 模块可见名字: {', '.join(visible[:30])}", file=sys.stderr)
            return 1
        try:
            # noinspection calling-non-callable
            target = cls()
            target_label = args.class_name
        except Exception as exc:  # noqa: BLE001
            print(f"[error] 实例化 {args.class_name} 失败: {exc}", file=sys.stderr)
            return 1
    else:
        instances, classes = _find_widget_targets(module)
        name, inst = _pick_instance(instances)
        if inst is not None:
            target = inst
            target_label = name or ""
        else:
            name, inst = _try_instantiate(classes)
            if inst is None:
                print(
                    "[error] 模块中找不到可用的 QWidget 实例或子类",
                    file=sys.stderr,
                )
                return 1
            target = inst
            target_label = name or ""

    if target is None:
        print("[error] 未能确定目标控件", file=sys.stderr)
        return 1

    # 运行命名
    result = auto_set_object_names(
        target,
        separator=args.separator,
        overwrite=args.overwrite,
        include_self=args.include_self,
        use_attr_names=args.use_attr_names,
    )

    print(f"# module : {path}")
    print(f"# target : {target_label} ({resolve_class_name(target)})")
    print(f"# count  : {len(result)}")
    print(f"# naming : {'attribute-aware' if args.use_attr_names else 'tree-only'}")
    print(format_object_names(
        result,
        style=args.style,
        show_class=args.show_class,
        sort=args.sort,
    ))
    return 0


if __name__ == "__main__":
    sys.exit(cli_main())
