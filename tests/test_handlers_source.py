#
"""``handlers/source.py``：``source.*`` 命令的行为契约。

守的是「命令只做转运」的四条：

* ``Param`` 声明与 handler 签名一致（``Dispatcher._bind`` 能绑定、无告警）；
* 写命令把**意图**如实翻成 DTO（PATCH 语义：没传的字段保持 ``UNSET``）；
* 读命令输出 JSON-able（枚举转值、DTO 摊平）；
* 参数级错误是 ``BadRequest``（``USAGE_ERROR``），不是 ``CommandFailed``。

线程边界不在这里测 —— 见 ``tests/test_source_thread_boundary.py``；本文件的
stub service 不做任何跨线程处理。
"""

from __future__ import annotations

import json
import warnings

import pytest

from albuswall.command.exceptions import BadRequest, DispatchError
from albuswall.common.enums import FileTypeCheckMode
from albuswall.core import ExitCode
from albuswall.dto.sentinel import UNSET
from albuswall.dto.source.query import IngestSource
from albuswall.service.command.dispatcher import Dispatcher
from albuswall.service.source.runtime import ScanTaskSnapshot
from tests.conftest import (
    FakeSourceRuntime,
    FakeSourceService,
    FakeSourceStateService,
    FakeSourceTrashService,
)


def _dispatch(registry, path, args=None, kwargs=None):
    """按生产装配分派：``Dispatcher`` 带服务层异常翻译器。"""
    from albuswall.service.command.dispatch_errors import (
        translate_service_error,
    )

    return Dispatcher(
        registry, exception_translator=translate_service_error,
    ).dispatch(path, list(args or []), dict(kwargs or {}))


@pytest.fixture
def source_services(
        fake_container,
) -> tuple[FakeSourceService, FakeSourceStateService]:
    return (
        fake_container.require("source_service"),
        fake_container.require("source_state_service"),
    )


@pytest.fixture
def source_runtime(fake_container) -> FakeSourceRuntime:
    return fake_container.require("source_runtime")


@pytest.fixture
def source_trash(fake_container) -> FakeSourceTrashService:
    return fake_container.require("source_trash_service")


def _sample_source(source_id: int = 3) -> IngestSource:
    return IngestSource(
        id=source_id,
        title="pics",
        source_path="/data/pics",
        target_path=None,
        mount_point="/mnt/pics",
        auto_mount=False,
        file_type_check=FileTypeCheckMode.SUFFIX,
        file_types=["png", "jpg"],
        tags=["family"],
    )


# ==================== 装配期 ====================

def test_assembly_declares_all_source_commands(assembled):
    registry, _ = assembled
    names = set(registry.names(include_hidden=True, subject=frozenset()))
    assert {
        "source.list", "source.get", "source.create", "source.update",
        "source.delete", "source.enable", "source.disable",
        "source.trash", "source.restore", "source.purge",
        "source.refresh", "source.mount",
        "source.tasks", "source.scan", "source.stop",
    } <= names


def test_assembly_emits_no_param_warnings(fresh_registry, fake_container):
    """``_params`` 工厂 + ``Param.__post_init__`` 都不该在装配期告警。"""
    from albuswall.service.command.handlers import setup as setup_handlers

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        setup_handlers(fresh_registry, fake_container)

    assert [str(w.message) for w in caught] == []


# ==================== 读 ====================

def test_list_is_json_serialisable_and_flattens_dto(assembled, source_services):
    registry, _ = assembled
    service, _state = source_services
    service.rows = [_sample_source(1), _sample_source(2)]

    payload = _dispatch(registry, "source.list")

    assert [row["id"] for row in payload] == [1, 2]
    # 枚举必须已转成值，否则过不了 json.dumps
    assert payload[0]["file_type_check"] == "suffix"
    json.dumps(payload)          # 不抛 = JSON-able


def test_get_returns_none_when_missing(assembled, source_services):
    registry, _ = assembled
    service, _state = source_services

    assert _dispatch(registry, "source.get", [99]) is None

    service.rows = [_sample_source(9)]
    payload = _dispatch(registry, "source.get", [9])
    assert payload["id"] == 9
    assert payload["title"] == "pics"


# ==================== 写：create ====================

def test_create_translates_params_into_dto(assembled, source_services):
    registry, _ = assembled
    service, _state = source_services

    result = _dispatch(
        registry, "source.create", ["pics", "/data/pics"],
        {
            "description": "家庭照片",
            "mount_point": "/mnt/pics",
            "file_types": "png, jpg",
            "auto_mount": True,
        },
    )

    assert result == {"id": 1}
    dto = service.created[0]
    assert (dto.title, dto.source_path) == ("pics", "/data/pics")
    assert dto.description == "家庭照片"
    assert dto.mount_point == "/mnt/pics"
    assert dto.auto_mount is True
    assert dto.file_types == ["png", "jpg"]      # 逗号串 → 列表
    assert dto.target_path is None


def test_create_accepts_list_file_types(assembled, source_services):
    """IPC / GUI 传数组，CLI 传逗号串，两者都要能用。"""
    registry, _ = assembled
    service, _state = source_services

    _dispatch(registry, "source.create", ["pics", "/p"],
              {"file_types": ["mp4", "mov"]})

    assert service.created[0].file_types == ["mp4", "mov"]


def test_create_rejects_non_string_file_types(assembled):
    registry, _ = assembled

    with pytest.raises(BadRequest, match="file_types"):
        _dispatch(registry, "source.create", ["pics", "/p"],
                  {"file_types": [1, 2]})


# ==================== 写：update 的 PATCH 语义 ====================

def test_update_only_sets_provided_fields(assembled, source_services):
    registry, _ = assembled
    service, _state = source_services

    result = _dispatch(registry, "source.update", [7], {"title": "新名字"})

    assert result == {"id": 7, "changed": ["title"]}
    source_id, patch = service.updates[0]
    assert source_id == 7
    assert patch.title == "新名字"
    # 没传的字段必须保持 UNSET（= 不改），不能变成 None（= 置空）
    assert patch.source_path is UNSET
    assert patch.mount_point is UNSET
    assert patch.auto_mount is UNSET
    assert patch.file_types is UNSET


def test_update_maps_loose_values(assembled, source_services):
    registry, _ = assembled
    service, _state = source_services

    _dispatch(registry, "source.update", [7], {
        "file_types": "",
        "file_type_check": "MAGIC",
        "auto_mount": "false",
    })

    _sid, patch = service.updates[0]
    assert patch.file_types == []                      # 空串 = 清空白名单
    assert patch.file_type_check is FileTypeCheckMode.MAGIC
    assert patch.auto_mount is False                   # 松散布尔归一


def test_update_without_changes_is_a_bad_request(assembled, source_services):
    registry, _ = assembled
    service, _state = source_services

    with pytest.raises(BadRequest, match="nothing to update") as excinfo:
        _dispatch(registry, "source.update", [7])

    assert excinfo.value.exit_code == ExitCode.USAGE_ERROR
    assert service.updates == []


def test_update_rejects_unknown_file_type_check(assembled):
    registry, _ = assembled

    with pytest.raises(BadRequest, match="file_type_check"):
        _dispatch(registry, "source.update", [7], {"file_type_check": "nope"})


# ==================== 写：enable / disable / delete ====================

def test_enable_disable_delete_delegate_to_service(assembled, source_services):
    registry, _ = assembled
    service, _state = source_services

    assert _dispatch(registry, "source.enable", [4]) == {"id": 4, "enabled": True}
    assert _dispatch(registry, "source.disable", [4]) == {"id": 4, "enabled": False}
    assert _dispatch(registry, "source.delete", [4]) == {"id": 4, "deleted": True}

    assert service.enabled == [4]
    assert service.disabled == [4]
    assert service.deleted == [4]


def test_trash_restore_purge_delegate_to_the_trash_service(
        assembled, source_trash,
):
    """回收站三件套只跟 ``SourceTrashService`` 说话（不碰 SourceService）。"""
    registry, _ = assembled

    assert _dispatch(registry, "source.trash", [6]) == {"id": 6, "trashed": True}
    assert _dispatch(registry, "source.restore", [6]) == {
        "id": 6, "restored": True}
    assert _dispatch(registry, "source.purge", [6]) == {
        "id": 6, "purged": True, "asset_ids": [], "asset_uuids": [],
    }

    assert source_trash.trashed == [6]
    assert source_trash.restored == [6]
    assert source_trash.purged == [6]


def test_purge_serialises_uuids(assembled, source_trash):
    """``SourcePurgeReport.asset_uuids`` 是 UUID 对象，必须转成字符串才能过 JSON。"""
    from uuid import UUID

    from albuswall.dto.source.events import SourcePurgeReport

    registry, _ = assembled
    fixed = UUID("00000000-0000-0000-0000-000000000001")
    source_trash.purge_source_now = lambda sid: SourcePurgeReport(
        source_id=sid, purged=True, asset_ids=[11], asset_uuids=[fixed],
    )

    payload = _dispatch(registry, "source.purge", [6])

    assert payload["asset_ids"] == [11]
    assert payload["asset_uuids"] == [str(fixed)]
    json.dumps(payload)          # 不能抛


# ==================== 运行时动作 ====================

def test_refresh_and_mount_go_through_state_service(assembled, source_services):
    registry, _ = assembled
    _service, state = source_services

    assert _dispatch(registry, "source.refresh", [5]) == {
        "id": 5, "refreshed": True}
    assert _dispatch(registry, "source.mount", [5]) == {
        "id": 5, "mounted": True}

    assert state.refreshed == [5]
    assert state.mounted == [5]


# ==================== 参数校验 ====================

@pytest.mark.parametrize("path", [
    "source.get", "source.update", "source.enable",
    "source.disable", "source.delete", "source.refresh", "source.mount",
    "source.trash", "source.restore", "source.purge",
])
def test_source_id_must_be_an_int(assembled, path):
    """字符串 / 布尔 id 一律 ``BadRequest``（不隐式转换）。"""
    registry, _ = assembled

    with pytest.raises(BadRequest, match="source_id") as excinfo:
        _dispatch(registry, path, ["not-an-int"])
    assert excinfo.value.exit_code == ExitCode.USAGE_ERROR

    with pytest.raises(BadRequest, match="source_id"):
        _dispatch(registry, path, [True])


def test_negative_source_id_is_rejected(assembled):
    registry, _ = assembled

    with pytest.raises(BadRequest, match=">= 0"):
        _dispatch(registry, "source.get", [-1])


def test_param_errors_are_dispatch_errors(assembled):
    """``BadRequest`` 属于 ``DispatchError`` 树，调用方 except 一棵树即可。"""
    registry, _ = assembled

    with pytest.raises(DispatchError):
        _dispatch(registry, "source.update", [1])


# ==================== 运行时：观测 + 控制 ====================

def test_tasks_serialises_snapshots(
        assembled, source_runtime, source_services,
):
    """``source.tasks`` 把快照摊平成 dict（含 last_scan），可直接 json.dumps。"""
    registry, _ = assembled
    service, _state = source_services
    service.rows = [_sample_source(1)]
    source_runtime.snapshots = [
        ScanTaskSnapshot(
            source_id=1, seq=5, source_path="/data/pics",
            state="finished", submitted=False,
            last_scan={"source_id": 1, "task_seq": 5, "new_file_count": 2},
            last_scan_at=1700000000.0,
        )
    ]

    payload = _dispatch(registry, "source.tasks")

    assert payload == [{
        "source_id": 1, "seq": 5, "source_path": "/data/pics",
        "state": "finished", "submitted": False,
        "last_scan": {"source_id": 1, "task_seq": 5, "new_file_count": 2},
        "last_scan_at": 1700000000.0,
    }]
    json.dumps(payload)


def test_tasks_accepts_optional_source_id(assembled, source_runtime):
    registry, _ = assembled
    source_runtime.snapshots = [
        ScanTaskSnapshot(
            source_id=1, seq=1, source_path="/a", state="running",
            submitted=True,
        ),
        ScanTaskSnapshot(
            source_id=2, seq=2, source_path="/b", state="pending",
            submitted=False,
        ),
    ]

    assert [row["source_id"] for row in _dispatch(registry, "source.tasks")] == [1, 2]
    assert [row["source_id"] for row in
            _dispatch(registry, "source.tasks", kwargs={"source_id": 2})] == [2]

    with pytest.raises(BadRequest, match="source_id"):
        _dispatch(registry, "source.tasks", kwargs={"source_id": "x"})


def test_scan_forces_scan_with_snapshot_from_source_service(
        assembled, source_runtime, source_services,
):
    """``source.scan`` 取源快照后交给 runtime.force_scan。"""
    registry, _ = assembled
    service, _state = source_services
    service.rows = [_sample_source(4)]

    result = _dispatch(registry, "source.scan", [4])

    assert result == {"id": 4, "accepted": True, "seq": source_runtime.forced_seq}
    assert source_runtime.scanned == [4]


def test_scan_reports_not_accepted_when_runtime_is_shut_down(
        assembled, source_runtime, source_services,
):
    registry, _ = assembled
    service, _state = source_services
    service.rows = [_sample_source(4)]
    source_runtime.forced_seq = None      # runtime 未启动 / 已关闭

    assert _dispatch(registry, "source.scan", [4]) == {
        "id": 4, "accepted": False, "seq": None,
    }


def test_scan_of_missing_source_maps_to_source_not_found(
        assembled, source_runtime,
):
    """不存在的源 → 经翻译表变成 ``source_not_found``（``USAGE_ERROR``）。

    handler 抛的是领域异常 ``SourceNotFound``，命令层负责翻译 —— 这一条同时
    验证"handler 不必自己套 try/except"这条契约。
    """
    from albuswall.command.exceptions import DispatchError
    from albuswall.core import ExitCode
    from albuswall.service.command.dispatch_errors import SourceNotFoundMapped

    registry, _ = assembled

    with pytest.raises(SourceNotFoundMapped) as excinfo:
        _dispatch(registry, "source.scan", [99])

    assert excinfo.value.source_id == 99
    assert excinfo.value.code == "source_not_found"
    assert excinfo.value.exit_code == ExitCode.USAGE_ERROR
    assert isinstance(excinfo.value, DispatchError)
    assert source_runtime.scanned == []


def test_stop_delegates_to_runtime(assembled, source_runtime):
    registry, _ = assembled

    assert _dispatch(registry, "source.stop", [8]) == {"id": 8, "stopped": True}
    assert source_runtime.stopped == [8]


@pytest.mark.parametrize("path", ["source.scan", "source.stop"])
def test_runtime_commands_validate_source_id(assembled, path):
    registry, _ = assembled

    with pytest.raises(BadRequest, match="source_id"):
        _dispatch(registry, path, ["abc"])
