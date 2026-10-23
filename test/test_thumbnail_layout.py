#
"""回归：私有模式与 XDG 模式的缩略图布局同构 + cleaner 配套清理。

本次重构把"私有布局"（``{thumb_root}/{version}/{uuid[:2]}/{uuid}/…``）
改成与 XDG Thumbnail Managing Standard 同构的
``<root>/<spec>/<md5(file_uri)>.png``，唯一差异是根目录常量。本脚本覆盖：

1. 私有模式写盘 → ``{thumb_root}/normal/<md5>.png``、文件 0600、
   父目录 0700、PNG 内嵌 ``Thumb::URI`` 为绝对 ``file://`` URI；
2. XDG 模式同样断言，仅根换成 ``xdg_thumbnails_root()``；
3. 永久失败（源是 .pdf）→ ``{root}/fail/<md5>.png``，第二次 ``_work_one``
   短路返回 ``xdg_fail_marker``；
4. ``purge`` 只清 DB，磁盘 PNG 仍在，cleaner 在源文件存在时不删它；
5. 删源后 cleaner 清掉孤儿，受 ``min_age_seconds`` 保护的存活源不误删；
6. 「缓存已有效 → 只回填路径」的短路分支同样广播 ``thumbnail_ready``
   （否则 UI 侧 submit 撞上去重后永远等不到事件，界面一直空白）。

不依赖 pytest：直接 ``python test/test_thumbnail_layout.py`` 运行。
"""

from __future__ import annotations

import sys
import tempfile
import traceback
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional
from uuid import UUID, uuid4

try:  # 允许直接以文件路径运行
    import albuswall  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import albuswall.services.thumbnail.service as svc_mod
from albuswall.common.enums import ThumbSpec
from albuswall.dto.thumbnail import ThumbnailPaths, ThumbnailTaskInput
from albuswall.services.source_trash.config import ThumbnailCleanerConfig
from albuswall.services.source_trash.thumbnail_cleaner import ThumbnailCleaner
from albuswall.services.thumbnail.service import ThumbnailService
from albuswall.utils.xdg_thumbnail import (
    file_uri,
    thumbnail_filename,
    xdg_fail_path,
)

_DEFAULT_SPEC_MAP: dict[ThumbSpec, str] = {
    ThumbSpec.SMALL: "normal",
    ThumbSpec.MEDIUM: "x-large",
    ThumbSpec.LARGE: "xx-large",
}
_SPECS: dict[ThumbSpec, int] = {
    ThumbSpec.SMALL: 128,
    ThumbSpec.MEDIUM: 512,
    ThumbSpec.LARGE: 1024,
}


# ─────────────────────────────────────────────────────────────
# 测试替身
# ─────────────────────────────────────────────────────────────
class FakeThumbRepo:
    """只实现 ThumbnailService 用到的那几个方法。"""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    def add_asset(
            self,
            asset_uuid: str,
            *,
            source_path: str,
            file_path: str,
    ) -> None:
        self.rows[asset_uuid] = {
            "input": ThumbnailTaskInput(
                asset_id=len(self.rows) + 1,
                uuid=UUID(asset_uuid),
                source_id=1,
                source_path=source_path,
                file_path=file_path,
            ),
            "paths": ThumbnailPaths(),
        }

    def get_task_input_by_uuid(
            self, asset_uuid: str, *, include_deleted: bool = False,
    ) -> Optional[ThumbnailTaskInput]:
        row = self.rows.get(str(asset_uuid))
        return None if row is None else row["input"]

    def get_paths_by_uuid(self, asset_uuid: str) -> Optional[ThumbnailPaths]:
        row = self.rows.get(str(asset_uuid))
        return None if row is None else row["paths"]

    def get_paths_bulk_by_uuid(self, uuids) -> dict[str, ThumbnailPaths]:
        out: dict[str, ThumbnailPaths] = {}
        for u in uuids:
            row = self.rows.get(str(u))
            if row is not None:
                out[str(u)] = row["paths"]
        return out

    def update_paths_by_uuid(
            self,
            asset_uuid: str,
            paths,
            *,
            base_dir=None,
    ) -> int:
        row = self.rows.get(str(asset_uuid))
        if row is None:
            return 0
        rels: dict[str, Optional[str]] = {
            spec.value: rel for spec, rel in paths.items()
        }
        row["paths"] = ThumbnailPaths(
            base=base_dir,
            small=rels.get("small"),
            medium=rels.get("medium"),
            large=rels.get("large"),
        )
        return 1

    def clear_and_snapshot_by_uuid(self, asset_uuid: str) -> Optional[ThumbnailPaths]:
        row = self.rows.get(str(asset_uuid))
        if row is None:
            return None
        snapshot = row["paths"]
        row["paths"] = ThumbnailPaths()
        return snapshot

    def clear_and_snapshot_bulk_by_uuid(self, uuids) -> dict[str, ThumbnailPaths]:
        out: dict[str, ThumbnailPaths] = {}
        for u in uuids:
            snap = self.clear_and_snapshot_by_uuid(u)
            if snap is not None:
                out[str(u)] = snap
        return out


class FakeTaskService:
    """ThumbnailService 构造需要它，但本测试直接调 _work_one，用不到。"""

    def submit(self, *_args, **_kwargs):  # pragma: no cover - 不应被调用
        raise AssertionError("task_service.submit should not be called in this test")


# ─────────────────────────────────────────────────────────────
# 辅助
# ─────────────────────────────────────────────────────────────
def make_temp_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="albuswall-thumb-"))


def make_jpeg(path: Path, color=(10, 20, 30)) -> Path:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 48), color).save(path, format="JPEG")
    return path


@contextmanager
def patched_service_env(
        private_root: Path, xdg_root: Path, xdg_enabled: bool,
) -> Iterator[None]:
    """把 service 模块的"环境依赖"换成测试值。

    这样两种模式都能指向临时目录，且不需要 bootstrap 整个 config 系统。
    配置侧只需给一份 typed dict 快照（``thumbnail_config()`` 的返回值形状），
    ``resolve_*`` 则各自换成固定值 —— 服务只消费归一化后的结果。
    ``image_format`` 故意留 "JPEG"，用来验证它确实不再影响输出（强制 PNG）。
    """
    fake_cfg = {
        "xdg_enabled": xdg_enabled,
        "batch_size": 4,
        "failure_cooldown_sec": 0.0,
        "write_max_retries": 0,
        "write_retry_backoff_sec": 0.0,
    }
    patches = {
        "thumbnail_config": lambda: fake_cfg,
        "resolve_thumb_root": lambda *_args: private_root,
        "xdg_thumbnails_root": lambda *_args: xdg_root,
        "resolve_specs": lambda *_args: dict(_SPECS),
        "resolve_image_format": lambda *_args: "JPEG",
        "resolve_xdg_spec_map": lambda *_args: dict(_DEFAULT_SPEC_MAP),
    }
    saved = {name: getattr(svc_mod, name) for name in patches}
    for name, value in patches.items():
        setattr(svc_mod, name, value)
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(svc_mod, name, value)


def make_service(
        repo: FakeThumbRepo,
        *,
        private_root: Path,
        xdg_root: Path,
        xdg_enabled: bool,
) -> ThumbnailService:
    with patched_service_env(private_root, xdg_root, xdg_enabled):
        return ThumbnailService(
            FakeTaskService(),  # type: ignore[arg-type]
            repo,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
        )


def run_work(
        svc: ThumbnailService,
        repo: FakeThumbRepo,
        asset_uuid: str,
        *,
        private_root: Path,
        xdg_root: Path,
        xdg_enabled: bool,
):
    """在补丁环境下跑一次 _work_one（service 内部读模块级依赖）。"""
    with patched_service_env(private_root, xdg_root, xdg_enabled):
        return svc._work_one(asset_uuid)


def assert_png_layout(
        root: Path, src: Path, *, specs: dict[ThumbSpec, str],
) -> Path:
    """断言某 spec 目录下的 PNG 存在 + 权限 + 内嵌元数据。"""
    from PIL import Image

    name = thumbnail_filename(src)
    checked = 0
    for _spec, spec_dir in specs.items():
        png = root / spec_dir / name
        assert png.is_file(), f"missing thumbnail: {png}"
        assert png.stat().st_mode & 0o777 == 0o600, (
            f"file mode {oct(png.stat().st_mode & 0o777)} != 0600: {png}"
        )
        assert png.parent.stat().st_mode & 0o777 == 0o700, (
            f"dir mode {oct(png.parent.stat().st_mode & 0o777)} != 0700: "
            f"{png.parent}"
        )
        with Image.open(png) as im:
            info = im.info or {}
            uri = info.get("Thumb::URI")
            assert uri is not None, f"no Thumb::URI in {png}"
            assert uri.startswith("file://"), uri
            assert uri == file_uri(src), (uri, file_uri(src))
            assert info.get("Thumb::MTime") is not None
            assert info.get("Thumb::Size") == str(src.stat().st_size)
            assert info.get("Software") == "albuswall"
        checked += 1
    assert checked == len(specs)
    return root / next(iter(specs.values())) / name


def png_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.png") if p.is_file())


# ─────────────────────────────────────────────────────────────
# 1. 私有模式写盘
# ─────────────────────────────────────────────────────────────
def test_private_mode_writes_png_layout() -> None:
    tmp = make_temp_dir()
    private_root = tmp / "private-thumbs"
    xdg_root = tmp / "xdg-thumbs"
    src = make_jpeg(tmp / "src" / "a.jpg")

    repo = FakeThumbRepo()
    asset_uuid = str(uuid4())
    repo.add_asset(asset_uuid, source_path=str(src.parent), file_path=src.name)
    svc = make_service(
        repo, private_root=private_root, xdg_root=xdg_root, xdg_enabled=False,
    )

    result = run_work(
        svc, repo, asset_uuid,
        private_root=private_root, xdg_root=xdg_root,
        xdg_enabled=False,
    )
    assert result.ok, result

    assert_png_layout(private_root, src, specs=_DEFAULT_SPEC_MAP)
    # 旧布局的 .jpg / version 目录不得再出现
    assert not list(private_root.rglob("*.jpg"))
    assert not (private_root / "test-version").exists()
    # DB 回填：base = 根，spec = <spec>/<md5>.png
    paths = repo.get_paths_by_uuid(asset_uuid)
    assert paths is not None and paths.base == str(private_root)
    assert paths.small == f"normal/{thumbnail_filename(src)}"
    assert paths.medium == f"x-large/{thumbnail_filename(src)}"
    assert paths.large == f"xx-large/{thumbnail_filename(src)}"
    # 读取侧 base/rel 拼接即可命中
    assert Path(paths.resolve(ThumbSpec.MEDIUM)).is_file()


# ─────────────────────────────────────────────────────────────
# 2. XDG 模式读写同一套布局（仅根不同）
# ─────────────────────────────────────────────────────────────
def test_xdg_mode_same_layout_other_root() -> None:
    tmp = make_temp_dir()
    private_root = tmp / "private-thumbs"
    xdg_root = tmp / "xdg-thumbs"
    src = make_jpeg(tmp / "src" / "b.jpg")

    repo = FakeThumbRepo()
    asset_uuid = str(uuid4())
    repo.add_asset(asset_uuid, source_path=str(src.parent), file_path=src.name)
    svc = make_service(
        repo, private_root=private_root, xdg_root=xdg_root, xdg_enabled=True,
    )

    result = run_work(
        svc, repo, asset_uuid,
        private_root=private_root, xdg_root=xdg_root,
        xdg_enabled=True,
    )
    assert result.ok, result

    assert_png_layout(xdg_root, src, specs=_DEFAULT_SPEC_MAP)
    assert not private_root.exists(), "XDG mode must not touch private root"
    paths = repo.get_paths_by_uuid(asset_uuid)
    assert paths is not None and paths.base == str(xdg_root)


# ─────────────────────────────────────────────────────────────
# 3. 永久失败 → fail 标记 + 第二次短路
# ─────────────────────────────────────────────────────────────
def test_permanent_failure_writes_fail_marker_and_short_circuits() -> None:
    tmp = make_temp_dir()
    private_root = tmp / "private-thumbs"
    xdg_root = tmp / "xdg-thumbs"
    src = tmp / "src" / "doc.pdf"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"%PDF-1.4 not an image")

    repo = FakeThumbRepo()
    asset_uuid = str(uuid4())
    repo.add_asset(asset_uuid, source_path=str(src.parent), file_path=src.name)
    svc = make_service(
        repo, private_root=private_root, xdg_root=xdg_root, xdg_enabled=False,
    )

    first = run_work(
        svc, repo, asset_uuid,
        private_root=private_root, xdg_root=xdg_root, xdg_enabled=False,
    )
    assert not first.ok and first.retryable is False, first
    marker = xdg_fail_path(src, root=private_root)
    assert marker.is_file(), f"missing fail marker: {marker}"
    assert marker.stat().st_mode & 0o777 == 0o600
    assert marker.parent.name == "fail"

    # 第二次：跨进程 fail 标记短路，不再尝试渲染
    second = run_work(
        svc, repo, asset_uuid,
        private_root=private_root, xdg_root=xdg_root, xdg_enabled=False,
    )
    assert second.ok is False
    assert second.error == "xdg_fail_marker", second.error
    assert second.retryable is False


# ─────────────────────────────────────────────────────────────
# 4. purge 只清 DB；cleaner 在源文件存在时不删
# ─────────────────────────────────────────────────────────────
def test_purge_keeps_files_and_cleaner_keeps_live_sources() -> None:
    tmp = make_temp_dir()
    private_root = tmp / "private-thumbs"
    src = make_jpeg(tmp / "src" / "c.jpg")

    repo = FakeThumbRepo()
    asset_uuid = str(uuid4())
    repo.add_asset(asset_uuid, source_path=str(src.parent), file_path=src.name)
    svc = make_service(
        repo, private_root=private_root, xdg_root=tmp / "xdg", xdg_enabled=False,
    )
    assert run_work(
        svc, repo, asset_uuid,
        private_root=private_root, xdg_root=tmp / "xdg", xdg_enabled=False,
    ).ok

    written = png_files(private_root)
    assert len(written) == len(_DEFAULT_SPEC_MAP)

    assert svc.purge(asset_uuid) is True
    paths = repo.get_paths_by_uuid(asset_uuid)
    assert paths is not None
    assert paths.as_dict() == {
        "base": None, "small": None, "medium": None, "large": None,
    }, paths.as_dict()
    # 文件仍在（共享 md5 文件名，按 asset 删会波及其它引用）
    for f in written:
        assert f.is_file(), f

    cleaner = ThumbnailCleaner(ThumbnailCleanerConfig(
        enabled=True, root=private_root, min_age_seconds=0.0,
    ))
    report = cleaner.scan_and_purge()
    assert report.deleted == 0, report
    for f in written:
        assert f.is_file(), f


# ─────────────────────────────────────────────────────────────
# 5. 删源后 cleaner 清孤儿（两种模式），存活源不误删
# ─────────────────────────────────────────────────────────────
def test_cleaner_removes_orphans_in_both_modes() -> None:
    for xdg_enabled in (False, True):
        tmp = make_temp_dir()
        private_root = tmp / "private-thumbs"
        xdg_root = tmp / "xdg-thumbs"
        root = xdg_root if xdg_enabled else private_root

        a = make_jpeg(tmp / "src" / "live.jpg", (1, 2, 3))
        b = make_jpeg(tmp / "src" / "orphan.jpg", (4, 5, 6))

        repo = FakeThumbRepo()
        uuid_a, uuid_b = str(uuid4()), str(uuid4())
        repo.add_asset(uuid_a, source_path=str(a.parent), file_path=a.name)
        repo.add_asset(uuid_b, source_path=str(b.parent), file_path=b.name)
        svc = make_service(
            repo, private_root=private_root, xdg_root=xdg_root,
            xdg_enabled=xdg_enabled,
        )
        for u in (uuid_a, uuid_b):
            assert run_work(
                svc, repo, u,
                private_root=private_root, xdg_root=xdg_root,
                xdg_enabled=xdg_enabled,
            ).ok

        live_pngs = [
            root / spec_dir / thumbnail_filename(a)
            for spec_dir in _DEFAULT_SPEC_MAP.values()
        ]
        assert all(p.is_file() for p in live_pngs)

        b.unlink()  # 源文件消失 → 其 PNG 成为孤儿

        cleaner = ThumbnailCleaner(ThumbnailCleanerConfig(
            enabled=True, root=root, min_age_seconds=0.0,
        ))
        report = cleaner.scan_and_purge()
        assert report.deleted == len(_DEFAULT_SPEC_MAP), report

        for p in live_pngs:
            assert p.is_file(), f"live thumbnail wrongly deleted: {p}"
        for spec_dir in _DEFAULT_SPEC_MAP.values():
            assert not (root / spec_dir / thumbnail_filename(b)).exists()


def test_min_age_protects_fresh_orphans() -> None:
    tmp = make_temp_dir()
    root = tmp / "private-thumbs"
    src = make_jpeg(tmp / "src" / "d.jpg")
    repo = FakeThumbRepo()
    asset_uuid = str(uuid4())
    repo.add_asset(asset_uuid, source_path=str(src.parent), file_path=src.name)
    svc = make_service(
        repo, private_root=root, xdg_root=tmp / "xdg", xdg_enabled=False,
    )
    assert run_work(
        svc, repo, asset_uuid,
        private_root=root, xdg_root=tmp / "xdg", xdg_enabled=False,
    ).ok
    src.unlink()

    cleaner = ThumbnailCleaner(ThumbnailCleanerConfig(
        enabled=True, root=root, min_age_seconds=600.0,
    ))
    report = cleaner.scan_and_purge()
    assert report.orphans == [], "fresh orphans must be protected by min_age"
    assert report.deleted == 0


# ─────────────────────────────────────────────────────────────
# 6. bootstrap 注入：root 由 thumbnail_config()["xdg_enabled"] 决定
# ─────────────────────────────────────────────────────────────
def test_bootstrap_injects_cleaner_root() -> None:
    import albuswall.services.source_trash.bootstrap as bs

    tmp = make_temp_dir()
    private_root, xdg_root = tmp / "private", tmp / "xdg"
    saved = {
        "thumbnail_config": bs.thumbnail_config,
        "resolve_thumb_root": bs.resolve_thumb_root,
        "xdg_thumbnails_root": bs.xdg_thumbnails_root,
    }
    bs.resolve_thumb_root = lambda *_args: private_root  # type: ignore[assignment]
    bs.xdg_thumbnails_root = lambda *_args: xdg_root  # type: ignore[assignment]
    try:
        bs.thumbnail_config = lambda: {"xdg_enabled": False}  # type: ignore[assignment]
        private_cfg = bs.load_thumbnail_cleaner_config()
        assert private_cfg.enabled is True
        assert private_cfg.root == private_root

        bs.thumbnail_config = lambda: {"xdg_enabled": True}  # type: ignore[assignment]
        xdg_cfg = bs.load_thumbnail_cleaner_config()
        assert xdg_cfg.enabled is True
        assert xdg_cfg.root == xdg_root
    finally:
        for name, value in saved.items():
            setattr(bs, name, value)


# ─────────────────────────────────────────────────────────────
# 7. 「缓存已有效」短路分支也要广播 thumbnail_ready
# ─────────────────────────────────────────────────────────────
def test_cached_short_circuit_still_broadcasts() -> None:
    """回归：第二次 _work_one 走短路，必须仍然广播 thumbnail_ready。

    场景（真实启动路径）：backfill 先提交 → 缩略图早已在盘上 → 短路回填
    路径 → UI 侧 submit 撞 in-flight 去重返回 None，只能等广播。短路分支
    若不广播，缩略图有文件、DB 有路径，界面却永远空白。
    """
    tmp = make_temp_dir()
    private_root = tmp / "private-thumbs"
    xdg_root = tmp / "xdg-thumbs"
    src = make_jpeg(tmp / "src" / "e.jpg")

    repo = FakeThumbRepo()
    asset_uuid = str(uuid4())
    repo.add_asset(asset_uuid, source_path=str(src.parent), file_path=src.name)
    svc = make_service(
        repo, private_root=private_root, xdg_root=xdg_root, xdg_enabled=False,
    )

    events: list[str] = []

    def _on_ready(_sender, *, asset_uuid: str) -> None:
        events.append(asset_uuid)

    signal = ThumbnailService.thumbnail_ready
    signal.connect(_on_ready)
    try:
        assert run_work(
            svc, repo, asset_uuid,
            private_root=private_root, xdg_root=xdg_root, xdg_enabled=False,
        ).ok
        assert events == [asset_uuid], events

        # 第二次：所有 spec 已存在且 mtime 校验通过 → 走短路分支。
        # 把 render 打爆来证明「没有重新渲染」，同时断言仍然广播。
        saved_render = svc_mod.render

        def _no_render(*_a, **_kw):  # pragma: no cover - 只应在坏实现里被调
            raise AssertionError("cached thumbnails must not be re-rendered")

        svc_mod.render = _no_render  # type: ignore[assignment]
        try:
            second = run_work(
                svc, repo, asset_uuid,
                private_root=private_root, xdg_root=xdg_root,
                xdg_enabled=False,
            )
        finally:
            svc_mod.render = saved_render  # type: ignore[assignment]

        assert second.ok, second
        assert events == [asset_uuid, asset_uuid], events
        paths = repo.get_paths_by_uuid(asset_uuid)
        assert paths is not None and paths.resolve(ThumbSpec.SMALL)
        assert Path(paths.resolve(ThumbSpec.SMALL)).is_file()
    finally:
        signal.disconnect(_on_ready)


# ─────────────────────────────────────────────────────────────
# 运行器
# ─────────────────────────────────────────────────────────────
def main() -> int:
    tests = [
        (name, obj)
        for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    failures = 0
    for name, func in tests:
        try:
            func()
        except Exception:  # noqa: BLE001
            failures += 1
            print(f"FAIL {name}")
            traceback.print_exc()
        else:
            print(f"ok   {name}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
