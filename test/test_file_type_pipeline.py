#
"""两段式文件类型判断 + 三态返回 + 路径防御的验证脚本。

覆盖（对应任务书的测试矩阵）：

Stage 1（粗筛，扫描线程）
  1. ``normalize_file_globs``：裸后缀 / 带点后缀 / 已是 glob / 空串
  2. ``_GlobMatcher``：右锚定语义、后缀快速路径、复杂 glob
  3. ``SourceExecutor._collect_candidate_paths``：只有命中 glob 的进候选，
     且返回值是相对 source_path 的 POSIX 路径

Stage 2（精判，进程池）
  4. ``OFF`` / ``SUFFIX``：伪造后缀也接受，mime 用 guess
  5. ``MAGIC``：.jpg 实为 PNG → mime=image/png
  6. ``MAGIC`` + 随机字节 → skip / magic_unknown
  7. ``MAGIC_LAX`` + 随机字节 → ok / octet-stream
  8. ``HAS_MAGIC=False`` → MAGIC* 降级为 SUFFIX 行为
  9. EXIF 按精判后的 mime 决定是否调用

路径防御
 10. ``_resolve_candidate_path``：反斜杠 / 绝对 / file:// / 空串 / 越界，
     以及相对 / 绝对双形态都能归一

路径契约（落库形态）
 16. 扫描 → 候选表、导入 → 资产表都写相对路径；兼容层同时接受两种形态
 17. ``_SCAN_BATCH_SIZE`` 不超过 ``MAX_PATHS_PER_QUERY``

状态机 / DB / 三态分派
 11. ``PENDING → SKIPPED`` / ``PROCESSING → SKIPPED`` 不抛 ValueError
 12. ``is_void()`` 对 off / magic_lax 放行
 13. ``ingest_source.file_type_check='off'`` 能落库（schema CHECK 已放行）
 14. ``_handle_result`` 三分派 + finalize 内部终态区分
 15. 回归：SUFFIX 模式下行为与改造前一致

不依赖 pytest：直接 ``python test/test_file_type_pipeline.py`` 运行。
（装了 pytest 的话 ``pytest test/test_file_type_pipeline.py`` 也能跑。）
"""

from __future__ import annotations

import logging
import struct
import sys
import tempfile
import traceback
import zlib
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional
from uuid import uuid4

try:  # 允许直接以文件路径运行
    import albuswall  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import albuswall.services.import_ as import_mod
import albuswall.services.source as source_mod
from albuswall.common.enums import CandidateStatus, FileTypeCheckMode
from albuswall.dto.import_ import AssetCandidateCacheDTO, AssetCreateDTO
from albuswall.dto.source import IngestSource, IngestSourceCreate
from albuswall.infrastructure.database import Connector
from albuswall.repositories.import_ import (
    MAX_PATHS_PER_QUERY,
    ImportRepository,
)
from albuswall.services.import_ import (
    ImportService,
    ProcessResult,
    _resolve_candidate_path,
    detect_magic,
    process_candidate,
)
from albuswall.services.source import (
    SourceExecutor,
    _GlobMatcher,
    normalize_file_globs,
)


# ─────────────────────────────────────────────────────────────
# 测试替身
# ─────────────────────────────────────────────────────────────
class FakeImportRepo:
    """内存版 ImportRepository：只实现 ImportService 用到的读写方法。"""

    def __init__(self) -> None:
        self.status: dict[int, CandidateStatus] = {}
        self.finalized: list[tuple[int, AssetCreateDTO]] = []
        self.failures: list[int] = []
        # finalize_candidate 的剧本：True → 返回 uuid（成功）；
        # False → 返回 None 且按 final_status 落终态。
        self.finalize_ok = True
        self.final_status = CandidateStatus.SKIPPED

    # 读
    def recover_stale_candidates(self) -> int:
        return 0

    def get_source_paths(self) -> dict[int, str]:
        return self.source_paths

    def get_pending_candidates(self, limit: Optional[int] = None):
        return self.pending

    def get_candidate_status(self, candidate_id: int):
        return self.status.get(candidate_id)

    # 写
    def update_candidate_status(
            self, candidate_id: int, status: CandidateStatus
    ) -> None:
        current = self.status.get(candidate_id)
        if current is not None and not current.can_transition_to(status):
            raise ValueError(
                f"illegal transition: {current.value} -> {status.value}"
            )
        self.status[candidate_id] = status

    def mark_candidate_failed(self, candidate_id: int) -> None:
        self.failures.append(candidate_id)
        self.update_candidate_status(candidate_id, CandidateStatus.FAILED)

    def finalize_candidate(self, candidate_id: int, dto: AssetCreateDTO):
        self.finalized.append((candidate_id, dto))
        if self.finalize_ok:
            self.status[candidate_id] = CandidateStatus.DONE
            return dto.uuid
        self.status[candidate_id] = self.final_status
        return None

    # 测试侧字段
    source_paths: dict[int, str] = {}
    pending: list[AssetCandidateCacheDTO] = []


class SyncTaskService:
    """submit 立刻同步执行 fn，返回已完成的 Future（不启进程池）。"""

    def __init__(self) -> None:
        self.calls: list[SimpleNamespace] = []
        self.raise_on_submit: Optional[Exception] = None

    def submit(self, fn, *args, executor=None, priority=0, **kwargs) -> Future:
        if self.raise_on_submit is not None:
            raise self.raise_on_submit
        future: Future = Future()
        self.calls.append(SimpleNamespace(fn=fn, args=args))
        try:
            future.set_result(fn(*args))
        except Exception as exc:  # pragma: no cover - 测试替身
            future.set_exception(exc)
        return future


class FakeSourceRepo:
    def __init__(self, sources: Optional[list[IngestSource]] = None) -> None:
        self.sources = sources or []

    def get_source_candidates(self) -> list[IngestSource]:
        return list(self.sources)


# ─────────────────────────────────────────────────────────────
# 辅助构造
# ─────────────────────────────────────────────────────────────
PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    + struct.pack(">I", 13)
    + b"IHDR"
    + struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    + struct.pack(">I", zlib.crc32(b"IHDR" + struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)))
)


def make_temp_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="albuswall-ftype-"))


def write_bytes(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def make_candidate(
        path: str,
        source_id: int = 7,
        mime_type: Optional[str] = None,
        candidate_id: Optional[int] = None,
) -> AssetCandidateCacheDTO:
    return AssetCandidateCacheDTO(
        uuid=uuid4(),
        path=path,
        source_id=source_id,
        mime_type=mime_type,  # type: ignore[arg-type]
        id=candidate_id,
    )


def make_service(
        repo: FakeImportRepo,
        task: SyncTaskService,
        sources: Optional[list[IngestSource]] = None,
) -> ImportService:
    return ImportService(  # type: ignore[arg-type]
        repo,
        task,
        FakeSourceRepo(sources),  # type: ignore[arg-type]
    )


def make_source(source_id: int, source_path: str, file_types: list[str],
                mode: FileTypeCheckMode = FileTypeCheckMode.SUFFIX) -> IngestSource:
    return IngestSource(
        id=source_id,
        title="t",
        source_path=source_path,
        file_types=file_types,
        file_type_check=mode,
    )


# ─────────────────────────────────────────────────────────────
# 1. normalize_file_globs
# ─────────────────────────────────────────────────────────────
def test_normalize_file_globs() -> None:
    assert normalize_file_globs(["jpg"]) == ["*.jpg"]
    assert normalize_file_globs([".JPG"]) == ["*.jpg"]
    assert normalize_file_globs(["*.jpeg"]) == ["*.jpeg"]
    assert normalize_file_globs(["IMG_*.HEIC"]) == ["img_*.heic"]
    assert normalize_file_globs(["jpg", ".jpg", "*.jpg"]) == ["*.jpg"]
    assert normalize_file_globs(["", "  ", None]) == []
    assert normalize_file_globs([]) == []
    assert normalize_file_globs(None) == []


# ─────────────────────────────────────────────────────────────
# 2. _GlobMatcher：右锚定 + 快速路径
# ─────────────────────────────────────────────────────────────
def test_glob_matcher_suffix_fast_path() -> None:
    m = _GlobMatcher(["*.jpg"])
    assert m.matches("a.jpg")
    assert m.matches("A.JPG")
    assert m.matches("sub/a.jpg")          # 右锚定：任意深度
    assert not m.matches("a.jpg.txt")
    assert not m.matches("a.png")


def test_glob_matcher_complex_right_anchored() -> None:
    m = _GlobMatcher(["img_*.jpeg"])
    assert m.matches("img_01.jpeg")
    assert m.matches("sub/img_01.jpeg")     # 右锚定语义（与 *.jpg 一致）
    assert not m.matches("img_01.jpeg.bak")

    m2 = _GlobMatcher(["*/shot/*.png"])
    assert m2.matches("x/shot/y.png")
    assert not m2.matches("x/shot/deep/y.png")


def test_glob_matcher_no_extension_and_empty() -> None:
    assert not _GlobMatcher(["*.jpg"]).matches("README")
    assert not _GlobMatcher([]).matches("a.jpg")
    assert bool(_GlobMatcher(["*.jpg"])) is True
    assert bool(_GlobMatcher([])) is False


def test_glob_matcher_skips_pure_posix_path_when_no_complex() -> None:
    """复杂 glob 列表为空时不得构造 PurePosixPath（零开销快速路径）。"""
    original = source_mod.PurePosixPath

    class _Boom:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            raise AssertionError("PurePosixPath 不应被构造")

    source_mod.PurePosixPath = _Boom  # type: ignore[assignment]
    try:
        m = _GlobMatcher(["*.jpg"])
        assert m.matches("sub/a.jpg") is True
        assert m.matches("sub/a.png") is False
    finally:
        source_mod.PurePosixPath = original  # type: ignore[assignment]

    # 有复杂 glob 时才构造（正向确认上面的 mock 确实起作用）
    source_mod.PurePosixPath = _Boom  # type: ignore[assignment]
    try:
        try:
            _GlobMatcher(["img_*"]).matches("img_1")
        except AssertionError:
            pass
        else:  # pragma: no cover
            raise AssertionError("复杂 glob 应构造 PurePosixPath")
    finally:
        source_mod.PurePosixPath = original  # type: ignore[assignment]


# ─────────────────────────────────────────────────────────────
# 3. Stage 1 集成：_collect_candidate_paths
# ─────────────────────────────────────────────────────────────
def test_collect_candidate_paths_filters_by_glob() -> None:
    root = make_temp_dir()
    files = [
        write_bytes(root / "a.jpg", b"x"),
        write_bytes(root / "sub" / "b.PNG", b"x"),
        write_bytes(root / "c.txt", b"x"),
        write_bytes(root / "sub" / "d.jpg.tmp", b"x"),
    ]

    original = source_mod.iter_files_depth_first
    source_mod.iter_files_depth_first = lambda _root, **kw: iter(files)  # type: ignore[assignment]
    try:
        got = source_mod.SourceExecutor._collect_candidate_paths(
            root, normalize_file_globs(["jpg", "png"]),
        )
    finally:
        source_mod.iter_files_depth_first = original  # type: ignore[assignment]

    expected = ["a.jpg", "sub/b.PNG"]
    assert sorted(got) == sorted(expected), got
    # 契约：返回相对 source_path 的 POSIX 路径（历史实现错误地返回绝对路径）
    assert all(not Path(p).is_absolute() for p in got), got
    assert all(not p.startswith("/") for p in got), got


def test_collect_candidate_paths_empty_rules() -> None:
    root = make_temp_dir()
    write_bytes(root / "a.jpg", b"x")
    assert source_mod.SourceExecutor._collect_candidate_paths(root, []) == []


def test_scan_batch_within_sqlite_variable_limit() -> None:
    """扫描批次必须落在 find_missing_paths 的变量上限内。"""
    assert source_mod._SCAN_BATCH_SIZE <= MAX_PATHS_PER_QUERY, (
        source_mod._SCAN_BATCH_SIZE, MAX_PATHS_PER_QUERY,
    )


# ─────────────────────────────────────────────────────────────
# 4-9. Stage 2 策略
# ─────────────────────────────────────────────────────────────
def test_off_and_suffix_accept_fake_suffix() -> None:
    root = make_temp_dir()
    write_bytes(root / "fake.jpg", b"this is plain text, not a jpeg")

    for mode in (FileTypeCheckMode.OFF, FileTypeCheckMode.SUFFIX):
        result = process_candidate(make_candidate("fake.jpg"), str(root), mode)
        assert result.status == "ok", (mode, result)
        assert result.dto is not None
        assert result.dto.mime_type == "image/jpeg", result.dto.mime_type


def test_magic_detects_real_type() -> None:
    root = make_temp_dir()
    write_bytes(root / "actually_png.jpg", PNG_BYTES)
    result = process_candidate(
        make_candidate("actually_png.jpg"), str(root), FileTypeCheckMode.MAGIC,
    )
    assert result.status == "ok", result
    assert result.dto is not None
    assert result.dto.mime_type == "image/png", result.dto.mime_type


def test_magic_unknown_is_skip() -> None:
    root = make_temp_dir()
    write_bytes(root / "blob.jpg", b"\x00\x01\x02\x03random")
    result = process_candidate(
        make_candidate("blob.jpg"), str(root), FileTypeCheckMode.MAGIC,
    )
    assert result.status == "skip", result
    assert result.reason == "magic_unknown"
    assert result.dto is None


def test_magic_lax_unknown_is_accepted() -> None:
    root = make_temp_dir()
    write_bytes(root / "blob.jpg", b"\x00\x01\x02\x03random")
    result = process_candidate(
        make_candidate("blob.jpg"), str(root), FileTypeCheckMode.MAGIC_LAX,
    )
    assert result.status == "ok", result
    assert result.dto is not None
    assert result.dto.mime_type == "application/octet-stream"


def test_magic_degrades_when_has_magic_false() -> None:
    root = make_temp_dir()
    write_bytes(root / "blob.jpg", b"\x00\x01\x02\x03random")

    original = import_mod.HAS_MAGIC
    import_mod.HAS_MAGIC = False
    try:
        result = process_candidate(
            make_candidate("blob.jpg"), str(root), FileTypeCheckMode.MAGIC,
        )
    finally:
        import_mod.HAS_MAGIC = original

    assert result.status == "ok", result
    assert result.dto is not None
    assert result.dto.mime_type == "image/jpeg"


def test_exif_uses_refined_mime() -> None:
    """EXIF 时机：按精判后的 mime 决定是否调用。"""
    root = make_temp_dir()
    write_bytes(root / "actually_png.jpg", PNG_BYTES)

    calls: list[Path] = []

    def spy(path: Path):
        calls.append(path)
        return 1, 2, "2024-01-01T00:00:00", None

    original_meta = import_mod._extract_image_metadata
    original_flag = import_mod.HAS_EXIF
    import_mod._extract_image_metadata = spy  # type: ignore[assignment]
    import_mod.HAS_EXIF = True
    try:
        result = process_candidate(
            make_candidate("actually_png.jpg"), str(root), FileTypeCheckMode.MAGIC,
        )
    finally:
        import_mod._extract_image_metadata = original_meta  # type: ignore[assignment]
        import_mod.HAS_EXIF = original_flag

    assert result.status == "ok", result
    assert result.dto is not None
    assert result.dto.mime_type == "image/png"
    assert len(calls) == 1, calls
    assert (result.dto.width, result.dto.height) == (1, 2)


def test_exif_not_called_for_non_image() -> None:
    root = make_temp_dir()
    write_bytes(root / "note.jpg", b"plain text")
    calls: list[Path] = []

    def spy(path: Path):
        calls.append(path)
        return 0, 0, None, None

    original_meta = import_mod._extract_image_metadata
    original_flag = import_mod.HAS_EXIF
    import_mod._extract_image_metadata = spy  # type: ignore[assignment]
    import_mod.HAS_EXIF = True
    try:
        # MAGIC 判定为 application/octet-stream → 不该调 EXIF
        result = process_candidate(
            make_candidate("note.jpg"), str(root), FileTypeCheckMode.MAGIC_LAX,
        )
    finally:
        import_mod._extract_image_metadata = original_meta  # type: ignore[assignment]
        import_mod.HAS_EXIF = original_flag

    assert result.status == "ok"
    assert calls == [], calls


def test_detect_magic_returns_none_for_empty_file() -> None:
    root = make_temp_dir()
    empty = write_bytes(root / "empty.bin", b"")
    assert detect_magic(empty) is None
    assert detect_magic(root / "does-not-exist.bin") is None


# ─────────────────────────────────────────────────────────────
# 10. _resolve_candidate_path
# ─────────────────────────────────────────────────────────────
def test_resolve_backslash_relative() -> None:
    root = make_temp_dir()
    target = write_bytes(root / "a" / "b.jpg", b"x")
    got = _resolve_candidate_path(make_candidate("a\\b.jpg"), str(root))
    assert got == target.resolve(), got


def test_resolve_file_uri() -> None:
    root = make_temp_dir()
    target = write_bytes(root / "c.jpg", b"x")
    uri = target.resolve().as_uri()
    got = _resolve_candidate_path(make_candidate(uri), str(root))
    assert got == target.resolve(), got


def test_resolve_empty_and_blank() -> None:
    root = make_temp_dir()
    assert _resolve_candidate_path(make_candidate(""), str(root)) is None
    assert _resolve_candidate_path(make_candidate("   "), str(root)) is None


def test_resolve_escape_is_rejected() -> None:
    root = make_temp_dir()
    (root / "sub").mkdir()
    write_bytes(root.parent / "evil.jpg", b"x")
    assert _resolve_candidate_path(make_candidate("../evil.jpg"), str(root)) is None
    assert _resolve_candidate_path(make_candidate("..\\evil.jpg"), str(root)) is None


def test_resolve_absolute_outside_is_rejected() -> None:
    root = make_temp_dir()
    outside = write_bytes(root.parent / "outside.jpg", b"x")
    got = _resolve_candidate_path(make_candidate(outside.as_posix()), str(root))
    assert got is None, got


def test_resolve_absolute_inside_is_accepted() -> None:
    root = make_temp_dir()
    inside = write_bytes(root / "inside.jpg", b"x")
    got = _resolve_candidate_path(make_candidate(inside.resolve().as_posix()), str(root))
    assert got == inside.resolve(), got


def test_resolve_accepts_relative_and_absolute_forms() -> None:
    """读取侧兼容层：相对（新契约）与绝对（历史脏数据）都能归一。"""
    root = make_temp_dir()
    target = write_bytes(root / "sub" / "dual.jpg", b"x")
    rel_form = "sub/dual.jpg"
    abs_form = target.resolve().as_posix()

    for raw in (rel_form, abs_form):
        got = _resolve_candidate_path(make_candidate(raw), str(root))
        assert got == target.resolve(), (raw, got)

        result = process_candidate(
            make_candidate(raw), str(root), FileTypeCheckMode.SUFFIX,
        )
        assert result.status == "ok", (raw, result)
        assert result.dto is not None
        # 兼容层只归一读取路径，不改造写入值：file_path 仍是 candidate.path
        assert result.dto.file_path == raw


def test_resolve_drive_letter_never_escapes() -> None:
    """盘符形态：要么解析失败，要么仍落在 source 内（不越权读）。"""
    root = make_temp_dir()
    got = _resolve_candidate_path(make_candidate("C:/x.jpg"), str(root))
    resolved_root = root.resolve()
    if got is not None:
        assert resolved_root in got.parents or got == resolved_root, got


def test_resolve_missing_source_path() -> None:
    cand = make_candidate("a.jpg", source_id=7)
    assert _resolve_candidate_path(cand, "") is None


def test_resolve_manual_source_keeps_absolute() -> None:
    """source_id=0（手动导入）没有 source 根：绝对路径按原样归一。"""
    root = make_temp_dir()
    target = write_bytes(root / "manual.jpg", b"x")
    cand = make_candidate(target.resolve().as_posix(), source_id=0)
    assert _resolve_candidate_path(cand, "") == target.resolve()


def test_process_candidate_path_invalid_is_skip() -> None:
    root = make_temp_dir()
    result = process_candidate(make_candidate("../evil.jpg"), str(root),
                               FileTypeCheckMode.SUFFIX)
    assert result.status == "skip", result
    assert result.reason == "path_invalid"


def test_process_candidate_missing_file_is_fail() -> None:
    root = make_temp_dir()
    result = process_candidate(make_candidate("nope.jpg"), str(root),
                               FileTypeCheckMode.SUFFIX)
    assert result.status == "fail", result
    assert result.reason == "missing"


# ─────────────────────────────────────────────────────────────
# 11. 状态机
# ─────────────────────────────────────────────────────────────
def test_state_transitions_allow_skip_and_fail() -> None:
    # skip 路径：当前链路不 claim，候选是 PENDING
    assert CandidateStatus.PENDING.can_transition_to(CandidateStatus.SKIPPED)
    # fail 路径：改前缺失，fail 候选永远停在 pending（既存 bug 的回归点）
    assert CandidateStatus.PENDING.can_transition_to(CandidateStatus.FAILED)
    # claim 之后（PROCESSING）的两条，为未来补 claim 预留
    assert CandidateStatus.PROCESSING.can_transition_to(CandidateStatus.SKIPPED)
    assert CandidateStatus.PROCESSING.can_transition_to(CandidateStatus.FAILED)
    # 不扩大到其他转移：PENDING → DONE 仍由 finalize 直写，不走状态机
    assert not CandidateStatus.PENDING.can_transition_to(CandidateStatus.DONE)
    # 终态 → 任何状态都拒绝
    for terminal in (CandidateStatus.DONE, CandidateStatus.SKIPPED,
                     CandidateStatus.FAILED):
        for target in CandidateStatus:
            assert not terminal.can_transition_to(target), (terminal, target)


def _insert_candidates(conn: Connector, repo: ImportRepository,
                       paths: tuple[str, ...]) -> list[int]:
    """走真实方法插入 pending 候选，返回各自 id。

    禁止直插 SQL 绕过 ``create_candidate_cache_batch``——绕开会掩盖
    "source 不存在 / OR IGNORE 吞掉违约行" 两类真实问题。
    契约：``mime_type`` 必须非 None（``mime_type TEXT NOT NULL``，
    None 会被 ``INSERT OR IGNORE`` 静默丢弃）；source_id=0 是 schema
    自带的 manual 源，恒存活。
    """
    candidates = [make_candidate(p, source_id=0, mime_type="image/jpeg")
                  for p in paths]
    assert repo.create_candidate_cache_batch(candidates) == len(paths)
    ids: list[int] = []
    for cand in candidates:
        row = repo._fetchone(
            "SELECT id FROM asset_candidate_cache WHERE uuid = ?",
            (str(cand.uuid),),
        )
        assert row is not None, f"候选未真正入库: {cand.path}"
        ids.append(int(row["id"]))
    return ids


def test_repo_update_status_pending_to_skipped() -> None:
    conn = Connector()
    repo = ImportRepository(conn)
    (row_id,) = _insert_candidates(conn, repo, ("rel/a.jpg",))

    assert repo.get_candidate_status(row_id) is CandidateStatus.PENDING
    repo.update_candidate_status(row_id, CandidateStatus.SKIPPED)
    assert repo.get_candidate_status(row_id) is CandidateStatus.SKIPPED


def test_repo_update_status_pending_to_failed() -> None:
    """既存 bug 的回归测试：改前 PENDING → FAILED 抛 ValueError，
    失败候选永远停在 pending，被下一轮反复捞出重试。"""
    conn = Connector()
    repo = ImportRepository(conn)
    (row_id,) = _insert_candidates(conn, repo, ("rel/f.jpg",))

    assert repo.get_candidate_status(row_id) is CandidateStatus.PENDING
    repo.mark_candidate_failed(row_id)
    assert repo.get_candidate_status(row_id) is CandidateStatus.FAILED


def test_repo_update_status_processing_to_skipped_and_failed() -> None:
    conn = Connector()
    repo = ImportRepository(conn)
    skip_id, fail_id = _insert_candidates(
        conn, repo, ("rel/b.jpg", "rel/c.jpg"),
    )

    repo.update_candidate_status(skip_id, CandidateStatus.PROCESSING)
    repo.update_candidate_status(skip_id, CandidateStatus.SKIPPED)
    assert repo.get_candidate_status(skip_id) is CandidateStatus.SKIPPED

    repo.update_candidate_status(fail_id, CandidateStatus.PROCESSING)
    repo.update_candidate_status(fail_id, CandidateStatus.FAILED)
    assert repo.get_candidate_status(fail_id) is CandidateStatus.FAILED


def test_repo_rejects_terminal_transition() -> None:
    conn = Connector()
    repo = ImportRepository(conn)
    (row_id,) = _insert_candidates(conn, repo, ("rel/t.jpg",))
    repo.update_candidate_status(row_id, CandidateStatus.SKIPPED)
    try:
        repo.update_candidate_status(row_id, CandidateStatus.FAILED)
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("终态转移应抛 ValueError")


# ─────────────────────────────────────────────────────────────
# 11b. create_candidate_cache_batch：真实方法（禁止直插 SQL 绕过）
# ─────────────────────────────────────────────────────────────
def _make_live_source(
        conn: Connector, source_id: int, source_path: str = "/tmp",
) -> None:
    """创建存活 source（is_deleted=0）——create_candidate_cache_batch 的前置。"""
    with conn.connect() as c:
        c.execute(
            "INSERT INTO ingest_source "
            "(id, title, source_path, file_type_check, file_types) "
            "VALUES (?, 't', ?, 'suffix', '[]')",
            (source_id, source_path),
        )
        c.commit()


def test_batch_insert_commits_and_counts_rows() -> None:
    conn = Connector()
    repo = ImportRepository(conn)
    _make_live_source(conn, 7)

    candidates = [make_candidate(f"rel/{i}.jpg", source_id=7,
                                 mime_type="image/jpeg") for i in range(3)]
    assert repo.create_candidate_cache_batch(candidates) == 3
    assert repo._fetchone(
        "SELECT COUNT(*) AS c FROM asset_candidate_cache WHERE source_id = 7", (),
    )["c"] == 3


def test_batch_insert_drops_candidates_for_dead_source() -> None:
    """kept 为空：返回 0、表内无新增（验证 _live_source_ids 过滤，不是 commit）。"""
    conn = Connector()
    repo = ImportRepository(conn)
    # 只建源再软删 → 源不再"存活"
    _make_live_source(conn, 9)
    with conn.connect() as c:
        c.execute("UPDATE ingest_source SET is_deleted = 1 WHERE id = 9")
        c.commit()

    before = repo._fetchone(
        "SELECT COUNT(*) AS c FROM asset_candidate_cache", (),
    )["c"]
    n = repo.create_candidate_cache_batch(
        [make_candidate("rel/dead.jpg", source_id=9, mime_type="image/jpeg")],
    )
    assert n == 0, n
    after = repo._fetchone(
        "SELECT COUNT(*) AS c FROM asset_candidate_cache", (),
    )["c"]
    assert after == before, (before, after)


def test_batch_insert_or_ignore_dropped_duplicates() -> None:
    """重复 (source_id, path) 被 OR IGNORE 静默丢弃：返回值是提交尝试数。"""
    conn = Connector()
    repo = ImportRepository(conn)
    _make_live_source(conn, 11)

    first = [make_candidate("rel/dup.jpg", source_id=11, mime_type="image/jpeg")]
    assert repo.create_candidate_cache_batch(first) == 1
    second = [make_candidate("rel/dup.jpg", source_id=11, mime_type="image/jpeg")]
    assert repo.create_candidate_cache_batch(second) == 1  # 提交尝试数
    assert repo._fetchone(
        "SELECT COUNT(*) AS c FROM asset_candidate_cache WHERE path = 'rel/dup.jpg'",
        (),
    )["c"] == 1


def test_batch_insert_or_ignore_masks_notnull_violation() -> None:
    """既存陷阱的真实成因：mime_type=None 撞 NOT NULL，被 OR IGNORE 吞掉。

    既不是"事务没提交"，也不是 ``_transaction`` 的问题——裸 INSERT 同一行
    会抛 IntegrityError，而 ``INSERT OR IGNORE`` 静默跳过，返回值仍是提交
    尝试数。``AssetCandidateCacheDTO.mime_type`` 声明为 ``str``，传 None
    属调用方违约。
    """
    conn = Connector()
    repo = ImportRepository(conn)
    _make_live_source(conn, 13)

    n = repo.create_candidate_cache_batch(
        [make_candidate("rel/bad.jpg", source_id=13, mime_type=None)],  # type: ignore[arg-type]
    )
    assert n == 1  # 提交尝试数
    assert repo._fetchone(
        "SELECT COUNT(*) AS c FROM asset_candidate_cache WHERE path = 'rel/bad.jpg'",
        (),
    )["c"] == 0  # 实际一行都没入库


# ─────────────────────────────────────────────────────────────
# 11c. 路径契约：扫描 / 导入落库都必须是相对路径
# ─────────────────────────────────────────────────────────────
def _scan_into_db(
        root: Path, source_id: int,
) -> tuple[Connector, ImportRepository]:
    """真实扫描一份临时目录，把候选写进内存库；返回 (conn, repo)。"""
    conn = Connector()
    repo = ImportRepository(conn)
    _make_live_source(conn, source_id, str(root.resolve()))
    executor = SourceExecutor(
        source=make_source(source_id, str(root), ["jpg", "png"]),
        repo=repo,
        seq=1,
    )
    result = executor.run()
    assert result.error is None, result.error
    assert result.inserted_count == 2, result
    return conn, repo


def test_scan_writes_relative_candidate_paths() -> None:
    """验收 1：新扫描的 source，候选表 path 无前导 /、无盘符。"""
    root = make_temp_dir()
    write_bytes(root / "a.jpg", b"x")
    write_bytes(root / "sub" / "b.png", b"x")

    _conn, repo = _scan_into_db(root, 21)
    rows = repo._fetchall(
        "SELECT path FROM asset_candidate_cache WHERE source_id = 21 "
        "ORDER BY path", (),
    )
    paths = [row["path"] for row in rows]
    assert paths == ["a.jpg", "sub/b.png"], paths
    assert all(not Path(p).is_absolute() for p in paths)
    assert all(not p.startswith("/") and ":" not in p for p in paths)


def test_scan_then_import_keeps_relative_file_paths() -> None:
    """验收 2：走完一轮 import，assets.file_path 同样是相对路径。"""
    root = make_temp_dir()
    write_bytes(root / "a.jpg", b"x")
    write_bytes(root / "sub" / "b.png", b"x")

    _conn, repo = _scan_into_db(root, 23)
    candidates = repo.get_pending_candidates()
    assert len(candidates) == 2, candidates

    for cand in candidates:
        result = process_candidate(
            cand, str(root.resolve()), FileTypeCheckMode.SUFFIX,
        )
        assert result.status == "ok", (cand.path, result)
        assert result.dto is not None
        assert repo.finalize_candidate(cand.id, result.dto) == cand.uuid

    rows = repo._fetchall(
        "SELECT file_path FROM assets WHERE source_id = 23 ORDER BY file_path", (),
    )
    paths = [row["file_path"] for row in rows]
    assert paths == ["a.jpg", "sub/b.png"], paths
    assert all(not Path(p).is_absolute() for p in paths)


def test_process_candidate_accepts_absolute_and_relative_rows() -> None:
    """验收 4：库里同时存绝对 / 相对两种形态，process_candidate 都能成功。"""
    root = make_temp_dir()
    target = write_bytes(root / "sub" / "dual.jpg", b"x")
    resolved_root = root.resolve()

    conn = Connector()
    repo = ImportRepository(conn)
    _make_live_source(conn, 25, str(resolved_root))

    rel_form = "sub/dual.jpg"
    abs_form = target.resolve().as_posix()
    assert repo.create_candidate_cache_batch([
        make_candidate(rel_form, source_id=25, mime_type="image/jpeg"),
        make_candidate(abs_form, source_id=25, mime_type="image/jpeg"),
    ]) == 2

    pending = repo.get_pending_candidates()
    assert sorted(c.path for c in pending) == sorted([rel_form, abs_form])
    for cand in pending:
        result = process_candidate(
            cand, str(resolved_root), FileTypeCheckMode.SUFFIX,
        )
        assert result.status == "ok", (cand.path, result)
        assert result.dto is not None
        assert result.dto.file_path == cand.path
        assert Path(result.dto.file_path).name == "dual.jpg"


def test_find_missing_paths_matches_relative_storage() -> None:
    """find_missing_paths 的入参必须是表内存储形式（相对路径）。"""
    conn = Connector()
    repo = ImportRepository(conn)
    _make_live_source(conn, 27)
    assert repo.create_candidate_cache_batch([
        make_candidate("rel/x.jpg", source_id=27, mime_type="image/jpeg"),
    ]) == 1

    assert repo.find_missing_paths(27, "rel/x.jpg", "rel/y.jpg") == ["rel/y.jpg"]
    # 绝对路径形态对不上表内相对路径 —— 这正是本次修复要消除的错位
    assert repo.find_missing_paths(
        27, (Path("/tmp/rel/x.jpg")).as_posix(),
    ) == ["/tmp/rel/x.jpg"]


# ─────────────────────────────────────────────────────────────
# 12. is_void
# ─────────────────────────────────────────────────────────────
def test_is_void_allows_off_and_magic_lax() -> None:
    root = make_temp_dir()
    for mode in (FileTypeCheckMode.OFF, FileTypeCheckMode.MAGIC_LAX,
                 FileTypeCheckMode.SUFFIX, FileTypeCheckMode.MAGIC):
        src = make_source(1, str(root), ["jpg"], mode)
        assert src.is_void() is False, mode
    assert make_source(1, str(root), ["jpg"], FileTypeCheckMode.NONE).is_void() is True


def test_create_dto_accepts_new_modes() -> None:
    for mode in (FileTypeCheckMode.OFF, FileTypeCheckMode.MAGIC_LAX):
        dto = IngestSourceCreate(
            title="t", source_path="/tmp/x", file_type_check=mode,
        )
        assert dto.file_type_check is mode


# ─────────────────────────────────────────────────────────────
# 13. DB CHECK 放行 off / magic_lax
# ─────────────────────────────────────────────────────────────
def test_schema_check_allows_new_modes() -> None:
    conn = Connector()
    with conn.connect() as c:
        for mode in ("off", "magic_lax", "suffix", "magic"):
            c.execute(
                "INSERT INTO ingest_source "
                "(title, source_path, file_type_check, file_types) "
                "VALUES (?, ?, ?, '[]')",
                (f"t-{mode}", "/tmp/x", mode),
            )
        c.commit()

    with conn.connect() as c:
        try:
            c.execute(
                "INSERT INTO ingest_source "
                "(title, source_path, file_type_check, file_types) "
                "VALUES ('bad', '/tmp/x', 'nonsense', '[]')",
            )
        except Exception as exc:
            assert "CHECK" in str(exc).upper(), exc
        else:  # pragma: no cover
            raise AssertionError("非法 file_type_check 应被 CHECK 拒绝")
        c.rollback()


def test_candidate_cache_has_no_new_columns() -> None:
    """存储层不变：候选表没有 type_check / skip_reason 列。"""
    conn = Connector()
    with conn.connect() as c:
        cols = {row["name"] for row in c.execute(
            "PRAGMA table_info(asset_candidate_cache)"
        )}
    assert "type_check" not in cols
    assert "skip_reason" not in cols


# ─────────────────────────────────────────────────────────────
# 14. _handle_result 三分派
# ─────────────────────────────────────────────────────────────
def test_service_passes_type_check_from_snapshot() -> None:
    root = make_temp_dir()
    write_bytes(root / "blob.jpg", b"\x00\x01\x02random")

    cand = make_candidate("blob.jpg", source_id=7, candidate_id=1)
    repo = FakeImportRepo()
    repo.status[1] = CandidateStatus.PENDING
    repo.pending = [cand]
    repo.source_paths = {7: str(root)}
    task = SyncTaskService()
    service = make_service(
        repo, task, [make_source(7, str(root), ["jpg"], FileTypeCheckMode.MAGIC)],
    )

    totals = {"ok": 0, "failed": 0, "skipped": 0, "retry": 0, "timeout": 0}
    source_map = service._load_source_map()
    assert list(source_map) == [7]

    service._process_batch([cand], repo.source_paths, source_map, totals)

    # 提交时带上了 type_check（位置参数第 3 个）
    assert len(task.calls) == 1
    assert task.calls[0].args[2] is FileTypeCheckMode.MAGIC
    # 精判拒绝 → skipped，且不是 failed
    assert totals["skipped"] == 1, totals
    assert totals["failed"] == 0, totals
    assert repo.failures == [], repo.failures


def test_service_fallback_type_check_for_manual_source() -> None:
    root = make_temp_dir()
    write_bytes(root / "a.jpg", b"x")
    cand = make_candidate("a.jpg", source_id=0)
    repo = FakeImportRepo()
    repo.source_paths = {0: ""}
    task = SyncTaskService()
    service = make_service(repo, task, [])  # 快照里没有 manual source

    totals = {"ok": 0, "failed": 0, "skipped": 0, "retry": 0, "timeout": 0}
    service._process_batch([cand], repo.source_paths,
                           service._load_source_map(), totals)
    assert task.calls[0].args[2] is FileTypeCheckMode.SUFFIX


def test_handle_result_skip_marks_skipped() -> None:
    cand = make_candidate("a.jpg")
    repo = FakeImportRepo()
    repo.status[1] = CandidateStatus.PENDING
    service = make_service(repo, SyncTaskService())

    future: Future = Future()
    future.set_result(ProcessResult("skip", reason="magic_unknown"))
    totals = {"ok": 0, "failed": 0, "skipped": 0, "retry": 0, "timeout": 0}

    service._handle_result(SimpleNamespace(id=1, uuid=cand.uuid), future, totals)
    assert totals["skipped"] == 1
    assert repo.status[1] is CandidateStatus.SKIPPED


def test_handle_result_fail_marks_failed() -> None:
    repo = FakeImportRepo()
    # 当前链路不 claim，候选是 PENDING —— 改前 PENDING → FAILED 抛 ValueError，
    # 候选永远停在 pending（既存 bug 的回归点）
    repo.status[2] = CandidateStatus.PENDING
    service = make_service(repo, SyncTaskService())

    future: Future = Future()
    future.set_result(ProcessResult("fail", reason="missing"))
    totals = {"ok": 0, "failed": 0, "skipped": 0, "retry": 0, "timeout": 0}

    service._handle_result(SimpleNamespace(id=2, uuid=uuid4()), future, totals)
    assert totals["failed"] == 1
    assert repo.failures == [2]


def test_handle_result_ok_distinguishes_finalize_outcomes() -> None:
    # case A：finalize 成功
    repo = FakeImportRepo()
    repo.status[3] = CandidateStatus.PENDING
    service = make_service(repo, SyncTaskService())
    dto = SimpleNamespace(uuid=uuid4())
    future: Future = Future()
    future.set_result(ProcessResult("ok", dto=dto))  # type: ignore[arg-type]
    totals = {"ok": 0, "failed": 0, "skipped": 0, "retry": 0, "timeout": 0}
    service._handle_result(SimpleNamespace(id=3, uuid=dto.uuid), future, totals)
    assert totals["ok"] == 1, totals

    # case B：finalize 返回 None 且终态是 SKIPPED（重复导入）
    repo2 = FakeImportRepo()
    repo2.status[4] = CandidateStatus.PENDING
    repo2.finalize_ok = False
    repo2.final_status = CandidateStatus.SKIPPED
    service2 = make_service(repo2, SyncTaskService())
    future2: Future = Future()
    future2.set_result(ProcessResult("ok", dto=SimpleNamespace(uuid=uuid4())))  # type: ignore[arg-type]
    totals2 = {"ok": 0, "failed": 0, "skipped": 0, "retry": 0, "timeout": 0}
    service2._handle_result(SimpleNamespace(id=4, uuid=uuid4()), future2, totals2)
    assert totals2["skipped"] == 1, totals2
    assert totals2["failed"] == 0, totals2

    # case C：finalize 返回 None 且终态是 FAILED（真失败）
    repo3 = FakeImportRepo()
    repo3.status[5] = CandidateStatus.PENDING
    repo3.finalize_ok = False
    repo3.final_status = CandidateStatus.FAILED
    service3 = make_service(repo3, SyncTaskService())
    future3: Future = Future()
    future3.set_result(ProcessResult("ok", dto=SimpleNamespace(uuid=uuid4())))  # type: ignore[arg-type]
    totals3 = {"ok": 0, "failed": 0, "skipped": 0, "retry": 0, "timeout": 0}
    service3._handle_result(SimpleNamespace(id=5, uuid=uuid4()), future3, totals3)
    assert totals3["failed"] == 1, totals3


def test_safe_mark_skipped_falls_back_to_failed() -> None:
    class BrokenRepo(FakeImportRepo):
        def update_candidate_status(self, candidate_id, status) -> None:
            raise RuntimeError("db down")

    repo = BrokenRepo()
    repo.status[9] = CandidateStatus.PENDING
    service = make_service(repo, SyncTaskService())
    service._safe_mark_skipped(9, "magic_unknown")
    assert repo.failures == [9]


# ─────────────────────────────────────────────────────────────
# 14b. fail 真正落库（改前 fail 候选停在 PENDING）
# ─────────────────────────────────────────────────────────────
def test_process_batch_fail_lands_failed_from_pending() -> None:
    """mock process_candidate 返回 fail → 候选 PENDING → FAILED（改前停在 PENDING）。"""
    cand = make_candidate("a.jpg", source_id=7, candidate_id=1)
    repo = FakeImportRepo()
    repo.status[1] = CandidateStatus.PENDING
    repo.source_paths = {7: "/tmp"}
    service = make_service(repo, SyncTaskService(),
                           [make_source(7, "/tmp", ["jpg"])])

    original = import_mod.process_candidate
    import_mod.process_candidate = (  # type: ignore[assignment]
        lambda *args, **kwargs: ProcessResult("fail", reason="missing")
    )
    totals = {"ok": 0, "failed": 0, "skipped": 0, "retry": 0, "timeout": 0}
    try:
        service._process_batch([cand], repo.source_paths,
                               service._load_source_map(), totals)
    finally:
        import_mod.process_candidate = original  # type: ignore[assignment]

    assert totals["failed"] == 1, totals
    assert repo.status[1] is CandidateStatus.FAILED, repo.status
    assert repo.failures == [1]


def test_process_batch_worker_exception_lands_failed() -> None:
    """worker 抛异常 → 候选落 FAILED，日志 reason=worker_exception。"""
    cand = make_candidate("a.jpg", source_id=7, candidate_id=1)
    repo = FakeImportRepo()
    repo.status[1] = CandidateStatus.PENDING
    repo.source_paths = {7: "/tmp"}
    service = make_service(repo, SyncTaskService(),
                           [make_source(7, "/tmp", ["jpg"])])

    def boom(*args, **kwargs):
        raise RuntimeError("worker blew up")

    original = import_mod.process_candidate
    import_mod.process_candidate = boom  # type: ignore[assignment]
    totals = {"ok": 0, "failed": 0, "skipped": 0, "retry": 0, "timeout": 0}
    records: list[logging.LogRecord] = []

    class _Grab(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Grab(level=logging.DEBUG)
    logger = logging.getLogger("albuswall.services.import_")
    logger.addHandler(handler)
    old_level = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        service._process_batch([cand], repo.source_paths,
                               service._load_source_map(), totals)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)
        import_mod.process_candidate = original  # type: ignore[assignment]

    assert totals["failed"] == 1, totals
    assert repo.status[1] is CandidateStatus.FAILED, repo.status
    assert any("worker_exception" in r.getMessage() for r in records), \
        [r.getMessage() for r in records]


def test_safe_mark_failed_does_not_raise_on_db_error() -> None:
    class BrokenRepo(FakeImportRepo):
        def mark_candidate_failed(self, candidate_id) -> None:
            raise RuntimeError("db down")

    service = make_service(BrokenRepo(), SyncTaskService())
    service._safe_mark_failed(42, "missing")  # 不上抛


# ─────────────────────────────────────────────────────────────
# 15. 回归：SUFFIX 行为不变
# ─────────────────────────────────────────────────────────────
def test_suffix_regression_end_to_end() -> None:
    root = make_temp_dir()
    write_bytes(root / "a.jpg", b"x")
    write_bytes(root / "b.png", b"x")
    write_bytes(root / "c.txt", b"x")

    files = [root / "a.jpg", root / "b.png", root / "c.txt"]
    original = source_mod.iter_files_depth_first
    source_mod.iter_files_depth_first = lambda _root, **kw: iter(files)  # type: ignore[assignment]
    try:
        got = source_mod.SourceExecutor._collect_candidate_paths(
            root, normalize_file_globs([".jpg", ".png"]),
        )
    finally:
        source_mod.iter_files_depth_first = original  # type: ignore[assignment]

    assert sorted(Path(p).name for p in got) == ["a.jpg", "b.png"], got
    # Stage 1 现在返回相对路径
    assert all(not Path(p).is_absolute() for p in got), got

    cand = make_candidate("a.jpg", source_id=7)
    result = process_candidate(cand, str(root), FileTypeCheckMode.SUFFIX)
    assert result.status == "ok"
    assert result.dto is not None
    # file_path 沿用 candidate.path 原值（Stage 1 写相对路径 → 落库即相对路径）
    assert result.dto.file_path == "a.jpg"
    assert result.dto.mime_type == "image/jpeg"


# ─────────────────────────────────────────────────────────────
# 入口
# ─────────────────────────────────────────────────────────────
def main() -> int:
    tests = [
        (name, obj)
        for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    failures: list[tuple[str, BaseException]] = []
    for name, fn in tests:
        try:
            fn()
        except BaseException as exc:  # noqa: BLE001
            failures.append((name, exc))
            print(f"FAIL {name}: {exc}")
            traceback.print_exc()
        else:
            print(f"ok   {name}")

    print()
    if failures:
        print(f"{len(failures)}/{len(tests)} 个用例失败")
        return 1
    print(f"全部 {len(tests)} 个用例通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
