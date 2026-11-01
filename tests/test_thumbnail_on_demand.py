#
"""缩略图「按需生成」与视图信号的契约。

守的是本轮最关键的一条行为变化：**导入完成后不再批量回填缩略图**。
需求原话是"只在被访问的时候才生成……确保不会一直卡在生成缩略图"，
所以这里钉死四件事：

1. ``request_bulk`` 只处理被点名的资产，并且一次最多排 ``batch_size`` 个；
2. 已经齐全（DB 里 spec 路径全在）/ 在冷却期 / 已在途的资产不重复排；
3. ``ViewService`` 在返回资产 DTO 时广播 ``assets_viewed``，而装配层把它接到
   ``ThumbnailService.request_bulk``；
4. 没有任何启动 / 导入钩子会调 ``scan_and_submit``。
"""

from __future__ import annotations

from concurrent.futures import Future
from pathlib import Path
from typing import Any, Optional
from uuid import UUID, uuid4

import pytest

from albuswall.common.enums import ThumbSpec
from albuswall.dto.thumbnail.paths import ThumbnailPaths
from albuswall.service.thumbnail import ThumbnailService
from albuswall.service.thumbnail.config import ThumbnailServiceConfig
from albuswall.service.view import ViewService


# =========================================================================== #
# 替身
# =========================================================================== #

def _config(**overrides: Any) -> ThumbnailServiceConfig:
    """一份"路径已齐"判断所需的配置快照（其余字段给安全默认）。"""
    base: dict[str, Any] = {
        "specs": {"small": 128, "medium": 512, "large": 1024},
        "image_format": "PNG",
        "quality": 85,
        "thumb_root": "/tmp/albuswall-thumbs-test",
        "batch_size": 4,
        "failure_cooldown_sec": 300.0,
        "write_max_retries": 0,
        "write_retry_backoff_sec": 0.0,
        "xdg_enabled": False,
        "xdg_spec_map": None,
    }
    base.update(overrides)
    return base  # type: ignore[return-value]


class _Tasks:
    """只记录提交、**不完成** future —— 这样 ``_inflight`` 保持占用，
    与真实 TaskService 在任务跑完前不 release 的行为一致。"""

    def __init__(self) -> None:
        self.submitted: list[tuple[Any, tuple, Optional[Future]]] = []

    def submit(self, fn, *args, executor=None, priority=0):
        fut: Future = Future()
        self.submitted.append((fn, args, fut))
        return fut


class _ThumbRepo:
    def __init__(self, paths: Optional[dict[str, ThumbnailPaths]] = None):
        self._paths = paths or {}
        self.bulk_calls: list[list[str]] = []

    def get_paths_bulk_by_uuid(self, uuids):
        self.bulk_calls.append(list(uuids))
        return {
            k: v for k, v in self._paths.items() if k in set(uuids)
        }

    def get_paths_by_uuid(self, uuid):  # pragma: no cover - 未调用
        return self._paths.get(uuid)

    def get_task_input_by_uuid(self, uuid, include_deleted=False):  # pragma: no cover
        raise AssertionError("按需请求路径不该真的跑 worker")


class _SourceRepo:
    pass


def _service(tmp_path: Path, **config: Any) -> tuple[ThumbnailService, _Tasks, _ThumbRepo]:
    monkeypatch_conf = _config(**config)
    from albuswall.service.thumbnail import service as svc_mod

    tasks = _Tasks()
    repo = _ThumbRepo()
    svc = ThumbnailService(tasks, repo, _SourceRepo())
    # 构造期读的是配置系统，这里直接覆盖成测试专用的快照（避免读真实配置）
    svc._specs = {
        ThumbSpec.SMALL: monkeypatch_conf["specs"]["small"],
        ThumbSpec.MEDIUM: monkeypatch_conf["specs"]["medium"],
        ThumbSpec.LARGE: monkeypatch_conf["specs"]["large"],
    }
    svc._spec_map = {
        ThumbSpec.SMALL: "normal",
        ThumbSpec.MEDIUM: "x-large",
        ThumbSpec.LARGE: "xx-large",
    }
    svc._batch_size = monkeypatch_conf["batch_size"]
    svc._failure_cooldown_sec = monkeypatch_conf["failure_cooldown_sec"]
    svc._root = tmp_path
    return svc, tasks, repo


def _complete_paths() -> ThumbnailPaths:
    return ThumbnailPaths(
        base="/cache", small="normal/a.png",
        medium="x-large/a.png", large="xx-large/a.png",
    )


# =========================================================================== #
# request / request_bulk
# =========================================================================== #

def test_request_bulk_submits_at_most_batch_size(tmp_path):
    svc, tasks, _repo = _service(tmp_path, batch_size=2)
    uuids = [uuid4() for _ in range(5)]

    assert svc.request_bulk(uuids) == 2, "一次访问最多排 batch_size 个"
    assert len(tasks.submitted) == 2


def test_request_bulk_skips_assets_whose_paths_are_complete(tmp_path):
    svc, tasks, repo = _service(tmp_path)
    done, missing = uuid4(), uuid4()
    repo._paths = {str(done): _complete_paths()}

    assert svc.request_bulk([done, missing]) == 1

    submitted_uuid = tasks.submitted[0][1][0]
    assert submitted_uuid == str(missing)


def test_request_bulk_dedupes_in_flight_and_repeated_input(tmp_path):
    svc, tasks, _repo = _service(tmp_path, batch_size=10)
    same = uuid4()

    assert svc.request_bulk([same, same, str(same)]) == 1
    # 第二次请求同一条：已在途 → 不再排
    assert svc.request_bulk([same]) == 0
    assert len(tasks.submitted) == 1


def test_request_bulk_skips_assets_in_failure_cooldown(tmp_path):
    svc, tasks, _repo = _service(tmp_path)
    asset = str(uuid4())
    svc._failed[asset] = type(
        "R", (), {"failed_at": __import__("time").monotonic()}
    )()

    assert svc.request_bulk([asset]) == 0
    assert tasks.submitted == []


def test_request_single_wraps_bulk(tmp_path):
    svc, tasks, _repo = _service(tmp_path)
    asset = uuid4()

    assert svc.request(asset) is True
    assert len(tasks.submitted) == 1
    # 幂等：同一资产第二次请求返回 False（已在途）
    assert svc.request(asset) is False


def test_stopped_service_accepts_no_new_requests(tmp_path):
    svc, tasks, _repo = _service(tmp_path)
    svc.stop(wait=False)

    assert svc.request_bulk([uuid4()]) == 0
    assert tasks.submitted == []


def test_start_never_submits_anything(tmp_path):
    """启动就是"什么都不做" —— 不能有回填。"""
    svc, tasks, _repo = _service(tmp_path)

    svc.start()

    assert tasks.submitted == [], "on_boot 不得提交任何缩略图任务"


# =========================================================================== #
# ViewService → 装配层 → ThumbnailService
# =========================================================================== #

class _Asset:
    def __init__(self, uuid: UUID) -> None:
        self.uuid = uuid


class _ViewRepo:
    def __init__(self, assets: list[_Asset]) -> None:
        self._assets = assets

    def list_assets(self, album_uuid, offset=0, limit=100):
        return list(self._assets)


def test_view_service_broadcasts_viewed_assets():
    assets = [_Asset(uuid4()) for _ in range(2)]
    view = ViewService(_ViewRepo(assets))
    seen: list[tuple[UUID, ...]] = []
    view.assets_viewed.connect(
        lambda _s, *, asset_uuids: seen.append(asset_uuids)
    )

    view.list_assets(uuid4())

    assert seen == [tuple(a.uuid for a in assets)]


def test_view_service_does_not_broadcast_on_empty_page():
    view = ViewService(_ViewRepo([]))
    seen: list[tuple[UUID, ...]] = []
    view.assets_viewed.connect(
        lambda _s, *, asset_uuids: seen.append(asset_uuids)
    )

    view.list_assets(uuid4())

    assert seen == [], "空页不该发信号（否则缩略图服务每页都被叫一次）"


def test_subscriber_crash_never_breaks_the_read_path():
    """订阅方炸了不能把读路径带崩 —— 列表照常返回。"""
    assets = [_Asset(uuid4())]
    view = ViewService(_ViewRepo(assets))

    def _boom(_sender, *, asset_uuids):
        raise RuntimeError("subscriber exploded")

    view.assets_viewed.connect(_boom)

    assert view.list_assets(uuid4()) == assets


def test_assembly_wires_view_to_thumbnail_bulk_request(monkeypatch, tmp_path):
    """装配层把 ``assets_viewed`` 接到 ``request_bulk(include_deleted=True)``。"""
    from albuswall.service import _wire_view_to_thumbnail
    from albuswall.core import Container

    calls: list[tuple[list[Any], bool]] = []

    class _Thumb:
        def request_bulk(self, uuids, *, include_deleted=False, limit=None):
            calls.append((list(uuids), include_deleted))
            return len(calls)

    container = Container()
    container.set("view_service", ViewService(_ViewRepo([])))
    container.set("thumbnail_service", _Thumb())

    _wire_view_to_thumbnail(container)
    asset = _Asset(uuid4())
    container.get("view_service")._repo._assets = [asset]
    container.get("view_service").list_assets(uuid4())

    assert calls == [([asset.uuid], True)], "回收站预览也要缩略图"
