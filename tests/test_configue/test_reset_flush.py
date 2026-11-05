#
"""``flush`` 的收尾顺序：``mark_clean`` 失败时 ``_reset_pending`` 仍要清掉。

背景（审计 A2 的结论）：``_flush_impl`` 在写盘成功之后要做两件事——把节点
移出 ``_dirty``（``mark_clean``）和清掉「待删行」标记
（``clear_reset_pending``）。两条都是**内存收尾**，不该因为其中一条抛异常
就漏掉另一条：文件里那一行已经删了、内存却还标着「待删行」的话，下次
``flush`` 会对同一个 key 再发一次 ``DELETE``。

``mark_clean`` 只做 ``_dirty.discard``，实际几乎不可能抛；本文件用
monkeypatch 把它强行弄炸，锁住「即使如此，标记也一定被清」这条契约。

用例自带 ``Configue`` 隔离（登记表是进程全局的），互不依赖执行顺序。
"""

from __future__ import annotations

from contextlib import suppress
from pathlib import Path

import pytest

from albuswall.common.keymap import SECTION
from albuswall.configue import Configue
from albuswall.configue.bootstrap import (
    SHORTCUT_DEFAULTS_FILE,
    SHORTCUT_USER_FILE,
    setup_config,
)
from albuswall.configue.registry import (
    _CLASSES,
    _FIELD_OWNER,
    _FILES,
    _PATHS,
)
from albuswall.configue.utils.ini_transform import DELETE
from albuswall.core import Container

from .conftest import write_ini


def _reset_registry() -> None:
    for table in (_FILES, _CLASSES, _PATHS, _FIELD_OWNER):
        table.clear()


@pytest.fixture
def config(tmp_path: Path):
    """portable 配置：随包默认表 + 用户表（``app.quit`` 被覆盖）。"""
    previous = Configue._instance
    _reset_registry()
    write_ini(tmp_path / "config.ini", "[ui]\ntheme = default\n")
    write_ini(
        tmp_path / "shortcuts.ini",
        f'[{SECTION}]\napp.quit = ["Ctrl+Alt+Q"]\n',
    )
    instance = setup_config(Container(), base_dir=tmp_path, portable=True)
    try:
        yield instance
    finally:
        with suppress(Exception):
            instance.close()
        Configue._instance = previous
        _reset_registry()


@pytest.fixture
def flush_edits(config: Configue, monkeypatch):
    """捕获每次 ``flush`` 交给后端的 ``{文件: {section: {key: 值|DELETE}}}``。"""
    import albuswall.configue.configue as configue_mod

    captured: dict[str, dict] = {}
    real_get_backend = configue_mod.get_backend

    def spy_get_backend(fmt):
        backend = real_get_backend(fmt)

        class _Spy:
            def write(self, path, edits):
                captured[str(path)] = edits
                return backend.write(path, edits)

            def __getattr__(self, item):
                return getattr(backend, item)

        return _Spy()

    monkeypatch.setattr(configue_mod, "get_backend", spy_get_backend)
    return captured


def test_mark_clean_failure_still_clears_reset_pending(
        config: Configue, flush_edits, tmp_path: Path,
) -> None:
    """``mark_clean`` 抛异常：``_reset_pending`` 清掉、dirty 保留、文件已删行。"""
    node = (SECTION, "app.quit")
    assert config.reset(*node) is True
    assert config.resolver.is_reset_pending(node) is True

    def boom(nodes):
        raise RuntimeError("mark_clean exploded (simulated)")

    config.resolver.mark_clean = boom  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError, match="mark_clean exploded"):
            config.flush(file=SHORTCUT_USER_FILE)
    finally:
        # 恢复真实实现，后面的 flush 才有意义
        del config.resolver.mark_clean  # type: ignore[attr-defined]

    # 1) 文件已经写成功（那一行被 DELETE）
    user_path = str(config.base_dir / "shortcuts.ini")
    assert flush_edits[user_path][SECTION]["app.quit"] is DELETE
    assert "app.quit" not in (tmp_path / "shortcuts.ini").read_text(
        encoding="utf-8")

    # 2) finally 生效：标记清掉了，不会重复发 DELETE
    assert config.resolver.is_reset_pending(node) is False
    # 3) mark_clean 失败 → dirty 保留（不知道有没有落干净，宁可保留）
    assert node in config.dirty_nodes()


def test_second_flush_does_not_repeat_the_delete(
        config: Configue, flush_edits,
) -> None:
    """上一条的延续：标记清掉后，重试的 flush 写的是**值**而不是 ``DELETE``。"""
    node = (SECTION, "app.quit")
    config.reset(*node)

    config.resolver.mark_clean = lambda nodes: None  # 不抛，但也不清 dirty
    try:
        config.flush(file=SHORTCUT_USER_FILE)
    finally:
        del config.resolver.mark_clean  # type: ignore[attr-defined]

    user_path = str(config.base_dir / "shortcuts.ini")
    assert flush_edits[user_path][SECTION]["app.quit"] is DELETE
    assert node in config.dirty_nodes()          # 刻意没清
    assert config.resolver.is_reset_pending(node) is False

    flush_edits.clear()
    config.flush(file=SHORTCUT_USER_FILE)

    written = flush_edits[user_path][SECTION]["app.quit"]
    assert written is not DELETE, "标记已清，不该再发 DELETE"
    assert "Ctrl+Q" in written                   # 写的是回退后的默认值


def test_reset_pending_is_cleared_only_after_a_successful_write(
        config: Configue, monkeypatch,
) -> None:
    """写盘失败的文件：标记**必须**留着，供下次重试（对照组）。"""
    import albuswall.configue.configue as configue_mod

    node = (SECTION, "app.quit")
    config.reset(*node)

    real_get_backend = configue_mod.get_backend

    def failing_backend(fmt):
        backend = real_get_backend(fmt)

        class _Failing:
            def write(self, path, edits):
                raise OSError(f"disk full: {path}")

            def __getattr__(self, item):
                return getattr(backend, item)

        return _Failing()

    monkeypatch.setattr(configue_mod, "get_backend", failing_backend)

    config.flush(file=SHORTCUT_USER_FILE)        # 非 strict：吞掉写盘异常

    assert config.last_flush_error is not None
    assert config.resolver.is_reset_pending(node) is True
    assert node in config.dirty_nodes()


def test_documented_defaults_table_is_untouched(
        config: Configue, tmp_path: Path,
) -> None:
    """顺带锁住边界：整个 reset/flush 过程不写随包默认表。"""
    bundled = _FILES[SHORTCUT_DEFAULTS_FILE].resolved_path
    assert bundled is not None
    before = bundled.read_bytes()

    config.reset(SECTION, "app.quit")
    config.flush(file=SHORTCUT_USER_FILE)

    assert bundled.read_bytes() == before
