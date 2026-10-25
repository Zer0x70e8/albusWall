"""把 v1 的 ``dynamic.keymap`` JSON 一次性迁移成 ``[shortcuts]`` 段。

用法::

    python -m albuswall.ui.builtin.keymap.tools.migrate_keymap \
        --keymap ~/.config/albuswall/keymap.json \
        --config ~/.config/albuswall/albuswall.ini

迁移后删掉旧的 keymap.json 即可。本模块**不在运行期读路径上**：正常启动
只读 ``[shortcuts]``。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path

from ..bindings import normalize_key, render_ops
from ..model import Binding

__all__ = ["build_bindings_from_legacy", "migrate_file", "main"]

SECTION = "shortcuts"


def _add(table: dict[str, list[Binding]], key: str, binding: Binding) -> None:
    try:
        norm = normalize_key(key)
    except ValueError:
        return
    if not norm:
        return
    bucket = table.setdefault(norm, [])
    if binding not in bucket:
        bucket.append(binding)


def build_bindings_from_legacy(
        payload: Mapping, defaults: Mapping[str, Iterable[Binding]],
) -> dict[str, tuple[Binding, ...]]:
    """旧 JSON → ``{键: (绑定, ...)}``（纯函数，测试直接调用）。"""
    table: dict[str, list[Binding]] = {
        key: list(bindings) for key, bindings in defaults.items()
    }
    fmt = int(payload.get("format", 1) or 1)
    if fmt >= 2:
        for key, action_ids in (payload.get("removals") or {}).items():
            try:
                norm = normalize_key(key)
            except ValueError:
                continue
            dropped = {str(a) for a in (action_ids or ())}
            table[norm] = [b for b in table.get(norm, []) if b.action not in dropped]
        for key, entries in (payload.get("additions") or {}).items():
            for entry in entries or ():
                try:
                    _add(table, key, Binding.from_raw(entry))
                except ValueError:
                    continue
    else:
        for action_id, key in (payload.get("bindings") or {}).items():
            target = str(action_id)
            for existing in list(table):
                table[existing] = [b for b in table[existing] if b.action != target]
            if key:
                _add(table, str(key), Binding(target))
    # 保留空元组：它表示「用户显式解绑了这个默认键」，迁移时必须写成空值。
    return {key: tuple(items) for key, items in table.items()}


def migrate_file(
        keymap_path: Path,
        config_path: Path,
        *,
        defaults: Mapping[str, Iterable[Binding]] | None = None,
        dry_run: bool = False,
) -> dict[str, tuple[Binding, ...]]:
    payload = json.loads(Path(keymap_path).read_text(encoding="utf-8"))
    defaults = defaults or _load_defaults()
    table = build_bindings_from_legacy(payload, defaults)
    edits = {
        key: render_ops(ops) for key, ops in table.items()
        if ops != defaults.get(key, ())
    }
    if dry_run:
        print(json.dumps(edits, ensure_ascii=False, indent=2))
        return table

    from albuswall.configue.utils.ini_transform import transform

    path = Path(config_path)
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True) if path.exists() else []
    rendered = list(transform(lines, {SECTION: edits}))
    path.write_text("".join(rendered), encoding="utf-8")
    return table


def _load_defaults() -> dict[str, tuple[Binding, ...]]:
    from albuswall.ui.builtin import shortcuts  # noqa: F401  导入即登记内置动作

    from ..model import registry

    return registry.default_bindings()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keymap", required=True, type=Path,
                        help="旧 keymap.json 路径")
    parser.add_argument("--config", required=True, type=Path,
                        help="主 INI（[shortcuts] 写到这里）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印将写入的内容")
    args = parser.parse_args(argv)

    table = migrate_file(
        args.keymap, args.config, dry_run=args.dry_run)
    print(f"{len(table)} keys migrated to [{SECTION}]"
          + (" (dry run)" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
