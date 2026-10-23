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
class ThumbnailCleanerConfig:
    """ThumbnailCleaner 的运行时配置。

    ``root`` 由 bootstrap 注入（私有模式 = thumb_root，XDG 模式 =
    xdg_thumbnails_root()），cleaner 自己不读 thumbnail 配置，保持解耦。

    ``enabled=False`` 或 ``root is None`` 时 cleaner 所有操作 no-op。
    """

    enabled: bool = True
    root: Path | None = None
    min_age_seconds: float = 600.0

    def __post_init__(self) -> None:
        if self.min_age_seconds < 0:
            raise ValueError("min_age_seconds must be >= 0")
        if self.enabled and self.root is None:
            raise ValueError("root is required when enabled is True")


@dataclass(frozen=True)
class SessionConfig:
    """会话脏标 + 崩溃恢复策略。"""

    marker_path: Path
    auto_cleanup_on_dirty: bool = True

    def __post_init__(self) -> None:
        if not self.marker_path.name:
            raise ValueError("marker_path must be a file path")
