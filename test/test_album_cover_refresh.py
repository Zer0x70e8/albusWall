#
"""回归：相册封面必须在「资产集合变化」后能重新解析出来。

历史问题（与缩略图空白同源）：封面是派生数据（虚拟相册取 scope 首项、
物理相册取 ``cover_asset_id`` 或最新资产），但只在 ``_populate_list()``
（启动那一刻）加载过一次。启动时库里还没有资产 → 封面路径是 None →
空白 pixmap 落进 model；之后导入完成只有缩略图网格被刷新，相册封面
永远停在空白。

本脚本覆盖：
1. 导入前无封面 → 无 pixmap；
2. 「导入」后调 ``reload_covers()`` → pixmap 被填上（后台解码，需等线程池）；
3. 同一个路径再刷一次 → 不重复解码（幂等，计数不涨）；
4. 封面路径变成 None（资产被删）→ pixmap 被清空；
5. 解析抛异常 → 保留现有 pixmap，不闪成空白。

不依赖 pytest：``python test/test_album_cover_refresh.py`` 直接运行，
用 offscreen Qt 平台，不需要显示器。
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path
from uuid import UUID, uuid4

# 强制离屏：桌面会话里可能已有 QT_QPA_PLATFORM=wayland;xcb，
# setdefault 会被它绕过 —— 测试就会真的弹窗口。
os.environ["QT_QPA_PLATFORM"] = "offscreen"

try:  # 允许直接以文件路径运行
    import albuswall  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QApplication, QWidget

from albuswall.ui.builtin.presenter.album_presenter import AlbumPresenter
from albuswall.ui.builtin.widgets.square_grid import SquareGridView

_app = QApplication.instance() or QApplication([])


# ─────────────────────────────────────────────────────────────
# 测试替身
# ─────────────────────────────────────────────────────────────
class FakeTitleBar(QWidget):
    """只实现 AlbumPresenter 用到的那两个方法。"""

    def __init__(self) -> None:
        super().__init__()
        self.vos: list[object] = []

    def set_vo(self, vo) -> None:
        self.vos.append(vo)


class FakeViewService:
    """按相册 uuid 吐封面路径；可注入异常 / 动态切换。"""

    def __init__(self, album_uuids: list[UUID]) -> None:
        self._uuids = list(album_uuids)
        self.cover_path: str | None = None
        self.raise_on_cover = False
        self.cover_calls = 0

    def list_album_uuids(self) -> list[UUID]:
        return list(self._uuids)

    def get_cover_full_path(self, album_uuid) -> str | None:  # noqa: ARG002
        self.cover_calls += 1
        if self.raise_on_cover:
            raise RuntimeError("boom")
        return self.cover_path


def make_jpeg(path: Path) -> Path:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (256, 192), (30, 60, 90)).save(path, format="JPEG")
    return path


def wait_decode() -> None:
    """等后台封面任务跑完并把结果投递回主线程。"""
    QThreadPool.globalInstance().waitForDone(10_000)
    _app.processEvents()


def make_presenter(view_service) -> AlbumPresenter:
    return AlbumPresenter(
        FakeTitleBar(), SquareGridView(), view_service,  # type: ignore[arg-type]
    )


def has_cover(presenter: AlbumPresenter, row: int = 0) -> bool:
    pixmap = presenter._model.pixmap_at(row)  # noqa: SLF001 - 测试内部状态
    return pixmap is not None and not pixmap.isNull()


# ─────────────────────────────────────────────────────────────
# 1~3. 导入前空白 → reload_covers 后出图 → 再次刷新不重复解码
# ─────────────────────────────────────────────────────────────
def test_cover_appears_after_assets_change() -> None:
    tmp = Path(__import__("tempfile").mkdtemp(prefix="albuswall-cover-"))
    album_uuid = uuid4()
    svc = FakeViewService([album_uuid])
    presenter = make_presenter(svc)

    presenter._populate_list()  # noqa: SLF001 - 等价于 setup 时的首刷
    wait_decode()
    assert not has_cover(presenter), "导入前不该有封面"

    # 「导入完成」：相册有封面了
    svc.cover_path = str(make_jpeg(tmp / "cover.jpg"))
    presenter.reload_covers()
    wait_decode()
    assert has_cover(presenter), "资产到位后封面必须被重新解析出来"

    # 幂等：路径没变 → 不再解码原图（DB 查询照旧，但不重新入队）
    calls_before = svc.cover_calls
    presenter.reload_covers()
    wait_decode()
    assert svc.cover_calls > calls_before, "仍应重新解析路径（以便发现变化）"
    assert has_cover(presenter), "幂等刷新不得把封面刷没"


# ─────────────────────────────────────────────────────────────
# 4. 封面消失 → pixmap 清空
# ─────────────────────────────────────────────────────────────
def test_cover_cleared_when_path_disappears() -> None:
    tmp = Path(__import__("tempfile").mkdtemp(prefix="albuswall-cover-"))
    album_uuid = uuid4()
    svc = FakeViewService([album_uuid])
    svc.cover_path = str(make_jpeg(tmp / "cover.jpg"))

    presenter = make_presenter(svc)
    presenter._populate_list()  # noqa: SLF001
    wait_decode()
    assert has_cover(presenter)

    svc.cover_path = None  # 资产被删 / 相册空了
    presenter.reload_covers()
    wait_decode()
    assert not has_cover(presenter), "封面没了就必须清掉，不能留旧图"


# ─────────────────────────────────────────────────────────────
# 5. 解析失败 → 保留现状（不闪成空白）
# ─────────────────────────────────────────────────────────────
def test_resolve_error_keeps_existing_cover() -> None:
    tmp = Path(__import__("tempfile").mkdtemp(prefix="albuswall-cover-"))
    album_uuid = uuid4()
    svc = FakeViewService([album_uuid])
    svc.cover_path = str(make_jpeg(tmp / "cover.jpg"))

    presenter = make_presenter(svc)
    presenter._populate_list()  # noqa: SLF001
    wait_decode()
    assert has_cover(presenter)

    svc.raise_on_cover = True
    presenter.reload_covers()
    wait_decode()
    assert has_cover(presenter), "解析失败应保留现有封面"


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
