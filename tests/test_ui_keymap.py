#
"""``albuswall.ui.keymap``：注册 / 一对多绑定 / 配置文件持久化。

本批只锁「注册层」的契约（键位不等于真实按键事件，后者属于 binder）：

* 三种键位形态（单按键 / 修饰键组合 / 非修饰键和弦）的解析与规范化；
* 快捷键槽的装饰器注册、重复注册报错、与 ``CommandRegistry`` 对接；
* 绑定表的双向一对多（一键多命令 / 一命令多键）；
* 每次 ``load`` 都从配置文件整表重读，默认表 + 用户表按命令覆盖；
* ``persist`` 只写与默认不同的条目，空列表表示显式解绑。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from albuswall.command import CommandRegistry
from albuswall.core import Container
from albuswall.ui.keymap import (
    DuplicateShortcutSlot,
    InvalidKeySequence,
    KeymapError,
    KeySequence,
    ShortcutBinding,
    ShortcutConfig,
    ShortcutConfigError,
    ShortcutKind,
    ShortcutRegistry,
    ShortcutSlot,
    default_config_path,
    format_sequences,
    get_default_registry,
    parse_sequences,
    registry_shortcut_registry,
    user_config_path,
)


# ==================== 辅助 ====================

def write_ini(path: Path, body: str, *, section: str = "shortcuts") -> Path:
    path.write_text(f"[{section}]\n{body}", encoding="utf-8")
    return path


def make_config(tmp_path: Path, defaults: str, user: str = "") -> ShortcutConfig:
    defaults_path = write_ini(tmp_path / "shortcuts.ini", defaults)
    user_path = (
        write_ini(tmp_path / "user.ini", user) if user else tmp_path / "user.ini"
    )
    return ShortcutConfig(
        defaults_path=defaults_path, user_path=user_path,
    )


# ==================== 键位解析 ====================

def test_parse_sequence_normalizes_and_classifies() -> None:
    assert KeySequence.parse("F5").kind is ShortcutKind.SINGLE
    assert KeySequence.parse("Ctrl+N").kind is ShortcutKind.MODIFIED
    assert KeySequence.parse("G, G").kind is ShortcutKind.CHORD
    assert KeySequence.parse("G, G").is_single_key is False

    # 写法大小写 / 修饰键顺序都会被规范化。
    assert KeySequence.parse("ctrl+shift+p").text == "Ctrl+Shift+P"
    assert KeySequence.parse("Shift+Ctrl+P").text == "Ctrl+Shift+P"
    # Qt 便携名字：Delete -> Del。
    assert KeySequence.parse("Delete").text == "Del"
    # 幂等：已经规范化的对象原样返回。
    seq = KeySequence.parse("Ctrl+Q")
    assert KeySequence.parse(seq) is seq


def test_parse_sequence_modifiers_and_chords() -> None:
    seq = KeySequence.parse("Ctrl+K, Ctrl+C")
    assert seq.chords == ("Ctrl+K", "Ctrl+C")
    assert seq.modifiers == frozenset({"Ctrl"})
    assert seq.kind is ShortcutKind.CHORD

    plain = KeySequence.parse("Ctrl+Shift+F11")
    assert plain.modifiers == frozenset({"Ctrl", "Shift"})
    assert plain.chords == ("Ctrl+Shift+F11",)


@pytest.mark.parametrize("raw", [
    "",
    "   ",
    "Ctrl+Foo",        # Qt 静默返回空串
    "Ctrl+K, Foo",     # Qt 静默截断成空和弦
    "Ctrl+Shift",      # 只有修饰键，不是快捷键
    "Backspace+0",     # 修饰键以外的「同时按下」，Qt 同样静默返回空串
    "G, G, G, G, G",   # 超过 Qt 的 4 步上限
])
def test_parse_sequence_rejects_invalid(raw: str) -> None:
    with pytest.raises(InvalidKeySequence):
        KeySequence.parse(raw)
    # 与 ValueError 系调用方兼容。
    assert issubclass(InvalidKeySequence, ValueError)
    assert issubclass(InvalidKeySequence, KeymapError)


def test_non_modifier_chord_with_named_keys() -> None:
    """非修饰键和弦（``Backspace, 0``）：先按 Backspace，再按 0。"""
    seq = KeySequence.parse("Backspace, 0")
    assert seq.chords == ("Backspace", "0")
    assert seq.kind is ShortcutKind.CHORD
    assert seq.modifiers == frozenset()

    registry = ShortcutRegistry()
    registry.bind_command("navigation.top", ["Backspace, 0", "0, 0"])
    assert registry.commands_for("Backspace, 0") == ("navigation.top",)
    assert registry.commands_for("0, 0") == ("navigation.top",)
    # 紧凑写法也会被规范化成带空格的 PortableText。
    assert KeySequence.parse("Backspace,0").text == "Backspace, 0"


def test_parse_sequences_accepts_json_list_and_dedupes() -> None:
    seqs = parse_sequences('["F5", "Ctrl+N", "F5"]')
    assert [str(seq) for seq in seqs] == ["F5", "Ctrl+N"]

    # 逗号是和弦分隔符，不是列表分隔符。
    assert [str(seq) for seq in parse_sequences("G, G")] == ["G, G"]
    # 逗号键本身也必须能表达（JSON 字符串里才不会被拆开）。
    assert [str(seq) for seq in parse_sequences('["Ctrl+,"]')] == ["Ctrl+,"]

    assert parse_sequences("[]") == ()
    assert parse_sequences("") == ()
    assert parse_sequences([]) == ()


def test_format_sequences_is_json_array() -> None:
    assert format_sequences(["ctrl+n", "F5"]) == '["Ctrl+N", "F5"]'


# ==================== 槽注册 ====================

def test_slot_decorator_registers_handler() -> None:
    registry = ShortcutRegistry()

    @registry.slot("view.fullscreen", help_="全屏", keys=["F11"])
    def toggle_fullscreen() -> str:
        return "toggled"

    slot = registry.get_slot("view.fullscreen")
    assert isinstance(slot, ShortcutSlot)
    assert slot.help == "全屏"
    assert [str(key) for key in slot.default_keys] == ["F11"]
    assert slot.handler is toggle_fullscreen
    # 装饰器不包装函数。
    assert toggle_fullscreen() == "toggled"
    assert registry.command_handler("view.fullscreen") is toggle_fullscreen
    assert registry.slot_ids() == ("view.fullscreen",)


def test_slot_duplicate_requires_replace() -> None:
    registry = ShortcutRegistry()

    def first() -> None:
        pass

    def second() -> None:
        pass

    registry.register_slot(ShortcutSlot("album.new", first))
    with pytest.raises(DuplicateShortcutSlot):
        registry.register_slot(ShortcutSlot("album.new", second))

    registry.register_slot(ShortcutSlot("album.new", second), replace=True)
    assert registry.command_handler("album.new") is second


def test_slot_validates_arguments() -> None:
    with pytest.raises(ValueError):
        ShortcutSlot("", lambda: None)
    with pytest.raises(TypeError):
        ShortcutSlot("x", "not callable")  # type: ignore[arg-type]


def test_register_commands_into_command_registry() -> None:
    commands = CommandRegistry()
    registry = ShortcutRegistry(command_registry=commands)

    @registry.slot("album.new", help_="新建相册")
    def new_album() -> str:
        return "new album"

    node = commands.get("album.new")
    assert node is not None
    assert node.func is new_album
    assert node.help == "新建相册"
    assert commands.executable("album.new")

    # 幂等：再次注册同一个 handler 不报错。
    registry.register_commands()
    assert commands.get("album.new") is not None

    # 换成另一个 handler 会撞名。
    other = CommandRegistry()
    registry.set_command_registry(other)
    assert other.get("album.new") is not None


def test_registry_shortcut_registry_into_container() -> None:
    container = Container()
    registry_shortcut_registry(container)
    assert container.get("shortcuts") is get_default_registry()


# ==================== 绑定表 ====================

def test_one_sequence_to_many_commands() -> None:
    registry = ShortcutRegistry()
    registry.bind("Ctrl+W", "window.close", "viewer.close")
    registry.bind("Ctrl+W", "window.close")  # 追加去重

    assert registry.commands_for("Ctrl+W") == ("window.close", "viewer.close")
    assert registry.commands_for("ctrl+w") == ("window.close", "viewer.close")
    assert registry.keys_for("viewer.close") == (KeySequence.parse("Ctrl+W"),)
    assert registry.conflicts() == {
        KeySequence.parse("Ctrl+W"): ("window.close", "viewer.close"),
    }


def test_one_command_to_many_sequences() -> None:
    registry = ShortcutRegistry()
    registry.bind_command("view.fullscreen", ["F11", "Ctrl+Shift+F"])

    assert [str(key) for key in registry.keys_for("view.fullscreen")] == [
        "F11", "Ctrl+Shift+F",
    ]
    assert registry.commands_for("F11") == ("view.fullscreen",)
    assert registry.commands_for("Ctrl+Shift+F") == ("view.fullscreen",)
    assert registry.conflicts() == {}


def test_bind_replace_and_unbind() -> None:
    registry = ShortcutRegistry()
    registry.bind("Ctrl+W", "window.close")
    registry.bind("Ctrl+W", "viewer.close", replace=True)
    assert registry.commands_for("Ctrl+W") == ("viewer.close",)

    registry.bind_command("album.new", ["Ctrl+N", "N"])
    registry.unbind("Ctrl+N", "album.new")
    assert registry.commands_for("Ctrl+N") == ()
    assert [str(key) for key in registry.keys_for("album.new")] == ["N"]

    registry.unbind_command("album.new")
    assert registry.keys_for("album.new") == ()
    assert registry.commands_for("Ctrl+W") == ("viewer.close",)


def test_bind_rejects_empty_input() -> None:
    registry = ShortcutRegistry()
    with pytest.raises(ValueError):
        registry.bind("Ctrl+W")
    with pytest.raises(ValueError):
        registry.bind("", "window.close")
    with pytest.raises(ValueError):
        registry.bind("Ctrl+W", "  ")


# ==================== 配置文件 ====================

def test_load_reads_defaults_then_user_overrides(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        defaults=(
            'view.fullscreen = ["F11", "Ctrl+Shift+F"]\n'
            'album.new = ["Ctrl+N"]\n'
            'navigation.grid = ["G, G"]\n'
            'asset.delete = ["Delete"]\n'
        ),
        user=(
            'view.fullscreen = ["F11"]\n'   # 覆盖：少一个键
            'album.new = []\n'              # 显式解绑
        ),
    )

    registry = ShortcutRegistry()
    registry.bind("Ctrl+Q", "app.quit")  # 旧绑定会被 load 清掉
    registry.load(config)

    assert [str(key) for key in registry.keys_for("view.fullscreen")] == ["F11"]
    assert registry.keys_for("album.new") == ()
    assert registry.commands_for("G, G") == ("navigation.grid",)
    assert registry.commands_for("Del") == ("asset.delete",)
    assert registry.commands_for("Ctrl+Q") == ()


def test_load_without_user_file_uses_defaults(tmp_path: Path) -> None:
    config = make_config(tmp_path, defaults='album.new = ["Ctrl+N"]\n')
    assert not config.user_path.exists()  # type: ignore[union-attr]

    registry = ShortcutRegistry().load(config)
    assert registry.commands_for("Ctrl+N") == ("album.new",)


def test_slot_default_keys_fill_missing_config_entries(tmp_path: Path) -> None:
    config = make_config(tmp_path, defaults='album.new = ["Ctrl+N"]\n')
    registry = ShortcutRegistry()

    @registry.slot("plugin.hello", keys=["Ctrl+Shift+H"])
    def hello() -> None:
        pass

    registry.load(config)
    # 配置里没有 plugin.hello，落到槽声明的兜底键位。
    assert registry.commands_for("Ctrl+Shift+H") == ("plugin.hello",)
    # 配置里有的命令仍以配置文件为准。
    assert registry.commands_for("Ctrl+N") == ("album.new",)
    # 没有任何键位的槽会被报告出来。
    assert registry.unbound_slots() == ()


def test_unbound_slots_reports_missing_binding(tmp_path: Path) -> None:
    config = make_config(tmp_path, defaults='album.new = ["Ctrl+N"]\n')
    registry = ShortcutRegistry()

    @registry.slot("plugin.hello")
    def hello() -> None:
        pass

    registry.load(config)
    assert registry.unbound_slots() == ("plugin.hello",)


def test_load_is_fresh_every_call(tmp_path: Path) -> None:
    config = make_config(tmp_path, defaults='album.new = ["Ctrl+N"]\n')
    registry = ShortcutRegistry().load(config)
    assert registry.commands_for("Ctrl+N") == ("album.new",)

    # 手改默认表（模拟「每次都从配置文件加载」）。
    config.defaults_path.write_text(  # type: ignore[union-attr]
        '[shortcuts]\nalbum.new = ["Ctrl+Shift+N"]\n', encoding="utf-8",
    )
    registry.load(config)
    assert registry.commands_for("Ctrl+N") == ()
    assert registry.commands_for("Ctrl+Shift+N") == ("album.new",)


def test_bad_config_value_raises_or_is_skipped(tmp_path: Path) -> None:
    config = make_config(tmp_path, defaults='album.new = ["Ctrl+Foo"]\n')
    with pytest.raises(ShortcutConfigError):
        config.load()

    table = config.load(strict=False)
    assert table == {}


def test_missing_section_and_missing_default_file(tmp_path: Path) -> None:
    path = tmp_path / "wrong.ini"
    path.write_text("[ui]\ntheme = default\n", encoding="utf-8")
    config = ShortcutConfig(defaults_path=path, user_path=None)
    with pytest.raises(ShortcutConfigError):
        config.load()

    missing = ShortcutConfig(
        defaults_path=tmp_path / "nope.ini", user_path=None,
    )
    with pytest.raises(ShortcutConfigError):
        missing.load()


# ==================== 持久化 ====================

def test_persist_writes_only_overrides_and_roundtrips(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        defaults=(
            'view.fullscreen = ["F11", "Ctrl+Shift+F"]\n'
            'album.new = ["Ctrl+N"]\n'
        ),
    )
    registry = ShortcutRegistry()
    registry.load(config)

    # 什么都不改：用户表保持空（没有 override）。
    registry.persist(config)
    user_text = config.user_path.read_text(encoding="utf-8")  # type: ignore[union-attr]
    assert "view.fullscreen" not in user_text
    assert "album.new" not in user_text

    # 改一个命令的键位 → 只写这一条（显式三元组形式）。
    registry.bind_command("view.fullscreen", ["F11"])
    registry.persist(config)
    user_text = config.user_path.read_text(encoding="utf-8")  # type: ignore[union-attr]
    assert 'view.fullscreen = ["F11", [], {}]' in user_text
    assert "album.new" not in user_text

    # 重新加载：用户表覆盖默认表。
    reloaded = ShortcutRegistry().load(config)
    assert [str(key) for key in reloaded.keys_for("view.fullscreen")] == ["F11"]
    assert reloaded.commands_for("Ctrl+N") == ("album.new",)

    # 改回默认 → 条目从用户表里消失。
    reloaded.bind_command("view.fullscreen", ["F11", "Ctrl+Shift+F"])
    reloaded.persist(config)
    user_text = config.user_path.read_text(encoding="utf-8")  # type: ignore[union-attr]
    assert "view.fullscreen" not in user_text


def test_persist_can_disable_a_default(tmp_path: Path) -> None:
    config = make_config(tmp_path, defaults='album.new = ["Ctrl+N"]\n')
    registry = ShortcutRegistry().load(config)
    registry.bind_command("album.new", [])
    registry.persist(config)

    user_text = config.user_path.read_text(encoding="utf-8")  # type: ignore[union-attr]
    assert "album.new = [[], [], {}]" in user_text
    reloaded = ShortcutRegistry().load(config)
    assert reloaded.keys_for("album.new") == ()


def test_persist_without_user_path_is_none() -> None:
    config = ShortcutConfig(defaults_path=None, user_path=None)
    registry = ShortcutRegistry().load(config)
    assert registry.persist(config) is None


def test_reset_removes_user_file(tmp_path: Path) -> None:
    config = make_config(tmp_path, defaults='album.new = ["Ctrl+N"]\n')
    config.save({"album.new": ["N"]})
    assert config.user_path.is_file()  # type: ignore[union-attr]
    config.reset()
    assert not config.user_path.exists()  # type: ignore[union-attr]


def test_save_uses_atomic_write_and_json_values(tmp_path: Path) -> None:
    config = ShortcutConfig(defaults_path=None, user_path=tmp_path / "s.ini")
    path = config.save({"navigation.grid": ["G, G"], "app.quit": ["Ctrl+Q"]})
    text = path.read_text(encoding="utf-8")
    assert "[shortcuts]" in text
    assert 'navigation.grid = ["G, G", [], {}]' in text
    assert 'app.quit = ["Ctrl+Q", [], {}]' in text
    assert not list(tmp_path.glob("*.tmp"))
    assert config.load() == {
        "navigation.grid": ShortcutBinding(keys=(KeySequence.parse("G, G"),)),
        "app.quit": ShortcutBinding(keys=(KeySequence.parse("Ctrl+Q"),)),
    }


# ==================== 随包资源 ====================

def test_shipped_default_table_is_loadable() -> None:
    path = default_config_path()
    assert path.is_file()
    table = ShortcutConfig(user_path=None).load()
    assert "window.fullscreen" in table
    assert [str(key) for key in table["window.fullscreen"].keys] == [
        "F11", "Ctrl+Shift+F",
    ]
    assert [str(key) for key in table["content.search"].keys] == ["Ctrl+F"]


def test_user_config_path_is_under_user_config_dir() -> None:
    path = user_config_path()
    assert path.name == "shortcuts.ini"
    assert "albuswall" in str(path)


# ==================== 槽参数（位置 / 关键字） ====================

def test_binding_record_parses_args_and_kwargs(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        defaults=(
            'album.open = ["Alt+1", [3], {"mode": "fit"}]\n'
            'view.reset = [["F9", "Ctrl+0"], [], {}]\n'
        ),
    )
    registry = ShortcutRegistry()
    calls: list[tuple[object, ...]] = []

    @registry.slot("album.open")
    def open_album(index: int, *, mode: str = "grid") -> int:
        calls.append((index, mode))
        return index

    @registry.slot("view.reset")
    def reset(*, zoom: float = 1.0) -> str:
        calls.append(("reset", zoom))
        return "reset"

    registry.load(config)

    binding = registry.binding_for("album.open")
    assert [str(key) for key in binding.keys] == ["Alt+1"]
    assert binding.args == (3,)
    assert binding.kwargs == {"mode": "fit"}

    # 按键 → 用配置里的固定实参调用槽。
    assert registry.trigger("Alt+1") == (3,)
    assert calls == [(3, "fit")]

    # 一个命令多个键，各键都带着同一份实参。
    assert registry.trigger("F9") == ("reset",)
    assert registry.trigger("Ctrl+0") == ("reset",)
    assert calls[-2:] == [("reset", 1.0), ("reset", 1.0)]


def test_one_sequence_many_commands_each_with_own_args() -> None:
    registry = ShortcutRegistry()
    seen: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    @registry.slot("window.close")
    def close_window(*args: object, **kwargs: object) -> str:
        seen.append(("window", args, kwargs))
        return "window"

    @registry.slot("viewer.close")
    def close_viewer(*args: object, **kwargs: object) -> str:
        seen.append(("viewer", args, kwargs))
        return "viewer"

    registry.bind("Ctrl+W", "window.close", "viewer.close")
    registry.set_call_args("window.close", ())
    registry.set_call_args("viewer.close", (1,), {"force": True})

    assert registry.trigger("Ctrl+W") == ("window", "viewer")
    assert seen == [
        ("window", (), {}),
        ("viewer", (1,), {"force": True}),
    ]


def test_invoke_merges_fixed_and_explicit_args() -> None:
    registry = ShortcutRegistry()

    @registry.slot("album.open")
    def open_album(*args: object, **kwargs: object) -> object:
        return args, kwargs

    registry.bind_command(
        "album.open", ["Alt+1"], args=(1,), kwargs={"mode": "fit"},
    )
    assert registry.invoke("album.open") == ((1,), {"mode": "fit"})
    assert registry.invoke("album.open", 2, mode="zoom", tag="x") == (
        (1, 2), {"mode": "zoom", "tag": "x"},
    )

    with pytest.raises(KeyError):
        registry.invoke("no.such.slot")


def test_set_call_args_replaces_both_parts() -> None:
    registry = ShortcutRegistry()
    registry.set_call_args("album.open", (7,), {"mode": "grid"})
    assert registry.args_for("album.open") == (7,)
    assert registry.kwargs_for("album.open") == {"mode": "grid"}

    registry.set_call_args("album.open", (8,))
    assert registry.args_for("album.open") == (8,)
    assert registry.kwargs_for("album.open") == {}

    with pytest.raises(ValueError):
        registry.set_call_args("album.open", 5)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        registry.set_call_args("album.open", (), [("a", 1)])  # type: ignore[arg-type]


def test_slot_decorator_default_args(tmp_path: Path) -> None:
    config = make_config(tmp_path, defaults='other.cmd = ["F1"]\n')
    registry = ShortcutRegistry()

    @registry.slot(
        "album.open", keys="Alt+1", args=(5,), kwargs={"mode": "fit"},
    )
    def open_album(*args: object, **kwargs: object) -> object:
        return args, kwargs

    registry.load(config)
    # 配置里没这个命令 → 用装饰器声明的兜底键位与兜底实参。
    assert registry.commands_for("Alt+1") == ("album.open",)
    assert registry.trigger("Alt+1") == (((5,), {"mode": "fit"}),)


def test_config_overrides_slot_default_args(tmp_path: Path) -> None:
    config = make_config(
        tmp_path, defaults='album.open = ["Alt+2", [9], {}]\n',
    )
    registry = ShortcutRegistry()

    @registry.slot(
        "album.open", keys="Alt+1", args=(5,), kwargs={"mode": "fit"},
    )
    def open_album(*args: object, **kwargs: object) -> object:
        return args, kwargs

    registry.load(config)
    # 键位与实参都以配置文件为准。
    assert registry.commands_for("Alt+1") == ()
    assert registry.trigger("Alt+2") == (((9,), {}),)


def test_persist_writes_args_and_roundtrips(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        defaults='album.open = ["Alt+1", [1], {"mode": "fit"}]\n',
    )
    registry = ShortcutRegistry()

    @registry.slot("album.open")
    def open_album(*args: object, **kwargs: object) -> object:
        return args, kwargs

    registry.load(config)
    registry.set_call_args("album.open", (2,), {"mode": "grid"})
    registry.persist(config)

    user_text = config.user_path.read_text(encoding="utf-8")  # type: ignore[union-attr]
    assert 'album.open = ["Alt+1", [2], {"mode": "grid"}]' in user_text

    reloaded = ShortcutRegistry()

    @reloaded.slot("album.open")
    def open_album_again(*args: object, **kwargs: object) -> object:
        return args, kwargs

    reloaded.load(config)
    assert reloaded.binding_for("album.open").args == (2,)
    assert reloaded.binding_for("album.open").kwargs == {"mode": "grid"}
    assert reloaded.trigger("Alt+1") == (((2,), {"mode": "grid"}),)


def test_legacy_flat_list_and_single_key_still_accepted(
        tmp_path: Path,
) -> None:
    config = make_config(
        tmp_path,
        defaults=(
            'view.fullscreen = ["F11", "Ctrl+Shift+F"]\n'
            'navigation.grid = "G, G"\n'
            'album.new = ["Ctrl+N", [], {}]\n'
        ),
    )
    table = config.load()
    assert [str(key) for key in table["view.fullscreen"].keys] == [
        "F11", "Ctrl+Shift+F",
    ]
    assert table["view.fullscreen"].args == ()
    assert table["view.fullscreen"].kwargs == {}
    assert [str(key) for key in table["navigation.grid"].keys] == ["G, G"]
    assert table["album.new"].args == ()
    assert table["album.new"].kwargs == {}


@pytest.mark.parametrize("value", [
    '["Alt+1", "1", {}]',      # 位置参数不是数组
    '["Alt+1", [1], []]',      # 关键字参数不是对象
    '[[1], [1], {}]',          # 快捷键不是字符串 / 字符串数组
    '["Alt+1", [1]]',          # 少了第三项，且不是扁平字符串数组
])
def test_bad_binding_record_raises(tmp_path: Path, value: str) -> None:
    config = make_config(tmp_path, defaults=f"slot_name = {value}\n")
    with pytest.raises(ShortcutConfigError):
        config.load()


def test_persist_without_load_does_not_disable_defaults(
        tmp_path: Path,
) -> None:
    """没 load 就 persist 只写本进程知道的命令，不能把默认表整片解绑。"""
    config = make_config(
        tmp_path,
        defaults=(
            'view.fullscreen = ["F11", "Ctrl+Shift+F"]\n'
            'album.new = ["Ctrl+N"]\n'
        ),
    )
    registry = ShortcutRegistry()

    @registry.slot("plugin.hello", keys="Ctrl+Shift+H")
    def hello() -> str:
        return "hello"

    registry.persist(config)

    user_text = config.user_path.read_text(encoding="utf-8")  # type: ignore[union-attr]
    assert "view.fullscreen" not in user_text
    assert "album.new" not in user_text
    # 没 load 过的槽，其兜底键位还不在「生效」状态，不该被当成 override 落盘。
    assert "plugin.hello" not in user_text
