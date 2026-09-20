#
""""""

from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any

from albuswall.common.enums import FileTypeCheckMode
from albuswall.utils.path import is_valid_mount_point

from .trigger import TriggerConfig


@dataclass
class IngestSourceSyncCandidate:
    """同步候选 DTO，表示一个可自动同步的导入源。"""
    id: int
    source_path: str
    target: Optional[str]
    mount_point: Optional[str]
    auto_mount: bool
    file_type_check: FileTypeCheckMode
    file_types: List[str] = field(default_factory=list)
    subfolder_recursion: bool = False
    subfolder_recursion_depth: Optional[int] = None
    trigger_config: Optional[Dict[str, Any]] = None

    def is_void(self) -> bool:
        """
        判断该同步候选是否不可用（即缺少必要信息）。
        返回 True 表示无法使用，False 表示可以尝试使用。
        """
        # 如果 有记录挂载目标源 路径在挂载后验证，
        # 否则 路径为空或者不是目录，则无效
        if self.target is not None and \
                (not self.source_path.strip() or
                 not Path(self.source_path).is_dir()):
            # print(f"DEBUG is_void: id={self.id}, "
            #       f"target={self.target!r}, "
            #       f"source_path={self.source_path!r}, "
            #       f"exists={Path(self.source_path).is_dir()}")
            return True

        # 2. 文件类型检查模式必须非空
        if self.file_type_check is FileTypeCheckMode.NONE:
            return True

        # 3. 如果启用自动挂载，则挂载点必须是一个有效格式的挂载点标识
        if self.auto_mount and \
                self.target is not None and \
                self.mount_point is not None and \
                not is_valid_mount_point(self.mount_point):
            return True

        # 4. id 应该存在，只是目前我还没维护允许创建空id占位的功能
        if self.id is None:
            return True

        # 5. 挂载点存在后还需要验证挂载后target位置存在，只是这超出这里的能力了

        return False

    def get_allowed_extensions(self) -> set[str]:
        """从源配置获取允许的扩展名集合（统一为小写、带点）"""
        if not self.file_types:
            return set()
        # 归一化：确保每个扩展名以点开头且为小写
        allowed = set()
        for ext in self.file_types:
            ext = str(ext).strip().lower()
            if not ext.startswith('.'):
                ext = '.' + ext
            allowed.add(ext)
        return allowed

    # alias
    is_valid = lambda self: not self.is_void()


@dataclass
class IngestSourceCreate:
    """用于创建导入源的 DTO"""
    title: str
    source_path: str
    description: Optional[str] = None
    target_path: Optional[str] = None
    mount_point: Optional[str] = None
    auto_mount: bool = False
    file_type_check: FileTypeCheckMode = FileTypeCheckMode.SUFFIX
    file_types: List[str] = None  # 默认空列表
    tags: List[str] = None  # 默认空列表
    subfolder_recursion: bool = False
    subfolder_recursion_depth: Optional[int] = None
    trigger_config: Optional[dict] = None


@dataclass
class IngestSourceUpdate:
    """用于更新导入源的 DTO，所有字段可选，仅更新传入的非 None 字段"""
    title: Optional[str] = None
    description: Optional[str] = None
    source_path: Optional[str] = None
    target_path: Optional[str] = None
    mount_point: Optional[str] = None
    auto_mount: Optional[bool] = None
    file_type_check: Optional[FileTypeCheckMode] = None
    file_types: Optional[List[str]] = None
    tags: Optional[List[str]] = None
    subfolder_recursion: Optional[bool] = None
    subfolder_recursion_depth: Optional[int] = None
    trigger_config: Optional[TriggerConfig] = None


from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class SourceScanFinished:
    """
    单个 source 扫描/持久化任务完成事件。

    Attributes:
        source_id:          源 ID。
        task_seq:           Task 序列号，用于区分同一 source 的多次执行/重试。
        scanned_file_count: 扫描到的符合扩展名条件的文件总数。
        new_file_count:     数据库中缺失、需要导入的新文件数。
        inserted_count:     实际写入候选缓存的记录数（一般为 new_file_count，
                            失败/部分成功时可能更少）。
        has_new_content:    是否发现了新内容（new_file_count > 0）。
        error:              若任务失败，携带异常堆栈；成功时为 None。
    """
    source_id: int
    task_seq: int
    scanned_file_count: int
    new_file_count: int
    has_new_content: bool
    inserted_count: int = 0
    error: Optional[str] = None
