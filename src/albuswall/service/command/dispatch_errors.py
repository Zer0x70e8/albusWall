#
"""服务层异常 → 命令层异常的唯一映射表。

为什么要这一层
--------------
服务层抛的是 ``ServiceError`` 家族（``service/errors.py``、
``service/source/errors.py``）：只带领域载荷与机器可读原因，**没有**协议码
与退出码，也不继承 ``DispatchError``。若原样穿过 ``Dispatcher``，会被兜底
包成 ``CommandFailed``（``code="command_error"``、``exit_code=70``）——
于是"源不存在"这种调用方的错，到了 ``albusctl`` 就成了软件错误。
本模块把每个服务层异常映射成带正确 ``code`` / ``exit_code`` 的
``DispatchError`` 子类，挂在 ``Dispatcher`` 的兜底分支上，
CLI / IPC / GUI 三个入口同时生效。

边界
----
* 本模块**只**做映射，不 import handler / 容器，不碰任何业务逻辑；
* 症状（``raise mapped from exc``）由 ``Dispatcher`` 建立因果链，这里只负责
  把领域载荷（``source_id`` / ``reason`` / ``mount_point`` …）搬过去；
* 新增业务域时在 ``_TABLE`` 里加行即可，不许在 handler 里各写一套。

顺序即优先级
------------
表按"具体在前"排列。目前各家族内部互不继承（除基类），仍是线性表；将来
若出现父子关系，先匹配到的行胜出。
"""

from __future__ import annotations

from collections.abc import Callable

from albuswall.command.exceptions import (
    DispatchError, DispatchTimeout, DispatchUnavailable,
)
from albuswall.core import ExitCode

from ..errors import (
    AlbumNotFound, AssetNotFound, AssetOpsError, BatchAtomicityError,
    MainLoopUnavailable, MainThreadTimeout, NotAMutableAlbum, NotAVisibleAsset,
)
from ..tag.errors import (
    TagCycle, TagError, TagHasChildren, TagMoveConflict, TagNameConflict,
    TagNotFound, TagParentNotFound, TagRestoreConflict,
)
from ..source.errors import (
    SourceCreateFailed, SourceDeleteFailed, SourceMountFailed,
    SourceNotFound, SourcePathInvalid, SourceServiceClosed, SourceUpdateFailed,
    SourceVirtualRoot,
)

__all__ = ["translate_service_error"]


# ==================== 映射后的命令层异常 ====================
#
# 基类**不声明** ``code``：``AppError.__init_subclass__`` 只认子类自己 dict
# 里的 code，基类声明了反而要占一个全局唯一的码。载荷字段在各家族里并不
# 统一，因此统一按"有就搬、没有就是 None"处理。

class _MappedError(DispatchError):
    """映射基类：保留领域载荷，人话文案沿用原异常。"""

    #: 从原异常搬到映射异常上的领域字段（各家族字段并不统一）。
    _fields: tuple[str, ...] = (
        "source_id", "path", "reason", "operation", "mount_point",
    )

    def __init__(self, original: BaseException) -> None:
        self.original = original
        for field in self._fields:
            setattr(self, field, getattr(original, field, None))
        super().__init__(str(original))


class SourceNotFoundMapped(_MappedError):
    """源不存在 → 调用方的错。"""
    code = "source_not_found"
    exit_code = ExitCode.USAGE_ERROR


class SourceVirtualRootMapped(_MappedError):
    """对虚拟根（手动导入源，id=0）做了禁止的操作。"""
    code = "source_virtual_root"
    exit_code = ExitCode.USAGE_ERROR


class SourcePathInvalidMapped(_MappedError):
    """``source_path`` 不合法（空 / 不是目录）。"""
    code = "source_path_invalid"
    exit_code = ExitCode.USAGE_ERROR


class SourceDeleteFailedMapped(_MappedError):
    """删除源未被允许（仍有资产引用）或未生效。"""
    code = "source_delete_failed"
    exit_code = ExitCode.USAGE_ERROR


class SourceCreateFailedMapped(_MappedError):
    """创建源失败（仓储 INSERT 抛错）。"""
    code = "source_create_failed"
    exit_code = ExitCode.SOFTWARE_ERROR


class SourceUpdateFailedMapped(_MappedError):
    """更新源失败（PATCH 没写进库）。"""
    code = "source_update_failed"
    exit_code = ExitCode.SOFTWARE_ERROR


class SourceMountFailedMapped(_MappedError):
    """自动挂载失败（命令非零退出 / 挂载点被占用 / 源目录无效）。"""
    code = "source_mount_failed"
    exit_code = ExitCode.OS_ERROR


class SourceServiceClosedMapped(_MappedError):
    """源服务已 shutdown，写操作不再受理。"""
    code = "source_service_closed"
    exit_code = ExitCode.SOFTWARE_ERROR


# ---- 资产域 ----
#
# ``AssetOpsError`` 家族带的是 ``asset_uuid`` / ``album_uuid``，与源域的
# ``source_id`` 不通用，所以这几个类各自声明 ``_fields``。

class AssetNotFoundMapped(_MappedError):
    """资产 uuid 不存在。"""
    code = "asset_not_found"
    exit_code = ExitCode.USAGE_ERROR
    _fields = ("asset_uuid", "uuid")


class AssetNotVisibleMapped(_MappedError):
    """资产存在但不可见（已软删 / 所属导入源已下线）。"""
    code = "asset_not_visible"
    exit_code = ExitCode.USAGE_ERROR
    _fields = ("asset_uuid", "reason")


class AlbumNotFoundMapped(_MappedError):
    """相簿 uuid 不存在。"""
    code = "album_not_found"
    exit_code = ExitCode.USAGE_ERROR
    _fields = ("album_uuid",)


class AlbumNotMutableMapped(_MappedError):
    """往系统相册 / 回收站相册写（``reason`` = ``trash`` / ``system``）。"""
    code = "album_not_mutable"
    exit_code = ExitCode.USAGE_ERROR
    _fields = ("album_uuid", "reason")


class AssetOpsErrorMapped(_MappedError):
    """资产域未细分的业务错误（基类兜底，别误报成"资产不存在"）。"""
    code = "asset_operation_failed"
    exit_code = ExitCode.SOFTWARE_ERROR
    _fields = ("asset_uuid", "album_uuid", "reason")


class BatchAtomicityErrorMapped(_MappedError):
    """批量写事务失败并已整体回滚（``cause`` 保留原始异常）。"""
    code = "batch_atomicity_error"
    exit_code = ExitCode.SOFTWARE_ERROR
    _fields = ("cause",)


# ---- 标签域 ----
#
# ``TagError`` 家族的一棵树下有 8 个具体类；命令层只给"用户能改的错"细分码，
# 其余统一 ``tag_error``（仍比 ``command_error`` 有用：调用方能区分"参数/状态
# 不对"与"程序出错"）。

class TagErrorMapped(_MappedError):
    """标签域未细分的业务错误（基类兜底）。"""
    code = "tag_error"
    exit_code = ExitCode.USAGE_ERROR
    _fields = ("uuid", "name")


class TagNotFoundMapped(_MappedError):
    """标签 / 别名 uuid 不存在。"""
    code = "tag_not_found"
    exit_code = ExitCode.USAGE_ERROR
    _fields = ("uuid",)


class TagNameConflictMapped(_MappedError):
    """同层重名（``existing_uuid`` 指向已存在的那个）。"""
    code = "tag_name_conflict"
    exit_code = ExitCode.USAGE_ERROR
    _fields = ("name", "existing_uuid")


class TagParentNotFoundMapped(_MappedError):
    """父标签不存在 / 已软删。"""
    code = "tag_parent_not_found"
    exit_code = ExitCode.USAGE_ERROR
    _fields = ("parent_uuid",)


class TagCycleMapped(_MappedError):
    """移动标签会成环（含别名"向上短路"）。"""
    code = "tag_cycle"
    exit_code = ExitCode.USAGE_ERROR
    _fields = ("tag_uuid", "new_parent_uuid", "alias_uuid", "target_uuid")


class TagHasChildrenMapped(_MappedError):
    """有子标签且未 ``cascade``。"""
    code = "tag_has_children"
    exit_code = ExitCode.USAGE_ERROR
    _fields = ("uuid", "children_uuids")


class TagMoveConflictMapped(_MappedError):
    """移动标签到目标层会重名。"""
    code = "tag_move_conflict"
    exit_code = ExitCode.USAGE_ERROR
    _fields = ("tag_uuid", "new_parent_uuid")


class TagRestoreConflictMapped(_MappedError):
    """恢复标签时同层已存在同名标签。"""
    code = "tag_restore_conflict"
    exit_code = ExitCode.USAGE_ERROR
    _fields = ("uuid",)


#: ``(服务层异常类型, 工厂)``；顺序即优先级。
#:
#: 工厂统一是 ``BaseException -> DispatchError``，因此 ``_MappedError`` 子类
#: 可以直接当工厂用（它们搬领域字段）；没有领域载荷的两条用 ``lambda`` 显式
#: 构造，并保留 ``original``。
_TABLE: tuple[
    tuple[type[BaseException], Callable[[BaseException], DispatchError]], ...
] = (
    # ---- 调度通道（跨所有域）----
    (MainLoopUnavailable,
     lambda exc: DispatchUnavailable(str(exc), original=exc)),
    (MainThreadTimeout,
     lambda exc: DispatchTimeout(str(exc), original=exc)),
    # ---- 源域 ----
    (SourceNotFound, SourceNotFoundMapped),
    (SourceVirtualRoot, SourceVirtualRootMapped),
    (SourcePathInvalid, SourcePathInvalidMapped),
    (SourceDeleteFailed, SourceDeleteFailedMapped),
    (SourceCreateFailed, SourceCreateFailedMapped),
    (SourceUpdateFailed, SourceUpdateFailedMapped),
    (SourceMountFailed, SourceMountFailedMapped),
    (SourceServiceClosed, SourceServiceClosedMapped),
    # ---- 资产域（具体在前，基类兜最后）----
    (AssetNotFound, AssetNotFoundMapped),
    (NotAVisibleAsset, AssetNotVisibleMapped),
    (AlbumNotFound, AlbumNotFoundMapped),
    (NotAMutableAlbum, AlbumNotMutableMapped),
    (BatchAtomicityError, BatchAtomicityErrorMapped),
    (AssetOpsError, AssetOpsErrorMapped),   # 基类兜底（放最后）
    # ---- 标签域（具体在前，TagError 兜最后）----
    (TagNotFound, TagNotFoundMapped),
    (TagNameConflict, TagNameConflictMapped),
    (TagParentNotFound, TagParentNotFoundMapped),
    (TagCycle, TagCycleMapped),
    (TagHasChildren, TagHasChildrenMapped),
    (TagMoveConflict, TagMoveConflictMapped),
    (TagRestoreConflict, TagRestoreConflictMapped),
    (TagError, TagErrorMapped),
)


def translate_service_error(exc: BaseException) -> DispatchError | None:
    """服务层异常 → ``DispatchError``；认不出（或已经是命令层异常）返回 ``None``。

    返回值是 ``None`` 时由 ``Dispatcher`` 走原有的 ``CommandFailed`` 兜底，
    因此本函数不需要覆盖"未知异常"这种兜底语义。

    :param exc: handler 抛出的原始异常（``Dispatcher`` 从 ``except`` 分支传入）。
    """
    if isinstance(exc, DispatchError):
        return None                      # 已经是命令层异常，别二次包装

    for service_type, factory in _TABLE:
        if isinstance(exc, service_type):
            return factory(exc)
    return None
