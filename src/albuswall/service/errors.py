#
"""服务层共享的类型化异常。

本模块只放**跨服务共用**的守卫异常，不含任何业务逻辑、不做 IO、不 import
Qt / 仓储 / DTO，避免服务层互相 import 造成环。

使用契约（``AssetOpsService`` / ``AssetTagService`` 共用）：

* 守卫失败一律 **抛异常**，不返回错误字符串、不返回 UI 文案、不返回
  ``None`` 让调用方自己猜；
* 异常只携带**领域标识**（uuid）与**机器可读的原因枚举/字符串**，
  面向用户的文案由 presenter 层根据异常类型与 uuid 自行拼装；
* ``AssetNotFound`` 全项目**只有一份定义**（本模块）。历史上它定义在
  ``services/asset_tag.py``，现下沉到此处，``asset_tag`` 继续按原名再导出，
  旧 import 路径不破坏。

异常层级::

    ServiceError
    ├── AssetOpsError
    │   ├── AssetNotFound
    │   ├── NotAVisibleAsset
    │   ├── AlbumNotFound
    │   ├── NotAMutableAlbum
    │   └── BatchAtomicityError
    ├── MainLoopUnavailable     （主线程调度不可用；见 main_loop_service.py）
    ├── MainThreadTimeout       （主线程调度超时）
    └── SourceError            （定义在 ``service/source/errors.py``）
        ├── SourceNotFound
        ├── SourceVirtualRoot
        └── …（见该模块）

``MainLoopUnavailable`` / ``MainThreadTimeout`` 描述的是**调度通道**的问题，
与业务域无关，因此直接挂在 ``ServiceError`` 下，四个源域子包与未来的资产域
都能抛；命令层由 ``service/command/dispatch_errors.py`` 统一翻译成带
``code`` / ``exit_code`` 的 ``DispatchError``。

三条家族除 ``ServiceError`` 之外**互不继承**：资产族的调用方
``except AssetOpsError`` 不会误吞源域错误，反之亦然。
"""

from __future__ import annotations

from uuid import UUID

__all__ = [
    "AlbumNotFound",
    "AssetNotFound",
    "AssetOpsError",
    "BatchAtomicityError",
    "MainLoopUnavailable",
    "MainThreadTimeout",
    "NotAMutableAlbum",
    "NotAVisibleAsset",
    "ServiceError",
]

# 服务层入参统一接受 ``UUID`` 或 canonical uuid 字符串（与 DTO 层 to_uuid
# 的口径一致），异常里存什么就回什么，不强转。
_UUIDLike = UUID | str


class ServiceError(Exception):
    """服务层异常基类。

    不继承 ``RuntimeError``：服务层错误是**预期内的业务分支**（资产没了、
    相册不可写），不是"程序跑飞"，调用方应当按类型分支处理。
    """


class AssetOpsError(ServiceError):
    """资产写操作异常基类。"""


class AssetNotFound(AssetOpsError):
    """资产 uuid 在仓储中不存在。

    ``.uuid`` 与 ``.asset_uuid`` 指向同一个值：前者是历史字段名
    （``AssetTagService`` 既有代码读 ``.uuid``），后者与 DTO 字段命名对齐。
    """

    def __init__(self, asset_uuid: _UUIDLike) -> None:
        self.asset_uuid = asset_uuid
        self.uuid = asset_uuid
        super().__init__(f"asset not found: {asset_uuid}")


class NotAVisibleAsset(AssetOpsError):
    """资产存在但不可见（已软删 / 所属导入源已软删）。

    ``reason`` 是底层视图仓储返回的可见性状态字符串，便于上层区分
    "已软删" 与 "其他不可见原因"，**不用于展示**。
    """

    def __init__(self, asset_uuid: _UUIDLike, reason: str = "deleted") -> None:
        self.asset_uuid = asset_uuid
        self.reason = reason
        super().__init__(f"asset not visible: {asset_uuid} ({reason})")


class AlbumNotFound(AssetOpsError):
    """相册 uuid 在仓储中不存在。"""

    def __init__(self, album_uuid: _UUIDLike) -> None:
        self.album_uuid = album_uuid
        super().__init__(f"album not found: {album_uuid}")


class NotAMutableAlbum(AssetOpsError):
    """目标相册为系统相册（All）或回收站相册，不允许写成员关系。

    ``reason`` 取值 ``"system"`` / ``"trash"``，机器可读，不用于展示。
    """

    def __init__(self, album_uuid: _UUIDLike, reason: str = "system") -> None:
        self.album_uuid = album_uuid
        self.reason = reason
        super().__init__(f"album not mutable: {album_uuid} ({reason})")


class BatchAtomicityError(AssetOpsError):
    """原子批量操作中途失败，仓储事务已整体回滚，**无部分生效**。

    ``cause`` 保留底层异常供日志排查；不向上抛原始异常，避免调用方
    误以为存在"部分成功"的返回值。
    """

    def __init__(self, message: str, cause: BaseException | None = None) -> None:
        self.cause = cause
        super().__init__(message)


class MainLoopUnavailable(ServiceError):
    """主循环调度通道不可用：调度器已关闭 / 无法把工作投递到主循环。

    与业务域无关：任何需要在主线程改状态的写入口都可能抛它。命令层
    （``service/command/dispatch_errors.py``）把它翻译成
    ``DispatchUnavailable``（``code="dispatch_unavailable"``）。
    """

    def __init__(self, message: str = "main loop is unavailable") -> None:
        super().__init__(message)


class MainThreadTimeout(ServiceError):
    """主循环在超时内没有响应（大概率被某个回调阻塞）。

    ``timeout`` 是等待时长（秒），供日志 / presenter 直接复用。
    命令层把它翻译成 ``DispatchTimeout``（``code="dispatch_timeout"``）。
    """

    def __init__(self, timeout: float, message: str = "") -> None:
        self.timeout = timeout
        super().__init__(
            message or f"main loop gave no response within {timeout}s"
        )
