#
""""""

import logging
import subprocess
from pathlib import Path
from typing import Optional

import psutil

_logger = logging.getLogger(__name__)


def ensure_not_mounted(
        mount_point: Path,
        expected_device: str,
        logger: Optional[logging.Logger] = None,
) -> None:
    """检查挂载点是否已被占用，若已挂载且设备匹配则直接返回，否则抛出异常。"""
    log = logger or _logger
    partitions = psutil.disk_partitions(all=False)
    for p in partitions:
        if Path(p.mountpoint) == mount_point:
            if p.device == expected_device:
                log.debug(
                    f"Already mounted at {mount_point} with device {expected_device}"
                )
                return
            else:
                raise RuntimeError(
                    f"Mount point {mount_point} is already mounted "
                    f"with device {p.device}, expected {expected_device}"
                )


def build_mount_command(
        mount_point: Path,
        mount_target: str,
        os_name: Optional[str] = None,
) -> list[str]:
    """根据操作系统返回挂载命令列表。"""
    if os_name is None:
        import platform
        os_name = platform.system().lower()

    if os_name == "windows":
        # Windows 下挂载点必须是盘符（如 Z:）
        mp_str = str(mount_point)
        if not (len(mp_str) == 3 and mp_str[1] == ':'):
            raise RuntimeError(
                "On Windows, mount_point must be a drive letter (e.g., 'Z:')"
            )
        return ["net", "use", mp_str, mount_target]
    elif os_name in ("darwin", "linux"):
        return ["mount", mount_target, str(mount_point)]
    else:
        raise NotImplementedError(f"Auto-mount not supported boot {os_name}")


def auto_mount(
        mount_point: Path,
        mount_target: str,
        source_path: Path,
        logger: Optional[logging.Logger] = None,
) -> None:
    """执行自动挂载：检查挂载点、验证源目录、执行挂载命令。"""
    log = logger or _logger

    # 检查是否已挂载
    ensure_not_mounted(mount_point, mount_target, logger=log)

    # 验证源路径
    if not source_path.is_dir():
        raise RuntimeError(
            f"Mounted but source_path is not a valid directory: {source_path}"
        )

    # 构建并执行挂载命令
    cmd = build_mount_command(mount_point, mount_target)
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Mount command failed: {e.stderr.strip()}") from e
