#
"""``service/source`` 各子包共用的类型化异常。

归属
----
本模块是 ``service/source`` 包**唯一**的异常出口：``command`` / ``state`` /
``runtime`` / ``trigger`` 四个子包都从这里 import，子包之间不互相
import 异常。导入方向因此始终是 ``子包 → errors``，天然无环。

层级
----
::

    ServiceError                （``albuswall.service.errors``）
    ├── AssetOpsError           （资产域，与本家族**平级**，互不继承）
    └── SourceError
        ├── SourceNotFound          # id 在库中不存在
        ├── SourceVirtualRoot       # 操作 MANUAL_SOURCE_ID（id=0）
        ├── SourceCreateFailed      # INSERT 失败
        ├── SourceUpdateFailed      # PATCH 未生效
        ├── SourceDeleteFailed      # 删除未被允许 / 未生效
        ├── SourceMountFailed       # auto_mount 执行失败
        ├── SourcePathInvalid       # source_path 不合法（空 / 非目录）
        └── SourceServiceClosed     # shutdown 之后的写调用

契约
----
* **守卫失败一律抛异常**，不返回 ``False`` / ``None`` 让调用方猜
  （旧 ``SourceService`` 的"静默 no-op + Optional 返回值"已被取消）；
* 异常只携带**领域标识**（``source_id`` / ``path`` / ``mount_point``）与
  **机器可读的原因**（``reason`` 字符串、``cause`` 原始异常），
  面向用户的文案由 presenter 层自行拼装，本模块不产出 UI 文案；
* 读路径（``list_sources`` / ``get_source`` / ``has_source``）**不抛**
  ``SourceNotFound``：查不到就是 ``None`` / ``False``，这是查询的正常结果。
"""

from __future__ import annotations

from typing import Optional

from albuswall.service.errors import ServiceError

__all__ = [
    "SourceCreateFailed",
    "SourceDeleteFailed",
    "SourceError",
    "SourceMountFailed",
    "SourceNotFound",
    "SourcePathInvalid",
    "SourceServiceClosed",
    "SourceUpdateFailed",
    "SourceVirtualRoot",
]


class SourceError(ServiceError):
    """导入源域异常基类。

    调用方只 ``except SourceError`` 一棵树即可覆盖命令层、状态层、触发器
    桥接层的全部守卫失败。
    """


class SourceNotFound(SourceError):
    """源不存在（库中无该行）。

    ``source_id`` 是调用方提供的原始值，不做归一化 —— 便于调用方拿异常
    里的 id 直接拼日志。
    """

    def __init__(self, source_id: int) -> None:
        self.source_id = source_id
        super().__init__(f"source not found: id={source_id}")


class SourceVirtualRoot(SourceError):
    """试图对虚拟根 / 手动导入源（``MANUAL_SOURCE_ID``，id=0）做禁止的操作。

    虚拟根是 UI 的"手动导入"落点，没有 ``ingest_source`` 行可供同步、删除
    或挂载；``source_id`` 字段恒为 0，保留它是为了让调用方的日志格式统一。
    """

    def __init__(self, source_id: int = 0, *, operation: str = "") -> None:
        self.source_id = source_id
        self.operation = operation
        super().__init__(
            f"source id={source_id} is the virtual root; "
            f"operation {operation or '<unknown>'} is not allowed"
        )


class SourceCreateFailed(SourceError):
    """创建源失败（仓储 INSERT 抛错）。

    ``cause`` 保留底层异常供日志排查；**不**向上抛原始异常，避免调用方
    误以为"部分成功"（id 没有产生就是没有）。
    """

    def __init__(self, cause: Optional[BaseException] = None) -> None:
        self.cause = cause
        super().__init__(
            "failed to create source"
            + (f": {cause}" if cause is not None else "")
        )


class SourceUpdateFailed(SourceError):
    """更新源失败（PATCH 没有被写进库）。"""

    def __init__(
            self,
            source_id: int,
            cause: Optional[BaseException] = None,
    ) -> None:
        self.source_id = source_id
        self.cause = cause
        super().__init__(
            f"failed to update source id={source_id}"
            + (f": {cause}" if cause is not None else "")
        )


class SourceDeleteFailed(SourceError):
    """删除源失败（虚拟根、仍有资产引用、或仓储未生效）。"""

    def __init__(
            self,
            source_id: int,
            cause: Optional[BaseException] = None,
    ) -> None:
        self.source_id = source_id
        self.cause = cause
        super().__init__(
            f"failed to delete source id={source_id}"
            + (f": {cause}" if cause is not None else "")
        )


class SourceMountFailed(SourceError):
    """自动挂载失败（``mount`` 命令非零退出 / 挂载点被占用 / 源目录无效）。

    ``mount_point`` 是配置里的挂载点字符串（可能为 ``None``：该源的
    ``auto_mount`` 为假时不涉及挂载，此时不应走到这里）。
    """

    def __init__(
            self,
            source_id: int,
            mount_point: Optional[str] = None,
            cause: Optional[BaseException] = None,
    ) -> None:
        self.source_id = source_id
        self.mount_point = mount_point
        self.cause = cause
        super().__init__(
            f"failed to mount source id={source_id} "
            f"at {mount_point!r}"
            + (f": {cause}" if cause is not None else "")
        )


class SourcePathInvalid(SourceError):
    """``source_path`` 不合法：空串、不是目录、或无法解析。

    ``reason`` 是机器可读的短原因，**不用于展示**。
    """

    def __init__(
            self,
            source_id: int,
            path: Optional[str],
            reason: str = "invalid",
    ) -> None:
        self.source_id = source_id
        self.path = path
        self.reason = reason
        super().__init__(
            f"invalid source path for id={source_id}: {path!r} ({reason})"
        )


class SourceServiceClosed(SourceError):
    """``SourceService`` 已 ``shutdown()``，写操作不再受理。

    只在**写**路径抛：关闭后读路径（列表 / 单查 / 存在性）仍然可用，
    它们只是转发仓储，不依赖本服务的生命周期。
    """

    def __init__(self, operation: str = "") -> None:
        self.operation = operation
        super().__init__(
            "source service is closed"
            + (f"; operation {operation} rejected" if operation else "")
        )
