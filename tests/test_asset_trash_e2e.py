#
"""资产回收站：**真 sqlite** 端到端（保留期清理 / 时钟水位 / 缩略图守卫）。

为什么必须有这一组
------------------
``service/asset/trash/`` 之前的覆盖只有替身（``_FakeTrash`` 顶掉整层），于是
下面这些**一条都没被跑过**：

* ``trash_state`` 的水位写入（列名 / 累加 SQL）；
* ``cleanup_expired`` 的 cutoff 计算与 30 天保留期；
* 时钟回拨 / 前跳时"本轮不清理、水位冻结 / 受限推进"；
* ``_delete_thumb_files`` 的**共享布局守卫** —— 这是本轮唯一一处"照搬旧代码
  会删光所有人缩略图"的地方；
* ``empty_trash`` 的七字段报告；
* ``restore_by_uuids`` 遇唯一索引冲突的逐条降级。

替身测不出"列名写错 / JOIN 写反 / 误删共享目录"，所以这里用内存 sqlite +
仓库里那份真 schema。
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from uuid import UUID, uuid4

import pytest

from albuswall.repositories.trash import TrashRepository, TrashStateRepository
from albuswall.service.asset.trash import (
    ADVANCE_PER_CYCLE,
    TRASH_GRACE,
    TrashService,
    utcnow,
)

_SCHEMA = Path(__file__).resolve().parents[1] / (
    "src/albuswall/resources/sql/media_library_schema.sql"
)


class _Db:
    def __init__(self) -> None:
        self._conn = sqlite3.connect(":memory:")
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA.read_text(encoding="utf-8"))
        self._conn.commit()

    def connect(self) -> sqlite3.Connection:
        return self._conn

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        cur = self._conn.execute(sql, params)
        self._conn.commit()
        return cur


@pytest.fixture
def db() -> _Db:
    return _Db()


def _iso(moment: datetime) -> str:
    """与 ``utils.time.format_iso`` 同格式（UTC + 3 位毫秒，无时区后缀）。"""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]


def _insert_asset(
        db: _Db,
        *,
        file_path: Optional[str] = None,
        is_deleted: int = 0,
        deleted_at: Optional[str] = None,
        thumb_path: Optional[str] = None,
        source_id: int = 0,
) -> tuple[int, UUID]:
    asset_uuid = uuid4()
    path = file_path or f"{asset_uuid}.png"
    db.execute(
        "INSERT INTO assets (uuid, file_path, source_id, original_name,"
        " mime_type, file_hash, is_deleted, deleted_at, thumb_path)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (str(asset_uuid), path, source_id, "a.png", "image/png",
         str(asset_uuid), is_deleted, deleted_at, thumb_path),
    )
    row = db.connect().execute(
        "SELECT id FROM assets WHERE uuid = ?", (str(asset_uuid),),
    ).fetchone()
    return int(row["id"]), asset_uuid


def _service(db: _Db, clock: Optional[Callable[[], datetime]] = None) -> TrashService:
    return TrashService(
        TrashRepository(db),
        TrashStateRepository(db),
        clock or utcnow,
    )


def _watermark(db: _Db) -> str:
    return db.connect().execute(
        "SELECT last_observed_at FROM trash_state WHERE id = 1"
    ).fetchone()["last_observed_at"]


# =========================================================================== #
# 1. 保留期清理
# =========================================================================== #

def test_cleanup_expired_purges_only_rows_past_the_grace_period(db: _Db):
    now = datetime.now(timezone.utc)
    old, _ = _insert_asset(
        db, is_deleted=1, deleted_at=_iso(now - TRASH_GRACE - timedelta(days=1)),
    )
    fresh, _ = _insert_asset(
        db, is_deleted=1, deleted_at=_iso(now - timedelta(days=1)),
    )
    alive, _ = _insert_asset(db)

    report = _service(db, clock=lambda: now).cleanup_expired()

    remaining = {
        r["id"] for r in db.connect().execute("SELECT id FROM assets")
    }
    assert remaining == {fresh, alive}, "只清超过保留期的软删行"
    assert len(report.purged) == 1
    assert report.clock_jump.kind == "normal"
    assert old not in remaining


def test_cleanup_expired_advances_the_watermark_and_records_count(db: _Db):
    now = datetime.now(timezone.utc)
    _insert_asset(
        db, is_deleted=1, deleted_at=_iso(now - TRASH_GRACE - timedelta(hours=1)),
    )

    _service(db, clock=lambda: now).cleanup_expired()

    row = db.connect().execute(
        "SELECT last_observed_at, last_cleanup_at, last_cleanup_deleted,"
        "       suspicious_clock_jumps FROM trash_state WHERE id = 1"
    ).fetchone()
    assert row["last_observed_at"] == _iso(now)
    assert row["last_cleanup_at"] == _iso(now)
    assert row["last_cleanup_deleted"] == 1
    assert row["suspicious_clock_jumps"] == 0


# =========================================================================== #
# 2. 时钟防护
# =========================================================================== #

def test_clock_rollback_freezes_the_watermark_and_purges_nothing(db: _Db):
    """用户把时间拨回去：本轮不清理，水位不动（宁可滞留，不可错杀）。"""
    now = datetime.now(timezone.utc)
    watermark = _iso(now)
    db.execute(
        "UPDATE trash_state SET last_observed_at = ? WHERE id = 1", (watermark,),
    )
    victim, _ = _insert_asset(
        db, is_deleted=1,
        deleted_at=_iso(now - TRASH_GRACE - timedelta(days=5)),
    )

    report = _service(
        db, clock=lambda: now - timedelta(hours=1),
    ).cleanup_expired()

    assert report.clock_jump.kind == "backward"
    assert report.purged == []
    assert _watermark(db) == watermark, "水位必须冻结"
    assert db.connect().execute(
        "SELECT COUNT(*) AS c FROM assets WHERE id = ?", (victim,)
    ).fetchone()["c"] == 1


def test_clock_forward_jump_limits_watermark_advance(db: _Db):
    """时间被大幅前跳：本轮不清理，水位只推进 ADVANCE_PER_CYCLE。"""
    now = datetime.now(timezone.utc)
    base = now - timedelta(days=2)
    db.execute(
        "UPDATE trash_state SET last_observed_at = ? WHERE id = 1", (_iso(base),),
    )
    victim, _ = _insert_asset(
        db, is_deleted=1,
        deleted_at=_iso(base - TRASH_GRACE - timedelta(days=5)),
    )

    report = _service(db, clock=lambda: now).cleanup_expired()

    assert report.clock_jump.kind == "forward"
    assert report.purged == [], "前跳当轮不得按新时间批量清理"
    assert _watermark(db) == _iso(base + ADVANCE_PER_CYCLE)
    assert db.connect().execute(
        "SELECT is_deleted FROM assets WHERE id = ?", (victim,)
    ).fetchone()["is_deleted"] == 1


def test_clock_jumps_are_counted(db: _Db):
    now = datetime.now(timezone.utc)
    db.execute(
        "UPDATE trash_state SET last_observed_at = ? WHERE id = 1",
        (_iso(now + timedelta(days=1)),),
    )

    _service(db, clock=lambda: now).cleanup_expired()

    assert db.connect().execute(
        "SELECT suspicious_clock_jumps AS c FROM trash_state WHERE id = 1"
    ).fetchone()["c"] == 1


# =========================================================================== #
# 3. 缩略图守卫（本轮最关键的一条）
# =========================================================================== #

def test_legacy_private_thumb_dir_is_removed(tmp_path: Path, db: _Db):
    """旧私有布局（``<root>/<version>/<uu2>/<uuid>/``）按资产整目录删。"""
    asset_uuid = uuid4()
    legacy_dir = tmp_path / "0.0.1" / str(asset_uuid)[:2] / str(asset_uuid)
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "small.jpg").write_bytes(b"jpg")

    asset_id, _ = _insert_asset(
        db, is_deleted=1,
        deleted_at=_iso(datetime.now(timezone.utc) - TRASH_GRACE
                        - timedelta(days=1)),
        thumb_path=str(legacy_dir),
    )
    db.execute("UPDATE assets SET uuid = ? WHERE id = ?", (str(asset_uuid), asset_id))

    _service(db).cleanup_expired()

    assert not legacy_dir.exists(), "旧布局目录应当被清掉"


def test_shared_thumb_root_is_never_removed(tmp_path: Path, db: _Db):
    """共享布局下 ``thumb_path`` 是全库根：**绝不能**按资产删。

    这条是"照搬旧实现会删光所有人缩略图"的防线：root 里放两个合法 PNG
    （一个属于本资产、一个是别人的），purge 之后根目录与两个文件都必须还在。
    """
    root = tmp_path / "thumbs"
    (root / "normal").mkdir(parents=True)
    mine = root / "normal" / ("a" * 32 + ".png")
    other = root / "normal" / ("b" * 32 + ".png")
    mine.write_bytes(b"png")
    other.write_bytes(b"png")

    _insert_asset(
        db, is_deleted=1,
        deleted_at=_iso(datetime.now(timezone.utc) - TRASH_GRACE
                        - timedelta(days=2)),
        thumb_path=str(root),
    )

    report = _service(db).cleanup_expired()

    assert len(report.purged) == 1, "DB 行照常删"
    assert root.is_dir(), "共享根必须活着"
    assert mine.exists() and other.exists(), "共享 PNG 交给 cleaner，不在这里删"
    # 共享布局不尝试删文件 → 也就不会产生 thumb 失败记录
    assert report.clock_jump.kind == "normal"


def test_xdg_cache_dir_is_protected_even_if_named_like_a_uuid(
        tmp_path: Path, db: _Db, monkeypatch,
):
    """即使目录名恰好等于 uuid，落在 XDG 共享缓存内也拒绝删除。"""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    asset_uuid = uuid4()
    guarded = tmp_path / "thumbnails" / str(asset_uuid)
    guarded.mkdir(parents=True)
    (guarded / "x.png").write_bytes(b"png")

    asset_id, _ = _insert_asset(
        db, is_deleted=1,
        deleted_at=_iso(datetime.now(timezone.utc) - TRASH_GRACE
                        - timedelta(days=2)),
        thumb_path=str(guarded),
    )
    db.execute("UPDATE assets SET uuid = ? WHERE id = ?", (str(asset_uuid), asset_id))

    _service(db).cleanup_expired()

    assert guarded.is_dir(), "XDG 缓存内一律不删（thumb_root 配错时别毁别人数据）"


def test_thumb_removal_failure_is_reported_not_just_logged(
        tmp_path: Path, db: _Db, monkeypatch,
):
    """缩略图删不掉时 ``CleanupReport`` 必须带出来（UI 才能提示残留）。"""
    asset_uuid = uuid4()
    legacy_dir = tmp_path / "thumbs" / str(asset_uuid)
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "small.jpg").write_bytes(b"jpg")

    asset_id, _ = _insert_asset(
        db, is_deleted=1,
        deleted_at=_iso(datetime.now(timezone.utc) - TRASH_GRACE
                        - timedelta(days=3)),
        thumb_path=str(legacy_dir),
    )
    db.execute("UPDATE assets SET uuid = ? WHERE id = ?", (str(asset_uuid), asset_id))

    def _boom(_path, *args, **kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr("shutil.rmtree", _boom)

    report = _service(db).cleanup_expired()

    assert len(report.purged) == 1, "DB 行照常删（文件失败只是垃圾）"
    assert len(report.thumb_failures) == 1
    assert report.thumb_failures[0].asset_id == asset_id
    assert "permission denied" in report.thumb_failures[0].error


# =========================================================================== #
# 4. 清空 / 恢复
# =========================================================================== #

def test_empty_trash_purges_everything_and_reports(db: _Db):
    now = datetime.now(timezone.utc)
    for days in (1, 3, 100):
        _insert_asset(
            db, is_deleted=1, deleted_at=_iso(now - timedelta(days=days)),
        )
    alive, _ = _insert_asset(db)

    report = _service(db).empty_trash()

    assert report.completed is True
    assert report.requested == 3
    assert report.count_after == 0
    assert len(report.purged) == 3
    assert report.skipped == ()
    remaining = {r["id"] for r in db.connect().execute("SELECT id FROM assets")}
    assert remaining == {alive}, "未软删的资产不受影响"


def test_empty_trash_on_empty_db_is_a_clean_noop(db: _Db):
    report = _service(db).empty_trash()

    assert report.purged == ()
    assert report.requested == 0
    assert report.completed is True


def test_restore_falls_back_to_per_id_on_unique_conflict(db: _Db):
    """恢复撞唯一索引 ``(source_id, file_path)``：逐条降级，其余照常恢复。"""
    now = datetime.now(timezone.utc)
    conflict_path = "dup.png"

    # 已软删的两条：一条与在库资产同 (source_id, file_path)，一条不冲突
    _insert_asset(db, file_path=conflict_path)                    # active
    clashing, clashing_uuid = _insert_asset(
        db, file_path=conflict_path, is_deleted=1,
        deleted_at=_iso(now - timedelta(days=1)),
    )
    fine, fine_uuid = _insert_asset(
        db, file_path="fine.png", is_deleted=1,
        deleted_at=_iso(now - timedelta(days=1)),
    )

    result = _service(db).restore_by_uuids([clashing_uuid, fine_uuid])

    assert result.restored == (fine_uuid,), "不冲突的那条要恢复成功"
    assert {c.asset_uuid for c in result.conflicts} == {clashing_uuid}
    rows = {
        r["uuid"]: r["is_deleted"]
        for r in db.connect().execute(
            "SELECT uuid, is_deleted FROM assets WHERE id IN (?, ?)",
            (clashing, fine),
        )
    }
    assert rows[str(fine_uuid)] == 0
    assert rows[str(clashing_uuid)] == 1, "冲突的那条留在回收站，可事后处理"


def test_restore_by_uuid_reports_unknown_ids(db: _Db):
    result = _service(db).restore_by_uuids([uuid4()])

    assert result.restored == ()
    assert len(result.not_found) == 1


# =========================================================================== #
# 5. 调度器：只是投递，不执行
# =========================================================================== #

class _Recorder:
    def __init__(self) -> None:
        self.submitted: list[tuple[Any, dict]] = []

    def submit(self, fn, *args, executor=None, priority=0, **kwargs):
        from concurrent.futures import Future

        self.submitted.append((fn, {"priority": priority}))
        fut: Future = Future()
        fut.cancel()
        return fut


def test_scheduler_submits_cleanup_below_foreground_tasks(db: _Db):
    from albuswall.service.asset.trash import (
        CLEANUP_PRIORITY,
        TrashCleanupScheduler,
    )

    service = _service(db)
    tasks = _Recorder()
    scheduler = TrashCleanupScheduler(
        service, tasks, initial_delay=timedelta(0), interval=timedelta(hours=1),
    )

    fut = scheduler.trigger_now()

    assert fut is not None
    assert tasks.submitted[0][0] == service.cleanup_expired
    assert tasks.submitted[0][1]["priority"] == CLEANUP_PRIORITY
    assert CLEANUP_PRIORITY > 0, "清理必须让位给用户可见任务（默认 0 最高）"


class _ShutdownTasks:
    """TaskService 已拆除：任何 ``submit`` 都抛 ``TaskServiceShutdownError``。

    这是进程收尾期的真实形状——``infrastructure.task`` 的 ``on_exit`` 登记在
    服务层钩子之前，``submit`` 必然失败。
    """

    def __init__(self) -> None:
        self.attempts = 0

    def submit(self, fn, *args, executor=None, priority=0, **kwargs):
        from albuswall.common.exceptions import TaskServiceShutdownError

        self.attempts += 1
        raise TaskServiceShutdownError("TaskService is shut down")


def test_scheduler_gives_up_when_task_service_is_gone(db: _Db):
    """收尾竞态：TaskService 已拆除 → 视为终态，不再重试，也不抛 traceback。"""
    from albuswall.service.asset.trash import TrashCleanupScheduler

    service = _service(db)
    tasks = _ShutdownTasks()
    scheduler = TrashCleanupScheduler(
        service, tasks, initial_delay=timedelta(0), interval=timedelta(0),
    )

    assert scheduler.trigger_now() is None, "拆掉的 TaskService 不可能产出 Future"
    assert tasks.attempts == 1

    # stop_event 已被置位：后续 tick 不再尝试提交。
    assert scheduler.trigger_now() is None
    assert tasks.attempts == 1, "终态之后不许再碰 TaskService"


def test_scheduler_tick_thread_exits_on_shutdown_error(db: _Db):
    """tick 线程必须自己退出，否则进程收尾期间会按周期刷错误日志。"""
    from albuswall.service.asset.trash import TrashCleanupScheduler

    service = _service(db)
    tasks = _ShutdownTasks()
    scheduler = TrashCleanupScheduler(
        service,
        tasks,
        initial_delay=timedelta(0),
        interval=timedelta(seconds=0.01),
    )

    scheduler.start()
    try:
        deadline = time.monotonic() + 5.0
        while _tick_thread_alive():
            assert time.monotonic() < deadline, (
                "tick 线程没有在 TaskService 拆除后退出"
            )
            time.sleep(0.01)
        assert tasks.attempts == 1, "退出前只应尝试提交一次"
    finally:
        scheduler.stop(wait=True)


def _tick_thread_alive() -> bool:
    import threading

    return any(
        t.name == "albuswall-trash-scheduler" and t.is_alive()
        for t in threading.enumerate()
    )
