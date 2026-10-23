#
"""IPC 端点：套接字地址与 token 文件的唯一解析处。

服务端与客户端**都**从这里取地址和密钥，保证两边不可能漂移。
本模块只依赖标准库与 ``albuswall.__about__``，CLI 进程可以直接导入。

平台差异集中在此处，其余代码不出现分支：

* **POSIX**  —— ``AF_UNIX``，地址为 ``$XDG_RUNTIME_DIR/albuswall/ipc.sock``；
  拿不到 ``XDG_RUNTIME_DIR`` 时退到系统临时目录下的 per-uid 子目录。
* **Windows** —— ``AF_PIPE``，地址为 ``\\\\.\\pipe\\albuswall-<user>``。

为什么 POSIX 优先 ``XDG_RUNTIME_DIR``
------------------------------------
该目录由登录会话创建、登出时自动清理，天然带 0700 权限，且不会在
``/tmp`` 上留下可被预创建的符号链接（symlink attack）。

安全边界
--------
* POSIX：``AF_UNIX`` 的访问控制完全靠文件权限——目录 0700、socket 0600、
  token 文件 0600。``multiprocessing.connection`` 把 fd 直接交给
  ``Connection``，拿不到对端凭据，所以**不做** ``SO_PEERCRED`` 校验。
* Windows：命名管道没有「文件权限」这一层，``authkey``（HMAC 挑战应答）
  才是真正的门禁。
"""

from __future__ import annotations

import getpass
import os
import secrets
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from albuswall.__about__ import __title__

__all__ = [
    "Endpoint",
    "EndpointUnavailable",
    "create_or_reuse_token",
    "endpoint",
    "endpoint_address",
    "ipc_dir",
    "read_token",
    "socket_path",
    "token_path",
]

_APP = __title__
_SOCKET_NAME = "ipc.sock"
_TOKEN_NAME = "token"
_DIR_MODE = 0o700
_FILE_MODE = 0o600
_TOKEN_BYTES = 32


class EndpointUnavailable(RuntimeError):
    """拿不到端点（通常是 token 不存在 → 应用没在跑，或权限不足）。"""


@dataclass(frozen=True)
class Endpoint:
    """一次会话的完整端点描述：协议族 + 地址 + 预共享密钥。"""

    family: str
    address: str
    authkey: bytes

    @property
    def is_pipe(self) -> bool:
        return self.family == "AF_PIPE"

    def __str__(self) -> str:
        return f"{self.family}:{self.address}"


# --------------------------------------------------------------------------- #
# 目录 / 文件
# --------------------------------------------------------------------------- #
def _fallback_dir() -> Path:
    """拿不到用户级运行时目录时的退路（per-uid，避免多用户撞名）。"""
    try:
        uid = os.getuid()
    except AttributeError:  # Windows
        uid = getpass.getuser()
    return Path(tempfile.gettempdir()) / f"{_APP}-{uid}"


def ipc_dir() -> Path:
    """IPC 文件所在目录。**只计算路径，不创建。**"""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
        return root / _APP / "ipc"
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime and Path(runtime).is_dir():
        return Path(runtime) / _APP
    return _fallback_dir()


def ensure_ipc_dir() -> Path:
    """创建目录并收紧到 0700（已存在时也纠正权限）。"""
    directory = ipc_dir()
    directory.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        try:
            os.chmod(directory, _DIR_MODE)
        except OSError:
            pass
    return directory


def socket_path() -> Path | None:
    """POSIX 上的 socket 文件路径；Windows 返回 None。"""
    if os.name == "nt":
        return None
    return ipc_dir() / _SOCKET_NAME


def token_path() -> Path:
    return ipc_dir() / _TOKEN_NAME


# --------------------------------------------------------------------------- #
# token
# --------------------------------------------------------------------------- #
def read_token() -> bytes | None:
    """读取现有 token；不存在/不可读返回 None（**不**创建）。"""
    path = token_path()
    try:
        data = path.read_bytes().strip()
    except OSError:
        return None
    return data or None


def create_or_reuse_token() -> bytes:
    """服务端启动时调用：有就复用，没有就新建。

    复用而非每次轮换的原因：轮换会让「客户端刚读到旧 token、服务端已经重启」
    变成一个必然失败窗口；而文件权限本来就是安全边界，轮换并不增加保障。
    """
    existing = read_token()
    if existing is not None:
        return existing

    ensure_ipc_dir()
    token = secrets.token_hex(_TOKEN_BYTES).encode("ascii")
    path = token_path()
    tmp = path.with_name(path.name + ".tmp")
    try:
        # O_EXCL + 0600：文件一落地就是私有权限，不存在「先 0644 再 chmod」的窗口
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _FILE_MODE)
        try:
            os.write(fd, token)
        finally:
            os.close(fd)
        os.replace(tmp, path)
        return token
    except FileExistsError:
        # 并发启动：别人刚刚写好，用它写的那份
        other = read_token()
        if other is not None:
            return other
        raise
    except OSError as exc:
        raise EndpointUnavailable(f"cannot write token {path}: {exc}") from exc


# --------------------------------------------------------------------------- #
# 地址
# --------------------------------------------------------------------------- #
def _pipe_name() -> str:
    try:
        user = getpass.getuser()
    except Exception:  # noinspection PyBroadException
        user = "default"
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in user) or "default"
    return rf"\\.\pipe\{_APP}-{safe}"


def endpoint_address() -> tuple[str, str]:
    """返回 ``(family, address)``。

    **无副作用**：不创建目录、不写文件。服务端的目录由
    :func:`create_or_reuse_token` 负责建好。
    """
    if os.name == "nt":
        return "AF_PIPE", _pipe_name()
    path = socket_path()
    assert path is not None
    return "AF_UNIX", str(path)


def endpoint(create: bool = False) -> Endpoint:
    """构造端点。

    :param create: ``True`` 表示服务端启动路径（缺 token 时创建）；
        ``False`` 表示客户端路径（缺 token 视为「应用没在跑」）。
    :raises EndpointUnavailable: ``create=False`` 且 token 不存在
    """
    family, address = endpoint_address()
    if create:
        authkey = create_or_reuse_token()
    else:
        authkey = read_token()
        if authkey is None:
            raise EndpointUnavailable(
                f"no ipc token at {token_path()}: application is not running"
            )
    return Endpoint(family=family, address=address, authkey=authkey)


#: 显式说明平台：让 ``--help`` / 日志能看出当前用的是哪种传输
TRANSPORT = "AF_PIPE" if sys.platform == "win32" else "AF_UNIX"
