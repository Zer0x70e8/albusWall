#
"""设置结构的**展示策略**（CLI 侧纯函数）。

为什么策略不在 ``configue`` 里
------------------------------
``configue.schema`` 是「内省 + 批量写回」层：它回答「有哪些配置、现在是什么值、
会写到哪个文件」。而「这个值该用什么控件显示 / 滑块范围多少 / 哪些算高级」
是**展示决策**，唯一消费者是命令行（将来的设置页走 ``@configclass`` + ``tree``
+ ``flush``，不经过 ``dump_schema_dict``）。策略留在配置层会让两边都背负对方
的语义，所以迁到这里。

形态约定
--------
- **数据是 dict**：``dump_schema_dict`` 的输出就是普通 dict，本模块只读它、
  往里补展示键，不引入中间对象。
- **策略是纯函数**：``infer_widget`` / ``apply_present`` / ``format_text`` 都
  不读 ``Configue``、不读全局配置、不碰 Qt、无副作用（``apply_present`` 就地
  mutate 并返回同一个 dict，调用方看得见明确返回值）。
- **手写覆盖是表，不是状态机**：:data:`PRESENT_OVERRIDES` 是模块级 dict，
  :func:`register_present_override` 只往里写，不触发任何计算；合并发生在
  :func:`apply_present` 里。

本模块**不** import ``albuswall.configue``：它只认 dict 的键。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Iterator

__all__: list[str] = []

#: 值类别（**不是** Qt 控件名）：CLI 拿它决定怎么排版提示，设置页自己再映射。
WIDGETS: tuple[str, ...] = (
    "bool", "int", "float", "text", "path", "choice", "list", "dict", "auto",
)

#: ``advanced`` 分组的约定 id（``advanced`` / ``advanced.xxx``）。
_ADVANCED_ID = "advanced"


# ══════════════════════════════════════════════════════════════
# 值类别推导（纯函数）
# ══════════════════════════════════════════════════════════════

#: ``configue.schema.type_name`` 的标量输出 → 值类别。键是**完整**的类型名。
_SCALAR_WIDGETS: dict[str, str] = {
    "bool": "bool",
    "int": "int",
    "float": "float",
    "str": "text",
    "Path": "path",
}


def _split_optional(type_text: str) -> str:
    """剥掉 ``" | None"``；多分支联合（``"int | str"``）原样返回。

    ``dump_schema_dict`` 的 ``type`` 字段是 :func:`configue.schema.type_name`
    的输出格式：``"int"`` / ``"int | None"`` / ``"list[str]"`` / ``"Path"``。
    """
    if "|" not in type_text:
        return type_text
    parts = [
        part.strip() for part in type_text.split("|")
        if part.strip() and part.strip() != "None"
    ]
    return parts[0] if len(parts) == 1 else type_text


def infer_widget(type_name: str, choices: Sequence = ()) -> str:
    """类型字符串 + 选项 → 值类别提示（见 :data:`WIDGETS`）。

    输入来自 ``dump_schema_dict`` 的 ``"type"`` / ``"choices"`` 字段，
    不读任何配置状态。纯函数。``choices`` 非空优先于类型（``Literal`` /
    ``Enum`` 的选项就是事实，不必再看内层类型）。

    Args:
        type_name: 形如 ``"int"`` / ``"bool"`` / ``"Path"`` /
            ``"int | None"`` / ``"list[str]"`` 的类型名。
        choices: 声明推导出的选项（``Literal`` 参数 / ``Enum`` 成员值）。

    Returns:
        ``"bool"`` / ``"int"`` / ``"float"`` / ``"text"`` / ``"path"`` /
        ``"choice"`` / ``"list"`` / ``"dict"`` / ``"auto"`` 之一。
    """
    if choices:
        return "choice"

    base = _split_optional(str(type_name or "").strip())
    scalar = _SCALAR_WIDGETS.get(base)
    if scalar is not None:
        return scalar
    if base in ("list", "tuple", "set") or base.startswith(
            ("list[", "tuple[", "set[")
    ):
        return "list"
    if base == "dict" or base.startswith("dict["):
        return "dict"
    return "auto"


# ══════════════════════════════════════════════════════════════
# 手写覆盖表（数据，不是状态）
# ══════════════════════════════════════════════════════════════

#: ``(section, key)`` → 展示覆盖字段。只在 :func:`apply_present` 里被读。
PRESENT_OVERRIDES: dict[tuple[str, str], dict[str, Any]] = {}

#: 覆盖表里认的键（顺序即 ``apply_present`` 的合并顺序）。
_OVERRIDE_KEYS: tuple[str, ...] = (
    "widget", "minimum", "maximum", "step", "choice_labels", "advanced",
    "label", "description",
)


def register_present_override(
        section: str,
        key: str,
        *,
        widget: str | None = None,
        minimum: float | None = None,
        maximum: float | None = None,
        step: float | None = None,
        choice_labels: Mapping[str, str] | None = None,
        advanced: bool | None = None,
        label: str | None = None,
        description: str | None = None,
) -> None:
    """登记展示覆盖（CLI / 未来设置页共用）。

    不读配置、不建树；只把 ``(section, key)`` → 覆盖字段记进
    :data:`PRESENT_OVERRIDES`，dump 后由 :func:`apply_present` 合并。
    只登记**给了值**的字段（``None`` 表示「不关心，用推导值」），所以同一
    叶子可以分多次登记不同字段。

    Args:
        section: 配置 section（dump 的 ``section`` 字段）。
        key: 配置键（dump 的 ``config_key`` 字段）。
    """
    entry = PRESENT_OVERRIDES.setdefault((str(section), str(key)), {})
    overrides = {
        "widget": widget,
        "minimum": minimum,
        "maximum": maximum,
        "step": step,
        "choice_labels": choice_labels,
        "advanced": advanced,
        "label": label,
        "description": description,
    }
    for name in _OVERRIDE_KEYS:
        value = overrides[name]
        if value is None:
            continue
        # 选项文案是映射：拷一份，登记后就与调用方的 dict 脱钩
        entry[name] = dict(value) if name == "choice_labels" else value


def _override_for(item: Mapping[str, Any]) -> Mapping[str, Any]:
    """取这个叶子的覆盖字段（没有就是空表）。"""
    return PRESENT_OVERRIDES.get(
        (str(item.get("section") or ""), str(item.get("config_key") or "")),
        {},
    )


# ══════════════════════════════════════════════════════════════
# 展示策略合并（纯函数，就地 mutate 并返回同一个 dict）
# ══════════════════════════════════════════════════════════════

def _is_advanced_id(group_id: str) -> bool:
    """``advanced`` 的约定 id（``advanced`` / ``advanced.xxx``）。"""
    return group_id == _ADVANCED_ID or group_id.startswith(f"{_ADVANCED_ID}.")


def _item_advanced(item: Mapping[str, Any]) -> bool:
    """叶子是否落在高级分组里（按 dump 的 ``group`` / ``section`` 约定）。"""
    return _is_advanced_id(str(item.get("group") or "")) or _is_advanced_id(
        str(item.get("section") or ""),
    )


def _apply_item(item: dict[str, Any]) -> None:
    """给一个叶子补 ``widget`` / ``constraints`` / ``choice_labels`` / 高级标记。"""
    override = _override_for(item)

    widget = override.get("widget")
    item["widget"] = (
        str(widget) if widget
        else infer_widget(str(item.get("type") or ""), item.get("choices") or ())
    )

    constraints = {
        name: override[name]
        for name in ("minimum", "maximum", "step")
        if override.get(name) is not None
    }
    item["constraints"] = constraints or None
    item["choice_labels"] = dict(override.get("choice_labels") or {})
    item["advanced"] = (
        bool(override["advanced"]) if "advanced" in override
        else _item_advanced(item)
    )

    for text_key in ("label", "description"):
        if text_key in override:
            item[text_key] = str(override[text_key])


def _apply_group(node: dict[str, Any]) -> None:
    """递归给分组补高级标记、给叶子补展示字段。"""
    node["advanced"] = _is_advanced_id(
        str(node.get("key") or node.get("id") or ""),
    ) or _is_advanced_id(str(node.get("id") or ""))
    for item in node.get("items") or ():
        _apply_item(item)
    for child in node.get("groups") or ():
        _apply_group(child)


def _flatten(node: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """按前序遍历摊平分组树里的叶子。"""
    yield from node.get("items") or ()
    for child in node.get("groups") or ():
        yield from _flatten(child)


def _grouping(
        items: Sequence[dict[str, Any]],
) -> tuple[dict[str, list[str]], list[str]]:
    """按 ``target_file`` 分组并收集 ``unbound``（与 configue 同形）。"""
    by_file: dict[str, list[str]] = {}
    unbound: list[str] = []
    for item in items:
        target = item.get("target_file")
        if target is None:
            unbound.append(item.get("key"))
        else:
            by_file.setdefault(str(target), []).append(item.get("key"))
    return by_file, unbound


def _strip_advanced(node: dict[str, Any]) -> dict[str, Any] | None:
    """丢掉高级子树 / 高级叶子，并按留下的叶子重算 ``item_count``。"""
    if node.get("advanced"):
        return None
    children = [
        kept for kept in (_strip_advanced(child) for child in node.get("groups") or ())
        if kept is not None
    ]
    items = [item for item in node.get("items") or () if not item.get("advanced")]
    kept_node = dict(node)
    kept_node["items"] = items
    kept_node["groups"] = children
    kept_node["item_count"] = sum(
        [len(items)] + [child["item_count"] for child in children]
    )
    return kept_node


def _drop_advanced(dump: dict[str, Any]) -> None:
    """``show_advanced=False``：过滤高级子树，并重算派生字段。"""
    groups = [
        kept for kept in (_strip_advanced(group) for group in dump.get("groups") or ())
        if kept is not None
    ]
    items: list[dict[str, Any]] = []
    for group in groups:
        items.extend(_flatten(group))
    by_file, unbound = _grouping(items)
    dump["groups"] = groups
    dump["items"] = items
    dump["by_file"] = by_file
    dump["unbound"] = unbound
    dump["summary"] = {
        "group_count": sum(1 for _node in _iter_groups(groups)),
        "item_count": len(items),
        "file_count": len(by_file),
    }


def _iter_groups(groups: Sequence[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    for group in groups:
        yield group
        yield from _iter_groups(group.get("groups") or ())


def apply_present(
        dump: dict[str, Any], *, show_advanced: bool = True,
) -> dict[str, Any]:
    """把展示策略合并进 dump（原地 mutate 并返回同一个 dict）。

    - 每个叶子补 ``"widget"``（由 :func:`infer_widget` 从 ``type`` /
      ``choices`` 推导，再被 :data:`PRESENT_OVERRIDES` 覆盖）；
    - 每个叶子补 ``"constraints"`` / ``"choice_labels"``（仅当覆盖表里有；
      没有时分别是 ``None`` / ``{}``，与旧 dump 同形）；
    - 分组补 ``"advanced"``（覆盖表无法表达分组，按 ``advanced`` 约定 id 判）；
      叶子补 ``"advanced"``（覆盖表优先，否则看所在分组）；
    - ``show_advanced=False`` 时过滤掉高级子树，并按留下的叶子重算
      ``items`` / ``by_file`` / ``unbound`` / ``summary``。

    被忽略/不认识的键原样保留：本函数只**加**展示字段，不改事实字段。

    Args:
        dump: ``dump_schema_dict`` 的返回值（tree 形态）；也接受只含
            ``items`` 的形态（例如 ``{"items": [...]}``）。
        show_advanced: ``False`` 时只留基础项（``--view=basic``）。

    Returns:
        同一个 ``dump`` 对象（便于链式调用 / 就地生效）。
    """
    for group in dump.get("groups") or ():
        _apply_group(group)
    for item in dump.get("items") or ():
        _apply_item(item)

    if not show_advanced:
        _drop_advanced(dump)
    return dump


# ══════════════════════════════════════════════════════════════
# 视图装配（纯函数，只吃 dict）
# ══════════════════════════════════════════════════════════════

def by_file_view(dump: dict[str, Any]) -> dict[str, Any]:
    """``--view=by_file``：按归属文件重组叶子（顺序 = 首次出现顺序）。"""
    files: dict[str, list[dict[str, Any]]] = {}
    unbound: list[dict[str, Any]] = []
    for item in dump.get("items") or ():
        target = item.get("target_file")
        if target is None:
            unbound.append(item)
        else:
            files.setdefault(str(target), []).append(item)
    return {
        "files": [
            {"file": path, "items": items} for path, items in files.items()
        ],
        "unbound": unbound,
    }


def _item_flags(item: Mapping[str, Any]) -> str:
    """叶子行的标记集合（``[dirty]`` / ``[readonly]`` …）。"""
    flags: list[str] = []
    if not item["editable"]:
        flags.append("fixed")
    if item["readonly"]:
        flags.append("readonly")
    if item["dirty"]:
        flags.append("dirty")
    if not item["present"]:
        flags.append("missing")
    if item["read_error"]:
        flags.append("read-error")
    return " ".join(f"[{flag}]" for flag in flags)


def _render_item_lines(
        item: dict[str, Any],
        prefix: str,
        is_last: bool,
) -> list[str]:
    """一个叶子：一行标题 + 四行缩进元信息（widget / default / file / origin）。"""
    branch = "└─ " if is_last else "├─ "
    detail_prefix = prefix + ("   " if is_last else "│  ")
    head = (
        f"{prefix}{branch}{item['label']} = {item['value_repr']}  "
        f"({item['path']}, {item['type']})"
    )
    flags = _item_flags(item)
    if flags:
        head += f"  {flags}"

    details = [
        f"widget={item['widget']}",
        f"default={item['default']!r}",
        f"file={item['target_file'] or '<unbound>'}",
        f"origin={item['origin_file'] or '-'}",
    ]
    if item["read_error"]:
        details.append(f"error={item['read_error']}")

    return [head] + [f"{detail_prefix}   {line}" for line in details]


def _render_group_lines(
        node: dict[str, Any],
        prefix: str,
        is_last: bool,
) -> list[str]:
    """一个分组：标题 + 递归渲染子分组与叶子（按 order 混排）。"""
    branch = "└─ " if is_last else "├─ "
    title = node["title"] or node["key"]
    head = f"{prefix}{branch}{title} [{node['key']}] ({node['item_count']} items)"
    if node["description"]:
        head += f" — {node['description']}"
    if node["file"]:
        head += f"  file={node['file']}"

    child_prefix = prefix + ("   " if is_last else "│  ")
    entries: list[tuple[str, dict[str, Any]]] = [
        ("group", child) for child in node["groups"]
    ] + [("item", item) for item in node["items"]]

    lines = [head]
    for index, (kind, entry) in enumerate(entries):
        entry_last = index == len(entries) - 1
        if kind == "group":
            lines.extend(_render_group_lines(entry, child_prefix, entry_last))
        else:
            lines.extend(_render_item_lines(entry, child_prefix, entry_last))
    return lines


def format_text(dump: dict[str, Any], *, show_files: bool = True) -> str:
    """把 dump 渲染成「人敲一下就能看」的树形文本（``--view=text``）。

    排版逐字沿用 ``configue.schema.format_schema_dump``（迁移时原样搬过来），
    只是入参从「配置实例」换成「已 ``apply_present`` 的 dict」——``widget``
    那一行因此由本模块负责。

    Args:
        dump: :func:`apply_present` 之后的 tree 形态 dict。
        show_files: 是否追加末尾的 ``Files:`` 段。
    """
    summary = dump["summary"]
    lines = [
        (
            f"Settings schema: {summary['group_count']} groups, "
            f"{summary['item_count']} items, "
            f"{summary['file_count']} files"
        )
    ]

    groups = dump.get("groups") or []
    if not groups:
        lines.append("(no setting groups registered)")
    else:
        lines.append("")
        for index, group in enumerate(groups):
            lines.extend(
                _render_group_lines(group, "", index == len(groups) - 1),
            )

    if show_files:
        lines.append("")
        lines.append("Files:")
        by_file = dump.get("by_file") or {}
        if not by_file:
            lines.append("  (none)")
        for path, keys in by_file.items():
            lines.append(f"  {path}")
            lines.extend(f"    {key}" for key in keys)
        if dump.get("unbound"):
            lines.append("  <unbound>")
            lines.extend(f"    {key}" for key in dump["unbound"])

    return "\n".join(lines)


def render_view(dump: dict[str, Any], view: str) -> Any:
    """把一个已 ``apply_present`` 的 dump 装配成 ``--view=<view>`` 的返回形态。

    ``text`` 是字符串，``flat`` 是叶子列表，``by_file`` 是按文件分组，其余是
    tree 形态 dict。视图名本身仍是命令行的契约（``--view=text`` 不变）。
    """
    if view == "flat":
        return dump.get("items") or []
    if view == "by_file":
        return by_file_view(dump)
    if view == "text":
        return format_text(dump)
    return dump
