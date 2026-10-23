#
"""服务层配置的 typed dict 层：形状、默认值来源、owner 绑定。

覆盖：

  1. 三个服务 typed dict 都能加载（键与声明一一对应）
  2. 缺失项回落到**静态配置声明**的默认值（不是硬编码在服务里的）
  3. INI 里的值覆盖声明默认值
  4. 同名字段按 owner 隔离：``[thumbnail] batch_size`` 与
     ``[import] batch_size`` 互不串味（这是 typed dict 分模块的关键）
  5. 绑错 owner → ``LookupError``（不静默兜底）
  6. bootstrap 的 ``load_*`` 用同一份快照折叠出 frozen dataclass
  7. 服务代码不再读旧描述符单例（``_cfg``）

不依赖 pytest：直接 ``python test/test_service_typed_config.py`` 运行，
装了 pytest 的话 ``pytest test/test_service_typed_config.py`` 也能跑。
"""

from __future__ import annotations

import sys
import tempfile
import traceback
from pathlib import Path
from textwrap import dedent

try:  # 允许直接以文件路径运行
    import albuswall  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# 声明必须先 import：ConfigField 靠 __set_name__ 登记，
# 必须在 setup_static_config → ConfigDeclaration.load 之前跑完。
from albuswall.services.import_ import (  # noqa: E402
    ImportConfig,
    import_config,
)
from albuswall.services.schema import (  # noqa: E402
    ImportServiceConfig,
    SourceTrashServiceConfig,
    ThumbnailServiceConfig,
)
from albuswall.services.source_trash.bootstrap import (  # noqa: E402
    _Config as SourceTrashDecl,
    load_session_config,
    load_source_trash_config,
    load_thumbnail_cleaner_config,
    source_trash_config,
)
from albuswall.services.thumbnail.config import (  # noqa: E402
    ThumbnailConfig,
    resolve_specs,
    resolve_thumb_root,
    thumbnail_config,
)

_SERVICES_ROOT = (
    Path(__file__).resolve().parents[1] / "src" / "albuswall" / "services"
)


# ══════════════════════════════════════════════════════════════
# 工具
# ══════════════════════════════════════════════════════════════
def _tmpdir() -> Path:
    return Path(tempfile.mkdtemp(prefix="albuswall-svc-cfg-"))


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
    """只给路径：用来验证「默认值全部来自声明」。"""
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
            (ThumbnailServiceConfig, thumbnail_config),
            (ImportServiceConfig, import_config),
            (SourceTrashServiceConfig, source_trash_config),
    ):
        values = loader()
        assert set(values) == set(td.__annotations__), (
            f"{td.__name__} 键不一致: {set(values) ^ set(td.__annotations__)}"
        )


# ══════════════════════════════════════════════════════════════
# 2. 默认值来自静态配置声明
# ══════════════════════════════════════════════════════════════
def test_thumbnail_defaults_come_from_declarations() -> None:
    _reset_config(_minimal_ini(_tmpdir()))

    conf = thumbnail_config()
    assert conf["quality"] == ThumbnailConfig.quality.default
    assert conf["thumb_root"] == ThumbnailConfig.thumb_root.default
    assert conf["batch_size"] == ThumbnailConfig.batch_size.default
    assert conf["image_format"] == ThumbnailConfig.image_format.default
    assert conf["xdg_enabled"] is ThumbnailConfig.xdg_enabled.default
    assert conf["specs"] == ThumbnailConfig.specs.default
    # xdg_spec_map 的默认值是 None（走内置映射）
    assert conf["xdg_spec_map"] is None


def test_import_defaults_come_from_declarations() -> None:
    _reset_config(_minimal_ini(_tmpdir()))

    conf = import_config()
    assert conf["batch_size"] == ImportConfig.batch_size.default
    assert conf["process_timeout"] == ImportConfig.process_timeout.default
    assert conf["initial_backoff"] == ImportConfig.initial_backoff.default
    assert conf["max_backoff"] == ImportConfig.max_backoff.default


def test_source_trash_defaults_come_from_declarations() -> None:
    _reset_config(_minimal_ini(_tmpdir()))

    conf = source_trash_config()
    assert conf["reconciler_name"] == SourceTrashDecl.reconciler_name.default
    assert conf["priority"] == SourceTrashDecl.priority.default
    assert conf["xdg_compat"] is None
    assert conf["session_marker_path"] is None


# ══════════════════════════════════════════════════════════════
# 3. INI 覆盖
# ══════════════════════════════════════════════════════════════
def test_ini_values_override_declaration_defaults() -> None:
    _reset_config(_minimal_ini(_tmpdir(), extra="""
        [thumbnail]
        quality = 70
        xdg_enabled = true
        thumb_root = /tmp/albuswall/thumbs

        [import]
        process_timeout = 42.5
        max_backoff = 120.0

        [source_trash]
        reconciler_name = custom
        priority = 3
        auto_cleanup_on_dirty = false
    """))

    thumb = thumbnail_config()
    assert thumb["quality"] == 70
    assert thumb["xdg_enabled"] is True
    assert thumb["thumb_root"] == "/tmp/albuswall/thumbs"

    imp = import_config()
    assert imp["process_timeout"] == 42.5
    assert imp["max_backoff"] == 120.0

    trash = source_trash_config()
    assert trash["reconciler_name"] == "custom"
    assert trash["priority"] == 3
    assert trash["auto_cleanup_on_dirty"] is False


# ══════════════════════════════════════════════════════════════
# 4. 同名字段按 owner 隔离
# ══════════════════════════════════════════════════════════════
def test_same_name_fields_are_isolated_by_owner() -> None:
    """``batch_size`` 两个服务都有：typed dict 必须各取各的 section。

    这正是不走全局注册表、而是显式传 owner 类的原因。
    """
    _reset_config(_minimal_ini(_tmpdir(), extra="""
        [thumbnail]
        batch_size = 7

        [import]
        batch_size = 700
    """))

    assert thumbnail_config()["batch_size"] == 7
    assert import_config()["batch_size"] == 700


def test_wrong_owner_raises_lookup_error() -> None:
    """把 ImportConfig 绑给 ThumbnailServiceConfig 应该直接报错。"""
    _reset_config(_minimal_ini(_tmpdir()))

    from albuswall.configue.typed import load_typed

    try:
        load_typed(ThumbnailServiceConfig, ImportConfig)
    except LookupError as exc:
        assert "specs" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("绑错 owner 应抛 LookupError")


# ══════════════════════════════════════════════════════════════
# 5. bootstrap 折叠 value object
# ══════════════════════════════════════════════════════════════
def test_bootstrap_folds_snapshot_into_dataclasses() -> None:
    tmp = _tmpdir()
    _reset_config(_minimal_ini(tmp, extra="""
        [source_trash]
        priority = 4
        debounce = 2.5
        orphan_min_age_seconds = 30.0
        auto_cleanup_on_dirty = false
        session_marker_path = /tmp/albuswall/session.marker
    """))

    conf = source_trash_config()

    trash = load_source_trash_config(conf)
    assert trash.priority == 4
    assert trash.debounce == 2.5

    cleaner = load_thumbnail_cleaner_config(conf)
    assert cleaner.enabled is True
    assert cleaner.min_age_seconds == 30.0

    session = load_session_config(conf)
    assert session.marker_path == Path("/tmp/albuswall/session.marker")
    assert session.auto_cleanup_on_dirty is False


def test_resolve_helpers_accept_snapshot() -> None:
    """归一化帮助函数既能自己取快照，也能接收调用方的快照。"""
    _reset_config(_minimal_ini(_tmpdir()))

    conf = dict(thumbnail_config())
    conf["specs"] = {"small": 64, "medium": 256, "large": 512}
    specs = resolve_specs(conf)
    assert {s.value: px for s, px in specs.items()} == {
        "small": 64, "medium": 256, "large": 512,
    }

    # 默认快照：specs 来自声明默认值
    assert resolve_specs() == resolve_specs(thumbnail_config())
    # thumb_root 默认值经 Path 展开后是绝对路径
    assert resolve_thumb_root(conf).is_absolute()


# ══════════════════════════════════════════════════════════════
# 6. 服务代码不再读旧描述符单例
# ══════════════════════════════════════════════════════════════
def test_services_never_read_descriptor_singleton() -> None:
    """AST 级检查：服务层不允许再出现 ``_cfg``（旧单例读法）。

    注释 / docstring 里的说明不算（AST 里没有它们）。
    """
    import ast

    offenders: list[str] = []
    for path in sorted(_SERVICES_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        lines = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Name) and node.id == "_cfg"
        ]
        if lines:
            offenders.append(f"{path.relative_to(_SERVICES_ROOT)}:{lines}")
    assert not offenders, f"这些服务文件仍在读旧描述符单例: {offenders}"


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
