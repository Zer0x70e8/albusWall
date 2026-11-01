#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""构建 ``resources/i18n`` 下的翻译文件（``.ts`` 源 + ``.qm`` 运行期文件）。

为什么需要脚本
=============
界面文案的来源有两类，提取方式不同：

1. **控件 / presenter 里的 ``self.tr(...)``** —— 用 Qt 自带的
   ``pyside6-lupdate`` 从 Python 源码里提取，上下文就是类名；
2. **运行时才确定的文案** —— 设置树的标题 / 标签 / 说明（经
   ``schema_tr``）、model / delegate / 编辑器里的模块级 ``tr()``。它们的
   上下文是固定的字符串常量，lupdate 看不到，只能由本脚本按约定补进
   ``.ts``。

本脚本把两类合并成最终 ``.ts``，再调 ``pyside6-lrelease`` 生成 ``.qm``：

.. code-block:: text

    resources/i18n/
        albuswall_en_US.ts   albuswall_en_US.qm
        albuswall_zh_CN.ts   albuswall_zh_CN.qm

翻译内容
========
已经完成的译文放在 ``scripts/i18n_catalog.py``（纯数据，方便 diff / 翻译
工具接手）。脚本只填「还没有译文」的条目，已有译文（包括在 Qt Linguist 里
手工改过的）不会被覆盖。

用法
====
::

    python scripts/build_i18n.py            # 提取 + 补译文 + 生成 .qm
    python scripts/build_i18n.py --check    # 只检查译文是否齐全（CI 用）

依赖 ``pyside6-lupdate`` / ``pyside6-lrelease``（``PySide6`` 自带，安装在
当前 ``.venv`` 即可）。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Iterable

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
for _path in (str(_ROOT), str(_SRC)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from scripts.i18n_catalog import (  # noqa: E402
    LANGUAGES,
    MANUAL_CONTEXTS,
    TRANSLATIONS,
    schema_strings,
)

#: lupdate 扫描范围（只有 UI 包里才有 self.tr 文案）
_LUPDATE_PATHS = ("src/albuswall/ui",)

#: 生成物目录
_OUT_DIR = _ROOT / "src" / "albuswall" / "resources" / "i18n"


# ══════════════════════════════════════════════════
# 工具
# ══════════════════════════════════════════════════
def _tool(name: str) -> str:
    """找 lupdate / lrelease：优先当前解释器同目录，其次 PATH。"""
    candidate = Path(sys.executable).parent / name
    if candidate.is_file():
        return str(candidate)
    found = shutil.which(name)
    if found:
        return found
    raise SystemExit(
        f"找不到 {name}；它在 PySide6 里（pip install PySide6 后随附）。"
    )


def _run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=_ROOT, check=check, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )


# ══════════════════════════════════════════════════
# 1. 提取（lupdate）→ 骨架 TS
# ══════════════════════════════════════════════════
def _extract_skeleton(work_dir: Path) -> tuple[ET.ElementTree, Path]:
    """用 lupdate 扫 Python 源码，得到「类名上下文 → 消息」的骨架。"""
    target = work_dir / "skeleton.ts"
    result = _run([
        _tool("pyside6-lupdate"),
        "-extensions", "py",
        "-no-obsolete",
        *_LUPDATE_PATHS,
        "-ts", str(target),
    ])
    if not target.is_file():
        raise SystemExit(f"lupdate 没有产出 {target}:\n{result.stdout}")
    return ET.parse(target), target


# ══════════════════════════════════════════════════
# 2. 读取已有译文（手工编辑 / 上次生成的结果）
# ══════════════════════════════════════════════════
def _existing_translations(path: Path) -> dict[tuple[str, str], str]:
    """``(context, source) -> 译文``；只收已完成的译文。"""
    if not path.is_file():
        return {}
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:  # pragma: no cover - 文件被改坏
        print(f"[warn] 忽略无法解析的 {path.name}: {exc}")
        return {}

    result: dict[tuple[str, str], str] = {}
    for context in root.findall("context"):
        name = context.findtext("name") or ""
        for message in context.findall("message"):
            source = message.findtext("source")
            translation = message.find("translation")
            if source is None or translation is None:
                continue
            if translation.get("type") in ("unfinished", "obsolete"):
                continue
            text = translation.text or ""
            if text.strip():
                result[(name, source)] = text
    return result


# ══════════════════════════════════════════════════
# 3. 合并（骨架 + 运行时文案）并补译文
# ══════════════════════════════════════════════════
def _normalize_location(location: ET.Element, base_dir: Path) -> None:
    """把 lupdate 写的相对路径改成仓库内相对路径（``src/albuswall/...``）。"""
    filename = (location.get("filename") or "").strip()
    if not filename:
        return
    candidate = Path(filename)
    if not candidate.is_absolute():
        candidate = (base_dir / candidate)
    try:
        resolved = candidate.resolve()
        location.set("filename", str(resolved.relative_to(_ROOT.resolve())))
    except (OSError, ValueError):
        # 解析不了就保留原样：位置只是给人看的，不影响翻译生效
        return


def _iter_skeleton(
        skeleton: ET.ElementTree,
        base_dir: Path,
) -> Iterable[tuple[str, str, ET.Element | None]]:
    """按骨架顺序产出 ``(context, source, location element)``。"""
    for context in skeleton.getroot().findall("context"):
        name = context.findtext("name") or ""
        for message in context.findall("message"):
            source = message.findtext("source")
            if source is None:
                continue
            location = message.find("location")
            if location is not None:
                _normalize_location(location, base_dir)
            yield name, source, location


def _merge(
        skeleton: ET.ElementTree,
        skeleton_path: Path,
        existing: dict[tuple[str, str], str],
        language: str,
        *,
        refresh: bool = False,
) -> tuple[ET.Element, int]:
    """生成目标语言的 ``<TS>`` 根节点；返回 (root, 未翻译条数)。"""
    # context -> [(source, location_element)]
    contexts: dict[str, list[tuple[str, ET.Element | None]]] = {}
    for name, source, location in _iter_skeleton(
            skeleton, skeleton_path.parent):
        contexts.setdefault(name, []).append((source, location))

    # 运行时文案（schema_tr / 模块级 tr）追加到对应上下文
    for name, sources in MANUAL_CONTEXTS.items():
        bucket = contexts.setdefault(name, [])
        known = {source for source, _ in bucket}
        for source in sources:
            if source not in known:
                bucket.append((source, None))
    # 设置树文案只在「设置页」这个上下文里出现
    bucket = contexts.setdefault("SettingSchema", [])
    known = {source for source, _ in bucket}
    bucket.extend(
        (source, None) for source in schema_strings() if source not in known
    )

    catalog = TRANSLATIONS.get(language, {})
    root = ET.Element("TS", {"version": "2.1", "language": language})
    missing = 0

    for name, messages in contexts.items():
        context_el = ET.SubElement(root, "context")
        ET.SubElement(context_el, "name").text = name
        for source, location in messages:
            message_el = ET.SubElement(context_el, "message")
            if location is not None:
                message_el.append(location)
            ET.SubElement(message_el, "source").text = source
            previous = "" if refresh else existing.get((name, source), "")
            text = previous or catalog.get(source) or ""
            translation_el = ET.SubElement(message_el, "translation")
            if text:
                translation_el.text = text
            else:
                # 源文本身就是目标语言（或尚未翻译）：留空，Qt 回退到源文
                translation_el.set("type", "unfinished")
                if _needs_translation(source, language):
                    missing += 1
    return root, missing


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text)


def _needs_translation(source: str, language: str) -> bool:
    """该语言下这条源文是否**应该**有译文。

    仓库里的源文是中英混排的：英文源文在 ``en_US`` 下就是成品，中文源文在
    ``zh_CN`` 下就是成品；反过来才需要译文。
    """
    if language.startswith("zh"):
        return not _has_cjk(source)
    return _has_cjk(source)


# ══════════════════════════════════════════════════
# 4. 写 .ts + 编译 .qm
# ══════════════════════════════════════════════════
def _write_ts(root: ET.Element, path: Path) -> None:
    ET.indent(root, space="    ")
    body = ET.tostring(root, encoding="unicode")
    header = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<!DOCTYPE TS>\n'
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(header + body + "\n", encoding="utf-8")


def _compile(ts_path: Path) -> Path:
    qm_path = ts_path.with_suffix(".qm")
    result = _run([
        _tool("pyside6-lrelease"),
        str(ts_path),
        "-qm", str(qm_path),
    ], check=False)
    if not qm_path.is_file():
        raise SystemExit(f"lrelease 失败（{ts_path.name}）:\n{result.stdout}")
    return qm_path


# ══════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════
def build(check_only: bool = False, refresh: bool = False) -> int:
    with tempfile.TemporaryDirectory(prefix="albuswall-i18n-") as tmp:
        skeleton, skeleton_path = _extract_skeleton(Path(tmp))

    exit_code = 0
    for language in LANGUAGES:
        ts_path = _OUT_DIR / f"albuswall_{language}.ts"
        existing = _existing_translations(ts_path)
        root, missing = _merge(
            skeleton, skeleton_path, existing, language, refresh=refresh,
        )

        if check_only:
            print(
                f"[check] {language}: "
                f"{'OK' if not missing else f'{missing} 条缺译文'}"
            )
            if missing:
                exit_code = 1
            continue

        _write_ts(root, ts_path)
        qm_path = _compile(ts_path)
        print(
            f"[i18n] {ts_path.relative_to(_ROOT)}  "
            f"->  {qm_path.relative_to(_ROOT)}"
            f"  (缺译文 {missing} 条)"
        )
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true",
        help="只检查译文是否齐全，不写文件",
    )
    parser.add_argument(
        "--refresh", action="store_true",
        help="用词表覆盖 .ts 里已有的译文（默认只补缺失条目）",
    )
    args = parser.parse_args(argv)
    return build(check_only=args.check, refresh=args.refresh)


if __name__ == "__main__":
    sys.exit(main())


# 供其它构建脚本 / 测试复用的入口（避免 import 时执行 main）
__all__ = ["build", "main", "schema_strings"]
