#
""""""

import re
import os
from pathlib import Path
from typing import Iterator, Optional, Union

from albuswall.dto.thumbnail import ThumbSpec  # ← 新增

_SPEC_SUFFIX: dict[str, str] = {}  # 留空，后缀由 fmt 参数决定


def is_valid_mount_point(mount_point: str) -> bool:
    """检查挂载点字符串是否为合法格式（不检查是否已挂载或目录存在）"""
    if not mount_point or not mount_point.strip():
        return False

    mp = mount_point.strip()

    # Windows 盘符：如 C:、C:\、D:、D:\ （盘符后可选反斜杠）
    if re.match(r'^[A-Za-z]:\\?$', mp):
        return True

    # Windows UNC 路径：\\server\share 或 \\server\share\subdir
    if mp.startswith('\\\\'):
        # 简单校验至少包含服务器名和共享名
        parts = mp.strip('\\').split('\\')
        return len(parts) >= 2 and all(part for part in parts[:2])

    # Unix/Linux 绝对路径：以 / 开头
    if mp.startswith('/'):
        # 可以进一步检查是否包含非法字符，但基本足够
        return True

    return False


def iter_files_depth_first(
        root: Union[str, Path],
        max_depth: Optional[int] = None,
        include_dirs: bool = True,
        include_files: bool = True,
        follow_symlinks: bool = False
) -> Iterator[Path]:
    """
    深度优先遍历文件夹，返回生成器。

    参数:
        root: 起始目录路径（字符串或 pathlib.Path）
        max_depth: 最大递归深度（相对于 root 的层级）。None 表示无限制。
                   例如：max_depth=0 只返回 root 本身；
                         max_depth=1 返回 root 及其直接子项；
                         max_depth=2 再深入一层，依此类推。
        include_dirs: 是否产出目录路径（默认 True）
        include_files: 是否产出文件路径（默认 True）
        follow_symlinks: 是否跟随符号链接（默认 False，避免循环）

    产出:
        pathlib.Path 对象，按深度优先顺序。
    """
    root = Path(root)

    def _walk(current: Path, depth: int) -> Iterator[Path]:
        # 检查是否超出深度限制
        if max_depth is not None and depth > max_depth:
            return

        # 根据参数决定是否产出当前目录本身
        if include_dirs and depth >= 0:
            yield current

        # 扫描当前目录
        try:
            with os.scandir(current) as it:
                entries = list(it)  # 先获取所有条目，以便排序（可选）
                # 可按名称排序，保证遍历顺序稳定（可选）
                entries.sort(key=lambda e: e.name)

                for entry in entries:
                    # 如果是目录
                    if entry.is_dir(follow_symlinks=follow_symlinks):
                        # 递归进入子目录（深度+1）
                        yield from _walk(Path(entry.path), depth + 1)
                    # 如果是文件
                    elif entry.is_file(follow_symlinks=follow_symlinks):
                        if include_files:
                            yield Path(entry.path)
                    # 其他类型（如符号链接、特殊文件）可按需处理
                    # 此处忽略
        except PermissionError:
            # 遇到权限错误时跳过该目录
            pass

    # 从根目录开始，深度为 0
    yield from _walk(root, 0)


def resolve_asset_source(
        *,
        source_id: Optional[int],
        source_path: Optional[str],
        file_path: str,
) -> Path:
    """把 (source_id / source_path, file_path) 解析为绝对文件路径。

    约定：
      - source_path 是源根目录（sources 表里的 path 列，绝对路径）
      - file_path   是相对根目录的路径
      - 若 file_path 本身已是绝对路径，直接返回
      - 若 source_path 为空，返回 file_path（交给调用方判断是否 is_file）

    source_id 目前只用于日志/未来扩展；保留参数以便将来在多源场景下
    按 id 查注册表替换 source_path。
    """
    if not file_path:
        # 让上层拿到一个一定 not is_file() 的值
        return Path(os.devnull)

    p = Path(file_path)
    if p.is_absolute():
        return p

    if not source_path:
        # 没有根目录：视作相对当前工作目录，交由上层 is_file 判定
        return p

    return Path(source_path) / p


def build_thumb_paths(
        *,
        uuid: str,
        version: int | str,
        fmt: str,
) -> tuple[str, dict[ThumbSpec, str]]:
    """构造缩略图相对路径。

    Returns:
        (base_rel, spec_rels)

        base_rel  —— 形如 "v1/ab/<uuid>"，spec 文件的父目录（相对 thumb_root）
        spec_rels —— {"small": "small.webp", "medium": "medium.webp", ...}

    布局设计：
        <thumb_root>/v1/ab/<uuid>/small.webp
        <thumb_root>/v1/ab/<uuid>/medium.webp
        <thumb_root>/v1/ab/<uuid>/large.webp
    用 uuid 的前两位做一级散列，避免单目录塞太多文件。
    """
    if not uuid:
        raise ValueError("uuid must be non-empty")

    v = f"v{version}" if isinstance(version, int) else str(version)
    shard = uuid[:2].lower()
    base_rel = f"{v}/{shard}/{uuid}"

    ext = (fmt or "webp").lower().lstrip(".")
    if ext == "jpg":
        ext = "jpeg"

    # noinspection string-conversion-without-dunder-method
    spec_rels: dict[ThumbSpec, str] = {
        spec: f"{spec.value}.{ext}"  # ← 用 .value，避免 3.12 Enum 格式化
        for spec in (ThumbSpec.SMALL, ThumbSpec.MEDIUM, ThumbSpec.LARGE)
    }
    return base_rel, spec_rels
