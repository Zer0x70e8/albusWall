#
"""``albuswall.ui.window.settings``：界面层配置使用接口的回归。

三组断言，对应接口的三件事：

* **读** —— ``window_state()`` 在没有配置单例 / 没有 ``[window]`` 段 / 值非法
  时都必须返回声明默认值，绝不能抛；
* **写** —— ``store_window_state`` / ``update_settings`` 走配置器的两遍算法
  （校验后统一落盘），写进去的值能读回来、能落到文件里，未知键要报错；
* **快捷键视图** —— ``shortcut_config_info`` 把「默认表 / 用户覆盖 / 生效
  键位 / 冲突」摊平，注册表缺席时退化成纯配置文件视图。

``WindowState`` 的声明由 ``ui.window.settings`` 在 import 期完成，但
``tests/test_configue/conftest.py`` 每个用例都会 clear 全局登记表（进程级
状态），所以这里显式重放一次 ``configclass(...)`` —— 与 ``setup_config``
里 ``_declare_app_sections`` 做的是同一件事。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from albuswall.configue import Configue, configclass
from albuswall.configue.bootstrap import setup_config
from albuswall.configue.registry import _CLASSES, _FIELD_OWNER, _FILES, _PATHS
from albuswall.core import Container
from albuswall.ui.keymap import ShortcutRegistry
from albuswall.ui.keymap.config import ShortcutConfig
from albuswall.ui.keymap.model import ShortcutSlot
from albuswall.ui.window.settings import (
    DEFAULT_TILE_COLUMNS,
    OVERLAY_PAGES,
    StateSnapshot,
    WindowState,
    format_geometry,
    parse_geometry,
    shortcut_config_info,
    store_window_state,
    update_settings,
    window_state,
)


# ==================== 隔离 / 辅助 ====================

def _reset_registry() -> None:
    """清掉进程级登记表（与 ``tests/test_configue/conftest.py`` 同一套动作）。"""
    _FILES.clear()
    _CLASSES.clear()
    _PATHS.clear()
    _FIELD_OWNER.clear()


@pytest.fixture
def config(tmp_path: Path):
    """一个 portable 的 ``Configue``（配置 + 数据全在 tmp_path 下）。"""
    previous = Configue._instance
    _reset_registry()
    configclass("window", "main")(WindowState)
    config = setup_config(
        Container(), base_dir=tmp_path, portable=True, ignore_missing=True,
    )
    try:
        yield config
    finally:
        config.close()
        Configue._instance = previous
        _reset_registry()


def _slot(command: str, *, keys: tuple[str, ...] = (), help_: str = "") -> ShortcutSlot:
    return ShortcutSlot(
        id=command, handler=lambda: None, help=help_, default_keys=keys,
    )


def _shortcut_config(tmp_path: Path, defaults: str, user: str = "") -> ShortcutConfig:
    defaults_path = tmp_path / "defaults.ini"
    defaults_path.write_text(f"[shortcuts]\n{defaults}\n", encoding="utf-8")
    user_path = tmp_path / "user.ini"
    if user:
        user_path.write_text(f"[shortcuts]\n{user}\n", encoding="utf-8")
    return ShortcutConfig(defaults_path=defaults_path, user_path=user_path)


# ==================== 几何文本 ====================

@pytest.mark.parametrize(("value", "expected"), [
    ("10,20,300,200", (10, 20, 300, 200)),
    (" 10, 20 , 300 ,200 ", (10, 20, 300, 200)),
    ("10;20;300;200", (10, 20, 300, 200)),
    ((1, 2, 3, 4), (1, 2, 3, 4)),
    ([1, 2, 3, 4], (1, 2, 3, 4)),
    ("", None),
    ("   ", None),
    ("1,2,3", None),
    ("1,2,0,4", None),          # 宽度 0 = 没存过
    ("a,b,c,d", None),
    (None, None),
    (object(), None),
])
def test_parse_geometry(value, expected) -> None:
    assert parse_geometry(value) == expected


def test_parse_geometry_accepts_qrect_like_objects() -> None:
    class _Rect:
        def x(self):
            return 3

        def y(self):
            return 4

        def width(self):
            return 500

        def height(self):
            return 400

    assert parse_geometry(_Rect()) == (3, 4, 500, 400)


def test_format_geometry_round_trip() -> None:
    assert format_geometry((1, 2, 3, 4)) == "1,2,3,4"
    assert parse_geometry(format_geometry((1, 2, 3, 4))) == (1, 2, 3, 4)
    assert format_geometry("") == ""
    assert format_geometry("nonsense") == ""


# ==================== 读（无配置单例） ====================

def test_window_state_falls_back_to_declared_defaults() -> None:
    """没有 ``Configue``（单测 / 无配置环境）时读到的是一份全默认快照。"""
    snapshot = window_state()

    assert isinstance(snapshot, StateSnapshot)
    assert snapshot == StateSnapshot()
    assert snapshot.rect() is None
    assert snapshot.empty is True
    assert snapshot.tile_columns == DEFAULT_TILE_COLUMNS


def test_defaults_table_matches_the_declaration() -> None:
    """``_DEFAULTS`` 与 ``WindowState`` 的字段必须逐条对齐（防手工漂移）。"""
    from albuswall.ui.window import settings as module

    declared = {
        name for name in vars(WindowState)
        if not name.startswith("_") and not callable(
            getattr(WindowState, name, None))
    }
    assert set(module._DEFAULTS) == declared
    assert set(module._DEFAULTS) == set(StateSnapshot.__dataclass_fields__)


# ==================== 写（真实 Configue） ====================

def test_store_window_state_round_trip(config, tmp_path: Path) -> None:
    report = store_window_state(
        geometry=(40, 60, 900, 700),
        maximized=True,
        overlay="album",
        album_uuid="00000000-0000-0000-0000-000000000001",
        tile_columns=5,
    )

    assert report["ok"] is True
    assert report["pending"] == []
    assert report["applied"], "至少写入了一条"
    assert "window.geometry" in report["applied"]

    snapshot = window_state()
    assert snapshot.geometry == "40,60,900,700"
    assert snapshot.rect() == (40, 60, 900, 700)
    assert snapshot.maximized is True
    assert snapshot.overlay == "album"
    assert snapshot.tile_columns == 5
    # 声明默认值的字段没被写入 → 从文件读回来也还是默认值
    assert snapshot.fullscreen is False

    text = (tmp_path / "config.ini").read_text(encoding="utf-8")
    assert "[window]" in text
    assert "40,60,900,700" in text


def test_update_settings_dry_run_does_not_write(config, tmp_path: Path) -> None:
    report = update_settings({"window.maximized": True}, dry_run=True)

    assert report["dry_run"] is True
    assert report["applied"] == []
    assert report["written"] == []
    assert window_state().maximized is False
    assert not (tmp_path / "config.ini").exists()


def test_update_settings_rejects_undeclared_keys(config) -> None:
    with pytest.raises(ValueError, match="unknown setting key"):
        update_settings({"window.nope": 1})


def test_update_settings_accepts_tuple_and_bracket_keys(config) -> None:
    update_settings({("window", "maximized"): True})
    assert window_state().maximized is True

    update_settings({"[window]fullscreen": True})
    assert window_state().fullscreen is True


def test_store_window_state_rejects_unknown_field() -> None:
    with pytest.raises(ValueError, match="unknown window state field"):
        store_window_state(nope=1)


def test_store_window_state_ignores_none(config) -> None:
    """``None`` 表示「这次不改」，不该把默认值写回去。"""
    store_window_state(maximized=True)
    report = store_window_state(maximized=None)

    assert report["applied"] == []
    assert window_state().maximized is True


@pytest.mark.parametrize("overlay", ["album", "source"])
def test_store_window_state_accepts_known_overlays(config, overlay) -> None:
    store_window_state(overlay=overlay)
    assert window_state().overlay == overlay


@pytest.mark.parametrize("overlay", ["detail", "nonsense", " "])
def test_store_window_state_drops_unknown_overlays(config, overlay) -> None:
    store_window_state(overlay="album")
    store_window_state(overlay=overlay)
    assert window_state().overlay == ""
    assert "detail" not in OVERLAY_PAGES


# ==================== 快捷键配置信息 ====================

def test_shortcut_config_info_reads_both_tables(tmp_path: Path) -> None:
    config = _shortcut_config(
        tmp_path,
        defaults=(
            'app.quit = ["Ctrl+Q"]\n'
            'window.fullscreen = ["F11"]\n'
            'content.search = []\n'
        ),
        user='app.quit = ["Ctrl+Shift+Q"]\n',
    )
    info = shortcut_config_info(config=config)

    assert info["section"] == "shortcuts"
    assert info["files"]["defaults"] == str(tmp_path / "defaults.ini")
    assert info["files"]["user"] == str(tmp_path / "user.ini")
    assert info["files"]["defaults_exists"] is True
    assert info["files"]["user_exists"] is True
    assert info["errors"] == []

    by_command = {item["command"]: item for item in info["commands"]}
    assert by_command["app.quit"]["source"] == "user"
    assert by_command["app.quit"]["overridden"] is True
    assert by_command["app.quit"]["default_keys"] == ["Ctrl+Q"]
    assert by_command["app.quit"]["effective_keys"] == ["Ctrl+Shift+Q"]
    assert by_command["window.fullscreen"]["source"] == "default"
    # 显式解绑：条目还在，只是没有键位
    assert by_command["content.search"]["bound"] is False
    assert "content.search" in info["unbound"]


def test_shortcut_config_info_without_user_file(tmp_path: Path) -> None:
    config = _shortcut_config(tmp_path, defaults='app.quit = ["Ctrl+Q"]\n')
    info = shortcut_config_info(config=config)

    assert info["files"]["user_exists"] is False
    assert [item["source"] for item in info["commands"]] == ["default"]


def test_shortcut_config_info_uses_registry_for_effective_keys(
        tmp_path: Path,
) -> None:
    registry = ShortcutRegistry()
    registry.register_slot(_slot("app.quit", keys=("Ctrl+Q",), help_="Quit"))
    registry.register_slot(_slot("window.album", keys=(), help_="Album"))
    registry.bind_command("window.album", ["Ctrl+1"])
    registry.bind("Ctrl+1", "window.album", "app.quit")
    # 空配置表：让「生效键位」只可能来自注册表（不读用户目录里的真文件）
    config = _shortcut_config(tmp_path, defaults="")

    info = shortcut_config_info(registry, config=config)
    by_command = {item["command"]: item for item in info["commands"]}

    assert by_command["app.quit"]["help"] == "Quit"
    assert by_command["app.quit"]["source"] == "slot"
    assert by_command["window.album"]["effective_keys"] == ["Ctrl+1"]
    assert info["conflicts"] == [
        {"keys": "Ctrl+1", "commands": ["window.album", "app.quit"]},
    ]


def test_shortcut_config_info_survives_a_missing_default_table(
        tmp_path: Path,
) -> None:
    """默认表不存在时只记一条 error，不抛（设置页要求「能显示别的字段」）。"""
    config = ShortcutConfig(
        defaults_path=tmp_path / "nope.ini", user_path=tmp_path / "user.ini",
    )
    info = shortcut_config_info(config=config)

    assert info["commands"] == []
    assert any("default table" in text for text in info["errors"])
    assert info["files"]["defaults_exists"] is False
