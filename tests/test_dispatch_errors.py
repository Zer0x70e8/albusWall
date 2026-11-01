#
"""服务层异常 → 命令层异常（``service/command/dispatch_errors.py``）。

没有这一层的话，``SourceNotFound`` 这类**调用方的错**穿过 ``Dispatcher``
会被包成 ``CommandFailed``（``code="command_error"``、退出码 70），
``albusctl`` 用户看到的是"软件错误"。本文件钉住映射表本身，以及
``Dispatcher`` 里的接入点。
"""

from __future__ import annotations

import pytest

from albuswall.command import Command, CommandRegistry
from albuswall.command.exceptions import (
    CommandFailed, DispatchTimeout, DispatchUnavailable, DispatchError,
)
from albuswall.core import ExitCode
from albuswall.service.command.dispatch_errors import (
    SourceCreateFailedMapped,
    SourceDeleteFailedMapped,
    SourceMountFailedMapped,
    SourceNotFoundMapped,
    SourcePathInvalidMapped,
    SourceServiceClosedMapped,
    SourceUpdateFailedMapped,
    SourceVirtualRootMapped,
    translate_service_error,
)
from albuswall.service.command.dispatch_errors import (
    AlbumNotFoundMapped,
    AlbumNotMutableMapped,
    AssetNotFoundMapped,
    AssetNotVisibleMapped,
    AssetOpsErrorMapped,
    BatchAtomicityErrorMapped,
    TagCycleMapped,
    TagErrorMapped,
    TagHasChildrenMapped,
    TagMoveConflictMapped,
    TagNameConflictMapped,
    TagNotFoundMapped,
    TagParentNotFoundMapped,
    TagRestoreConflictMapped,
)
from albuswall.service.command.dispatcher import Dispatcher
from albuswall.service.errors import (
    AlbumNotFound,
    AssetNotFound,
    AssetOpsError,
    BatchAtomicityError,
    MainLoopUnavailable,
    MainThreadTimeout,
    NotAMutableAlbum,
    NotAVisibleAsset,
)
from albuswall.service.tag.errors import (
    TagCycle,
    TagError,
    TagHasChildren,
    TagMoveConflict,
    TagNameConflict,
    TagNotFound,
    TagParentNotFound,
    TagRestoreConflict,
)
from albuswall.service.source import (
    SourceCreateFailed,
    SourceDeleteFailed,
    SourceMountFailed,
    SourceNotFound,
    SourcePathInvalid,
    SourceServiceClosed,
    SourceUpdateFailed,
    SourceVirtualRoot,
)


# ==================== 映射表 ====================

_TABLE = (
    # (服务层异常, 期望的命令层类型, code, exit_code)
    (SourceNotFound(7), SourceNotFoundMapped, "source_not_found",
     ExitCode.USAGE_ERROR),
    (SourceVirtualRoot(0, operation="delete"), SourceVirtualRootMapped,
     "source_virtual_root", ExitCode.USAGE_ERROR),
    (SourcePathInvalid(3, "", "empty"), SourcePathInvalidMapped,
     "source_path_invalid", ExitCode.USAGE_ERROR),
    (SourceDeleteFailed(4, RuntimeError("has assets")),
     SourceDeleteFailedMapped, "source_delete_failed", ExitCode.USAGE_ERROR),
    (SourceCreateFailed(RuntimeError("db")), SourceCreateFailedMapped,
     "source_create_failed", ExitCode.SOFTWARE_ERROR),
    (SourceUpdateFailed(5), SourceUpdateFailedMapped,
     "source_update_failed", ExitCode.SOFTWARE_ERROR),
    (SourceMountFailed(6, "/mnt", RuntimeError("mount")),
     SourceMountFailedMapped, "source_mount_failed", ExitCode.OS_ERROR),
    (SourceServiceClosed("create"), SourceServiceClosedMapped,
     "source_service_closed", ExitCode.SOFTWARE_ERROR),
    (MainLoopUnavailable("gone"), DispatchUnavailable,
     "dispatch_unavailable", ExitCode.SOFTWARE_ERROR),
    (MainThreadTimeout(3.0), DispatchTimeout,
     "dispatch_timeout", ExitCode.SOFTWARE_ERROR),
    # ---- 资产域 ----
    (AssetNotFound("uuid-1"), AssetNotFoundMapped,
     "asset_not_found", ExitCode.USAGE_ERROR),
    (NotAVisibleAsset("uuid-2", reason="deleted"), AssetNotVisibleMapped,
     "asset_not_visible", ExitCode.USAGE_ERROR),
    (AlbumNotFound("album-1"), AlbumNotFoundMapped,
     "album_not_found", ExitCode.USAGE_ERROR),
    (NotAMutableAlbum("album-2", reason="trash"), AlbumNotMutableMapped,
     "album_not_mutable", ExitCode.USAGE_ERROR),
    (BatchAtomicityError("move failed", cause=RuntimeError("db")),
     BatchAtomicityErrorMapped, "batch_atomicity_error",
     ExitCode.SOFTWARE_ERROR),
    (AssetOpsError("boom"), AssetOpsErrorMapped,
     "asset_operation_failed", ExitCode.SOFTWARE_ERROR),
    # ---- 标签域 ----
    (TagNotFound("tag-1"), TagNotFoundMapped,
     "tag_not_found", ExitCode.USAGE_ERROR),
    (TagNameConflict("旅行"), TagNameConflictMapped,
     "tag_name_conflict", ExitCode.USAGE_ERROR),
    (TagParentNotFound("tag-2"), TagParentNotFoundMapped,
     "tag_parent_not_found", ExitCode.USAGE_ERROR),
    (TagCycle("tag-3", "tag-4"), TagCycleMapped,
     "tag_cycle", ExitCode.USAGE_ERROR),
    (TagHasChildren("tag-5", ["c1"]), TagHasChildrenMapped,
     "tag_has_children", ExitCode.USAGE_ERROR),
    (TagMoveConflict("tag-6", "tag-7"), TagMoveConflictMapped,
     "tag_move_conflict", ExitCode.USAGE_ERROR),
    (TagRestoreConflict("tag-8"), TagRestoreConflictMapped,
     "tag_restore_conflict", ExitCode.USAGE_ERROR),
    (TagError("generic"), TagErrorMapped,
     "tag_error", ExitCode.USAGE_ERROR),
)


@pytest.mark.parametrize(
    "original,mapped_type,code,exit_code", _TABLE,
    ids=[type(exc).__name__ for exc, *_ in _TABLE],
)
def test_service_error_maps_to_typed_dispatch_error(
        original, mapped_type, code, exit_code,
):
    mapped = translate_service_error(original)

    assert mapped is not None, f"{type(original).__name__} 没有被翻译"
    assert type(mapped) is mapped_type
    assert isinstance(mapped, DispatchError)
    assert mapped.code == code
    assert mapped.exit_code == exit_code
    # 人话文案沿用原异常，且原异常挂在 ``original`` 上供日志排查
    assert str(mapped) == str(original)
    assert mapped.original is original


def test_codes_are_registered_for_the_client_exit_code_lookup():
    """``ExitCode.from_code`` 靠类注册表反查退出码（客户端侧依赖它）。"""
    for _original, _mapped, code, exit_code in _TABLE:
        assert ExitCode.from_code(code) == exit_code


def test_domain_payload_is_carried_over():
    """领域载荷（source_id / reason / operation / mount_point）不能丢。"""
    mapped = translate_service_error(SourceMountFailed(9, "/mnt/x"))
    assert mapped.source_id == 9
    assert mapped.mount_point == "/mnt/x"

    invalid = translate_service_error(SourcePathInvalid(9, "/nope", "not a dir"))
    assert (invalid.source_id, invalid.path, invalid.reason) == (
        9, "/nope", "not a dir")


def test_unknown_and_already_dispatch_errors_are_left_alone():
    """认不出的异常返回 None（交给 ``CommandFailed`` 兜底），不二次包装。"""
    assert translate_service_error(ValueError("nope")) is None
    assert translate_service_error(DispatchError("already")) is None
    assert translate_service_error(SourceNotFoundMapped(SourceNotFound(1))) is None


# ==================== Dispatcher 接入点 ====================

def _dispatcher(registry: CommandRegistry, *, translator=True) -> Dispatcher:
    return Dispatcher(
        registry, exception_translator=translate_service_error if translator
        else None,
    )


def test_dispatcher_maps_service_error_and_keeps_cause():
    registry = CommandRegistry()
    original = SourceNotFound(42)

    def _cmd() -> None:
        raise original

    registry.register(Command(path="source.get", func=_cmd))

    with pytest.raises(SourceNotFoundMapped) as excinfo:
        _dispatcher(registry).dispatch("source.get", [], {})

    assert excinfo.value.source_id == 42
    assert excinfo.value.__cause__ is original


def test_dispatcher_without_translator_still_wraps_into_command_failed():
    """没装翻译器时行为与今天一致（``CommandFailed``），不是静默放行。"""
    registry = CommandRegistry()

    def _cmd() -> None:
        raise SourceNotFound(1)

    registry.register(Command(path="source.get", func=_cmd))

    with pytest.raises(CommandFailed) as excinfo:
        _dispatcher(registry, translator=False).dispatch("source.get", [], {})

    assert excinfo.value.code == "command_error"


def test_handler_raised_dispatch_error_is_not_double_wrapped():
    """handler 自己抛 ``DispatchError`` 时，翻译器不介入。"""
    registry = CommandRegistry()
    original = DispatchUnavailable("shutting down")

    def _cmd() -> None:
        raise original

    registry.register(Command(path="any.cmd", func=_cmd))

    with pytest.raises(DispatchUnavailable) as excinfo:
        _dispatcher(registry).dispatch("any.cmd", [], {})

    assert excinfo.value is original
    assert excinfo.value.__cause__ is None
