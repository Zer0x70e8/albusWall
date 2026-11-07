#
"""``ui.shortcuts`` / ``ui.state`` 命令的行为回归（命令层，不启动 GUI）。

用**真实**的 ``ShortcutRegistry`` / ``ShortcutConfig`` 装配，验证：

* 表格与 JSON 两种视图都能读出「命令 / 默认键 / 生效键 / 来源」；
* ``--source=<default|user|slot|none>`` 只留下对应来源的条目；
* 注册表没进容器 ≠ 空注册表：接口退回纯配置文件视图，两条命令都不抛；
* ``ui.state`` 在没有任何配置的环境里给出「全默认」快照。

快捷键文件的路径一律用 ``monkeypatch`` 指到 ``tmp_path``：用例不能读真实
用户目录里的 ``shortcuts.ini``（否则结果随开发机而变）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from albuswall.command import CommandRegistry
from albuswall.command.exceptions import BadRequest
from albuswall.core import Container
from albuswall.service.command.dispatcher import Dispatcher
from albuswall.service.command.handlers import ui as ui_cmd
from albuswall.ui.keymap import ShortcutRegistry
from albuswall.ui.keymap.model import ShortcutSlot


def _dispatch(
        container: Container,
        path: str,
        args: list | tuple = (),
        kwargs: dict | None = None,
):
    registry = CommandRegistry()
    ui_cmd.setup(registry, container)
    return Dispatcher(registry).dispatch(path, list(args), dict(kwargs or {}))


def _registry() -> ShortcutRegistry:
    shortcuts = ShortcutRegistry()
    shortcuts.register_slot(ShortcutSlot(
        id="app.quit", handler=lambda: None, help="Quit albusWall",
        default_keys=("Ctrl+Q",),
    ))
    shortcuts.register_slot(ShortcutSlot(
        id="window.album", handler=lambda: None, help="Show / hide the album page",
        default_keys=(),
    ))
    shortcuts.bind_command("window.album", ["Ctrl+1"])
    # 用户覆盖表里的 app.quit：注册表是「最终生效」的那一份
    shortcuts.bind_command("app.quit", ["Ctrl+Shift+Q"])
    return shortcuts


DEFAULTS = """\
app.quit        = ["Ctrl+Q"]
window.album    = []
content.search  = ["Ctrl+F"]
"""

USER = """\
app.quit        = ["Ctrl+Shift+Q"]
"""


@pytest.fixture
def shortcut_files(tmp_path: Path, monkeypatch) -> Path:
    """把快捷键默认表 / 用户表指到 ``tmp_path``（不碰真实用户目录）。"""
    defaults = tmp_path / "shortcuts.ini"
    defaults.write_text(f"[shortcuts]\n{DEFAULTS}", encoding="utf-8")
    user = tmp_path / "user-shortcuts.ini"
    user.write_text(f"[shortcuts]\n{USER}", encoding="utf-8")

    monkeypatch.setattr(
        "albuswall.ui.keymap.config.default_config_path", lambda: defaults,
    )
    monkeypatch.setattr(
        "albuswall.ui.keymap.config.user_config_path",
        lambda file_name="shortcuts.ini": user,
    )
    return tmp_path


def _container(shortcuts: ShortcutRegistry | None = None) -> Container:
    container = Container()
    if shortcuts is not None:
        container.set("shortcuts", shortcuts)
    return container


# ==================== ui.shortcuts：表格 ====================

def test_shortcuts_table_lists_commands_and_files(shortcut_files) -> None:
    text = _dispatch(_container(), "ui.shortcuts")

    assert "default table:" in text
    assert "shortcuts.ini" in text
    assert "app.quit" in text
    assert "Ctrl+Shift+Q" in text            # 用户覆盖后的生效键位
    assert "conflicts: none" in text
    assert "content.search" in text           # 默认表里的键都在


def test_shortcuts_table_marks_unbound_commands(shortcut_files) -> None:
    text = _dispatch(_container(), "ui.shortcuts")

    assert "unbound (no key)" in text
    assert "window.album" in text


def test_shortcuts_verbose_adds_help_column(shortcut_files) -> None:
    text = _dispatch(_container(_registry()), "ui.shortcuts", kwargs={"verbose": True})

    assert "help" in text
    assert "Quit albusWall" in text


# ==================== ui.shortcuts：JSON ====================

def test_shortcuts_json_shape(shortcut_files) -> None:
    payload = _dispatch(_container(_registry()), "ui.shortcuts",
                        kwargs={"format": "json"})

    assert payload["section"] == "shortcuts"
    assert payload["filter"] == "all"
    assert set(payload["files"]) >= {"defaults", "user", "defaults_exists"}
    assert payload["errors"] == []

    by_command = {item["command"]: item for item in payload["commands"]}
    assert by_command["app.quit"]["effective_keys"] == ["Ctrl+Shift+Q"]
    assert by_command["app.quit"]["help"] == "Quit albusWall"
    assert by_command["window.album"]["effective_keys"] == ["Ctrl+1"]
    assert "window.album" not in payload["unbound"]


def test_shortcuts_json_is_jsonable(shortcut_files) -> None:
    """``skipkeys`` 无关：报告里不能有 Path / 自定义对象（IPC 要序列化它）。"""
    import json

    payload = _dispatch(_container(_registry()), "ui.shortcuts",
                        kwargs={"format": "json"})
    assert json.loads(json.dumps(payload)) == payload


# ==================== ui.shortcuts：过滤 / 报错 ====================

def test_shortcuts_source_filter_keeps_only_that_source(shortcut_files) -> None:
    payload = _dispatch(_container(), "ui.shortcuts",
                        kwargs={"format": "json", "source": "user"})

    assert payload["filter"] == "user"
    assert [item["command"] for item in payload["commands"]] == ["app.quit"]


def test_shortcuts_source_filter_accepts_positional_form(shortcut_files) -> None:
    """``--source user``（CLI 拆成 True + 位置参数）与 ``--source=user`` 等价。"""
    payload = _dispatch(_container(), "ui.shortcuts",
                        args=["user"],
                        kwargs={"source": True, "format": "json"})

    assert payload["filter"] == "user"
    assert [item["command"] for item in payload["commands"]] == ["app.quit"]


def test_shortcuts_rejects_unknown_source(shortcut_files) -> None:
    with pytest.raises(BadRequest, match="unknown --source"):
        _dispatch(_container(), "ui.shortcuts", kwargs={"source": "nope"})


def test_shortcuts_rejects_positional_arguments(shortcut_files) -> None:
    with pytest.raises(BadRequest, match="no positional arguments"):
        _dispatch(_container(), "ui.shortcuts", args=["app.quit"])


def test_shortcuts_without_registry_still_reads_the_files(shortcut_files) -> None:
    """容器里没有 ``shortcuts`` 键（没起过界面）：照样能从配置文件读出来。"""
    text = _dispatch(_container(), "ui.shortcuts")

    assert "app.quit" in text
    payload = _dispatch(_container(), "ui.shortcuts", kwargs={"format": "json"})
    assert payload["errors"] == []


# ==================== ui.state ====================

def test_state_reports_declared_defaults() -> None:
    """没有任何配置单例时：全默认 + 一句明确提示（不抛）。"""
    text = _dispatch(_container(), "ui.state")

    assert "geometry" in text
    assert "tile_columns" in text
    assert "nothing saved yet" in text


def test_state_json_shape() -> None:
    payload = _dispatch(_container(), "ui.state", kwargs={"format": "json"})

    assert payload == {
        "geometry": "",
        "rect": None,
        "maximized": False,
        "fullscreen": False,
        "album_uuid": "",
        "overlay": "",
        "tile_columns": 0,
    }


def test_state_rejects_positional_arguments() -> None:
    with pytest.raises(BadRequest, match="no positional arguments"):
        _dispatch(_container(), "ui.state", args=["geometry"])


# ==================== 命名空间 ====================

def test_ui_namespace_entry_is_registered() -> None:
    registry = CommandRegistry()
    ui_cmd.setup(registry, Container())
    names = set(registry.names())

    assert "ui" in names
    assert {"ui.shortcuts", "ui.state"} <= names
