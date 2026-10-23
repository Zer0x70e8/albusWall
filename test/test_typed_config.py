#
"""UI 配置的 typed dict 层：形状、默认值来源、覆盖顺序。

覆盖：

  1. 每个 UI typed dict 都能加载（键与静态配置声明一一对应）
  2. 缺失项回落到 **静态配置声明** 的默认值（不是硬编码在 UI 里的）
  3. INI 里的值覆盖声明默认值
  4. 偏好层声明（``PreferenceDeclaration``）优先于通用静态字段
  5. typed dict 里的键找不到声明 → ``LookupError``（不静默兜底）
  6. ``Path`` 注解会把字符串值收敛成 ``Path``
  7. UI 源码不再直接读 ``config.static.*``（统一走 typed dict）

不依赖 pytest：直接 ``python test/test_typed_config.py`` 运行，
装了 pytest 的话 ``pytest test/test_typed_config.py`` 也能跑。
"""

from __future__ import annotations

import sys
import tempfile
import traceback
from pathlib import Path
from textwrap import dedent
from typing import Optional, TypedDict

try:  # 允许直接以文件路径运行
    import albuswall  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# UI 配置声明必须先 import：ConfigField 靠 __set_name__ 注册，
# 必须在 setup_static_config → ConfigDeclaration.load 之前跑完。
from albuswall.ui.builtin.config import (  # noqa: E402
    album_config,
    preference_source_config,
    thumbnail_grid_config,
    window_config,
)
from albuswall.configue.declaration import ConfigDeclaration  # noqa: E402
from albuswall.ui.builtin.config.static import (  # noqa: E402
    WindowPresenterConfs,
)
from albuswall.ui.schema import (  # noqa: E402
    AlbumConfig,
    PreferenceSourceConfig,
    ThumbnailGridConfig,
    WindowConfig,
)

_UI_ROOT = Path(__file__).resolve().parents[1] / "src" / "albuswall" / "ui"


# ══════════════════════════════════════════════════════════════
# 工具
# ══════════════════════════════════════════════════════════════
def _tmpdir() -> Path:
    return Path(tempfile.mkdtemp(prefix="albuswall-typed-cfg-"))


def _reset_config(ini_text: str):
    """重建一份全局 Configue 的 Resolver，并用给定 INI 跑静态加载。"""
    from albuswall.configue import setup_static_config
    from albuswall.configue.configue import Configue, StaticConfig
    from albuswall.configue.resolver import Resolver
    from albuswall.core import Container

    config = Configue()
    config.resolver = Resolver()
    config.static = StaticConfig(config.resolver)
    config._last_flush_error = None
    config._default_write_file = None
    config.configure_persistence(autosave="atexit")

    tmp = _tmpdir()
    main = tmp / "main.ini"
    main.write_text(dedent(ini_text).lstrip("\n"), encoding="utf-8")
    setup_static_config(Container(), main)
    return config


def _minimal_ini(tmp: Path, extra: str = "") -> str:
    """只给路径，不给任何 UI 字段：用来验证「默认值全部来自声明」。"""
    return f"""
        [path]
        theme = {tmp / "theme"}
        cache = {tmp / "cache"}
        temp = {tmp / "temp"}
        data = {tmp / "data"}
    """ + extra


# ══════════════════════════════════════════════════════════════
# 1. 形状
# ══════════════════════════════════════════════════════════════
def test_every_typed_dict_loads() -> None:
    _reset_config(_minimal_ini(_tmpdir()))

    for td, loader in (
            (WindowConfig, window_config),
            (AlbumConfig, album_config),
            (ThumbnailGridConfig, thumbnail_grid_config),
            (PreferenceSourceConfig, preference_source_config),
    ):
        values = loader()
        assert set(values) == set(td.__annotations__), (
            f"{td.__name__} 键不一致: {set(values) ^ set(td.__annotations__)}"
        )


# ══════════════════════════════════════════════════════════════
# 2. 默认值来自静态配置声明
# ══════════════════════════════════════════════════════════════
def test_defaults_come_from_static_declarations() -> None:
    _reset_config(_minimal_ini(_tmpdir()))

    window = window_config()
    assert window["auto_complete"] is WindowPresenterConfs.auto_complete.default
    assert (
        window["window_state_file_name"]
        == WindowPresenterConfs.window_state_file_name.default
    )
    assert window["qss_file_name"] == WindowPresenterConfs.qss_file_name.default

    thumbs = thumbnail_grid_config()
    # 声明里的默认值形如 "${path:icon}/favorite_icon.svg"，
    # typed dict 拿到的是已插值 + Path 化后的绝对路径。
    declared = ConfigDeclaration.metas()["favorite_on_icon"]["default"]
    assert thumbs["favorite_on_icon"] is not None
    assert thumbs["favorite_on_icon"].name == Path(declared).name


def test_ui_config_exposes_language() -> None:
    """界面语言是 UI 引导配置的一部分（``[ui] language``）。"""
    from albuswall.ui.bootstrap import ui_config

    _reset_config(_minimal_ini(_tmpdir()))
    conf = ui_config()
    assert conf["language"] == "zh_CN"
    assert conf["theme"] == "default"


def test_album_defaults_come_from_preference_declarations() -> None:
    _reset_config(_minimal_ini(_tmpdir()))

    album = album_config()
    assert album["search_expanded"] is False
    assert album["album_icon"] == Path("album_button.svg")
    assert album["default_album_uuid"] is None


# ══════════════════════════════════════════════════════════════
# 3. INI 覆盖
# ══════════════════════════════════════════════════════════════
def test_ini_values_override_declaration_defaults() -> None:
    tmp = _tmpdir()
    _reset_config(_minimal_ini(tmp, extra="""
        [files]
        qss = custom.qss
        window_state = state.json

        [ui]
        auto_complete_theme = false
        window_title = My Wall
        style_sheet = /tmp/albuswall/style/custom.qss

        [icons]
        album_icon = /tmp/albuswall/style/icon/my_album.svg

        [album]
        album_text = 相册
    """))

    window = window_config()
    assert window["auto_complete"] is False
    assert Path(window["qss"]) == Path("/tmp/albuswall/style/custom.qss")
    assert window["window_state_file_name"] == "state.json"

    album = album_config()
    assert album["window_title"] == "My Wall"
    assert Path(album["album_icon"]) == Path(
        "/tmp/albuswall/style/icon/my_album.svg")
    assert album["album_text"] == "相册"


def test_preference_source_prefers_explicit_file() -> None:
    tmp = _tmpdir()
    explicit = tmp / "my_preference.ini"
    _reset_config(_minimal_ini(tmp, extra=f"""
        [ui]
        preference_file = {explicit}
    """))

    source = preference_source_config()
    assert Path(source["preference_file"]) == explicit
    assert Path(source["data"]) == tmp / "data"


def test_preference_source_falls_back_to_data_dir() -> None:
    tmp = _tmpdir()
    _reset_config(_minimal_ini(tmp))

    source = preference_source_config()
    assert source["preference_file"] is None
    assert source["preference_file_name"] == "preference"
    expected = Path(source["data"]) / "preference" / "preference.ini"
    assert expected == tmp / "data" / "preference" / "preference.ini"


# ══════════════════════════════════════════════════════════════
# 4. 加载器语义
# ══════════════════════════════════════════════════════════════
class _UnknownKeyConfig(TypedDict):
    not_declared_anywhere: str


class _PathConfig(TypedDict):
    qss: Optional[Path]


def test_unknown_key_raises_lookup_error() -> None:
    _reset_config(_minimal_ini(_tmpdir()))

    from albuswall.configue.typed import load_typed

    try:
        load_typed(_UnknownKeyConfig)
    except LookupError as exc:
        assert "not_declared_anywhere" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("未知键应抛 LookupError")


def test_path_annotation_coerces_string() -> None:
    tmp = _tmpdir()
    _reset_config(_minimal_ini(tmp))

    from albuswall.configue.typed import load_typed

    value = load_typed(_PathConfig)["qss"]
    assert isinstance(value, Path), f"应收敛成 Path，实际 {type(value)}"

    # 与 window_config 的结果保持一致（两条路径都来自同一个声明）
    assert value == window_config()["qss"]


# ══════════════════════════════════════════════════════════════
# 5. UI 层不再直读 static
# ══════════════════════════════════════════════════════════════
def test_ui_never_reads_static_config_directly() -> None:
    """AST 级检查：UI 代码里不允许出现 ``<something>.static`` 属性访问。

    注释 / docstring 里的说明不算（AST 里没有它们），这样文档可以照常
    提 ``config.static.*`` 而不触发这里。
    """
    import ast

    offenders: list[str] = []
    for path in sorted(_UI_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        lines = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr == "static"
        ]
        if lines:
            offenders.append(f"{path.relative_to(_UI_ROOT)}:{lines}")
    assert not offenders, f"这些 UI 文件仍在直读静态配置: {offenders}"


# ══════════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════════
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
