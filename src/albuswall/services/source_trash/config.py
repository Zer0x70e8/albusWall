#
"""SourceTrash 服务的配置值对象。

所有字段都是"已经确定的值"——平台判断、路径计算、config 系统读取
都在 bootstrap 层完成。本模块的 dataclass 只做承载和校验，不做 IO。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SourceTrashConfig:
    """SourceTrashService 的运行时配置。"""

    reconciler_name: str = "source_trash"
    priority: int = 8
    debounce: float = 1.0

    def __post_init__(self) -> None:
        if self.debounce < 0:
            raise ValueError("debounce must be >= 0")
        if not self.reconciler_name:
            raise ValueError("reconciler_name must be non-empty")


@dataclass(frozen=True)
class XdgCleanerConfig:
    """XdgThumbnailCleaner 的运行时配置。

    ``root is None`` 表示禁用——平台不支持 XDG（macOS / Windows）
    或用户显式关闭时，bootstrap 传 None 即可，cleaner 所有操作 no-op。
    """

    root: Path | None = None
    min_age_seconds: float = 600.0

    def __post_init__(self) -> None:
        if self.root is not None and self.min_age_seconds < 0:
            raise ValueError("min_age_seconds must be >= 0")

    @property
    def enabled(self) -> bool:
        return self.root is not None


@dataclass(frozen=True)
class SessionConfig:
    """会话脏标 + 崩溃恢复策略。"""

    marker_path: Path
    auto_cleanup_on_dirty: bool = True

    def __post_init__(self) -> None:
        if not self.marker_path.name:
            raise ValueError("marker_path must be a file path")
