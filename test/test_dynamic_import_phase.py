#
"""动态子树的「导入期登记 + 配置期导入」契约。

覆盖本轮新增：

  1. ``@dynamic_state`` 是导入期安全的：只写登记表，不读配置
  2. ``filename`` 可以是静态配置路径（``("files", "state_name")``），
     访问期才解析成真实文件名
  3. ``filename`` 也可以直接传 ``ConfigField``（等价于它的 ``.path``）
  4. 配置值为空 → ``ConfigError``（不是静默写到一个空文件名）
  5. ``register_dynamic_module`` + ``setup_dynamic_config(modules=None)``：
     配置期把登记的模块全部 import（``DYNAMIC_REGISTRY`` 因此完整）
  6. 显式 ``modules=[...]`` 优先于登记表（空列表 = 什么都不 import）
  7. 顶层导出与 ``ConfigField`` 同一层：``from albuswall.configue import dynamic_state``
  8. 非法 filename 形态在导入期就抛 ``TypeError``

不依赖 pytest：直接 ``python test/test_dynamic_import_phase.py`` 运行，
装了 pytest 的话 ``pytest test/test_dynamic_import_phase.py`` 也能跑。
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

# 顶层导出（和 ConfigField 同一层）
from albuswall.configue import (
    ConfigField,
    DynamicMountSpec,
    StateField,
    StatefulNamespace,
    dynamic_state,
    register_dynamic_module,
)
from albuswall.configue.dynamic_declaration import (
    DYNAMIC_MODULES, DYNAMIC_REGISTRY,
)
from albuswall.configue.exceptions import ConfigError, ConfigueErrorSource


# ══════════════════════════════════════════════════════════════
# 模块级登记：模拟「插件在导入期声明状态子树」
# ══════════════════════════════════════════════════════════════
class ProbeConfs:
    """用来验证「filename 直接传 ConfigField」的声明类。"""

    state_name = ConfigField("files", "state_name", default="probe.json")


@dynamic_state(
    "probe_lazy_file", ("path", "cache"), ("files", "state_name"),
    autosave=False, replace=True,
)
class ProbeLazyFileState(StatefulNamespace):
    width = StateField(int, default=800)


@dynamic_state(
    "probe_field_file", ("path", "cache"), ProbeConfs.state_name,
    autosave=False, replace=True,
)
class ProbeFieldFileState(StatefulNamespace):
    height = StateField(int, default=600)


# ══════════════════════════════════════════════════════════════
# 工具
# ══════════════════════════════════════════════════════════════
def _reset_config(name: str = "probe.json") -> tuple[object, Path]:
    """建一个最小静态配置（path.cache + files.state_name）。"""
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

    d = Path(tempfile.mkdtemp(prefix="albuswall-dyn-import-"))
    main = d / "main.ini"
    main.write_text(dedent(f"""
        [path]
        cache = {d / "cache"}

        [files]
        state_name = {name}
    """).lstrip("\n"), encoding="utf-8")
    setup_static_config(Container(), main)
    return config, d


def _fresh_dynamic(config, name: str) -> None:
    config.dynamic.__dict__.pop(name, None)


def _state_path(state) -> Path:
    return Path(object.__getattribute__(state, "_ctx").path)


# ══════════════════════════════════════════════════════════════
# 导入期登记
# ══════════════════════════════════════════════════════════════
def test_decorator_registers_without_touching_config() -> None:
    """模块导入时（静态配置还没影子）就已经登记好了。"""
    spec = DYNAMIC_REGISTRY["probe_lazy_file"]

    assert isinstance(spec, DynamicMountSpec)
    assert spec.base_key == ("path", "cache")
    assert spec.filename is None, "配置路径形态不该有字面文件名"
    assert spec.filename_key == ("files", "state_name")


def test_config_field_filename_uses_its_path() -> None:
    spec = DYNAMIC_REGISTRY["probe_field_file"]

    assert spec.filename_key == ("files", "state_name")
    assert spec.filename is None


def test_literal_filename_stays_literal() -> None:
    @dynamic_state("probe_literal_tmp", ("path", "cache"), "literal.json",
                   replace=True)
    class _Literal(StatefulNamespace):
        a = StateField(int, default=1)

    spec = DYNAMIC_REGISTRY.pop("probe_literal_tmp")

    assert spec.filename == "literal.json"
    assert spec.filename_key is None


def test_bad_filename_type_raises_at_import_time() -> None:
    try:
        dynamic_state("probe_bad_filename", ("path", "cache"), 123)
    except TypeError as exc:
        assert "filename" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected TypeError for a bad filename spec")


# ══════════════════════════════════════════════════════════════
# 访问期解析
# ══════════════════════════════════════════════════════════════
def test_lazy_filename_resolves_from_static_config() -> None:
    config, d = _reset_config("probe.json")
    _fresh_dynamic(config, "probe_lazy_file")

    state = config.dynamic.probe_lazy_file

    assert isinstance(state, ProbeLazyFileState)
    assert _state_path(state) == d / "cache" / "probe.json"


def test_lazy_filename_follows_ini_value() -> None:
    config, d = _reset_config("custom-state.json")
    _fresh_dynamic(config, "probe_field_file")

    state = config.dynamic.probe_field_file

    assert _state_path(state) == d / "cache" / "custom-state.json"


def test_state_saves_to_resolved_lazy_path() -> None:
    config, d = _reset_config("saved.json")
    _fresh_dynamic(config, "probe_lazy_file")

    state = config.dynamic.probe_lazy_file
    state.width = 1234
    saved = state.save()

    assert Path(saved) == d / "cache" / "saved.json"
    assert Path(saved).is_file()


def test_empty_filename_raises_config_error() -> None:
    config, _ = _reset_config("")
    _fresh_dynamic(config, "probe_lazy_file")

    try:
        config.dynamic.probe_lazy_file
    except ConfigError as exc:
        assert exc.source is ConfigueErrorSource.FILE
        assert "filename" in str(exc)
        assert "files.state_name" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ConfigError for an empty filename")


def test_register_dynamic_state_with_class_string() -> None:
    """手动登记：cls 写 "pkg.mod:Class"，只有访问时才 import 该模块。"""
    from albuswall.configue import register_dynamic_state

    d = _make_probe_module(kind="plain")
    saved_registry = dict(DYNAMIC_REGISTRY)
    config, cache_dir = _reset_config("probe.json")
    try:
        sys.path.insert(0, str(d))
        assert _PROBE_PLAIN_MODULE not in sys.modules

        register_dynamic_state(
            "probe_str_cls", f"{_PROBE_PLAIN_MODULE}:PlainProbeState",
            ("path", "cache"), ("files", "state_name"),
        )
        # 登记本身不 import
        assert _PROBE_PLAIN_MODULE not in sys.modules

        _fresh_dynamic(config, "probe_str_cls")
        state = config.dynamic.probe_str_cls

        assert _PROBE_PLAIN_MODULE in sys.modules
        assert state.value == 3
        assert _state_path(state) == cache_dir / "cache" / "probe.json"
    finally:
        _cleanup_probe_module(d, _PROBE_PLAIN_MODULE)
        DYNAMIC_REGISTRY.clear()
        DYNAMIC_REGISTRY.update(saved_registry)


def test_register_dynamic_state_with_factory() -> None:
    from albuswall.configue import register_dynamic_state

    saved_registry = dict(DYNAMIC_REGISTRY)
    config, _ = _reset_config("probe.json")
    calls: list[str] = []

    def factory():
        calls.append("called")
        return ProbeLazyFileState

    try:
        register_dynamic_state(
            "probe_factory_cls", factory, ("path", "cache"), "factory.json",
        )
        assert calls == [], "工厂要等到访问期才调用"

        _fresh_dynamic(config, "probe_factory_cls")
        state = config.dynamic.probe_factory_cls

        assert calls == ["called"]
        assert isinstance(state, ProbeLazyFileState)
    finally:
        DYNAMIC_REGISTRY.clear()
        DYNAMIC_REGISTRY.update(saved_registry)


def test_register_dynamic_state_rejects_bad_cls() -> None:
    from albuswall.configue import register_dynamic_state

    try:
        register_dynamic_state("probe_bad_cls", 123, ("path", "cache"), "x.json")
    except TypeError as exc:
        assert "cls" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected TypeError for a bad cls spec")


def test_register_dynamic_state_bad_cls_string_raises_on_access() -> None:
    from albuswall.configue import register_dynamic_state

    saved_registry = dict(DYNAMIC_REGISTRY)
    config, _ = _reset_config("probe.json")
    try:
        register_dynamic_state(
            "probe_bad_cls_str", "no_colon_here", ("path", "cache"), "x.json",
        )
        _fresh_dynamic(config, "probe_bad_cls_str")
        try:
            config.dynamic.probe_bad_cls_str
        except TypeError as exc:
            assert "pkg.mod:Class" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected TypeError for a malformed cls string")
    finally:
        DYNAMIC_REGISTRY.clear()
        DYNAMIC_REGISTRY.update(saved_registry)


# ══════════════════════════════════════════════════════════════
# 配置期统一导入
# ══════════════════════════════════════════════════════════════
_PROBE_MODULE = "albuswall_dyn_import_probe"
_PROBE_SOURCE = '''
from albuswall.configue import dynamic_state, StateField, StatefulNamespace


@dynamic_state("probe_from_module", ("path", "cache"), "from-module.json",
               autosave=False, replace=True)
class ProbeFromModule(StatefulNamespace):
    value = StateField(int, default=7)
'''

_PROBE_PLAIN_MODULE = "albuswall_dyn_plain_probe"
_PROBE_PLAIN_SOURCE = '''
from albuswall.configue import StateField, StatefulNamespace


class PlainProbeState(StatefulNamespace):
    value = StateField(int, default=3)
'''


def _make_probe_module(kind: str = "decorated") -> Path:
    d = Path(tempfile.mkdtemp(prefix="albuswall-dyn-mod-"))
    if kind == "plain":
        (d / f"{_PROBE_PLAIN_MODULE}.py").write_text(
            _PROBE_PLAIN_SOURCE, encoding="utf-8",
        )
    else:
        (d / f"{_PROBE_MODULE}.py").write_text(_PROBE_SOURCE, encoding="utf-8")
    return d


def _cleanup_probe_module(d: Path, module: str = _PROBE_MODULE) -> None:
    DYNAMIC_REGISTRY.pop("probe_from_module", None)
    DYNAMIC_REGISTRY.pop("probe_str_cls", None)
    sys.modules.pop(module, None)
    while str(d) in sys.path:
        sys.path.remove(str(d))


def test_register_dynamic_module_is_imported_in_config_phase() -> None:
    from albuswall.configue.bootstrap import setup_dynamic_config
    from albuswall.core import Container

    d = _make_probe_module()
    saved_modules = list(DYNAMIC_MODULES)
    saved_registry = dict(DYNAMIC_REGISTRY)
    config, cache_dir = _reset_config("probe.json")
    try:
        sys.path.insert(0, str(d))

        register_dynamic_module(_PROBE_MODULE)
        assert _PROBE_MODULE not in sys.modules

        # 配置 phase：不传 modules → import 登记表里的全部模块
        setup_dynamic_config(Container())

        assert _PROBE_MODULE in sys.modules, "登记的模块必须在配置期被 import"
        assert "probe_from_module" in DYNAMIC_REGISTRY

        _fresh_dynamic(config, "probe_from_module")
        state = config.dynamic.probe_from_module
        assert state.value == 7
        assert _state_path(state) == cache_dir / "cache" / "from-module.json"
    finally:
        _cleanup_probe_module(d)
        DYNAMIC_MODULES[:] = saved_modules
        DYNAMIC_REGISTRY.clear()
        DYNAMIC_REGISTRY.update(saved_registry)


def test_explicit_modules_override_registered_list() -> None:
    from albuswall.configue.bootstrap import setup_dynamic_config
    from albuswall.core import Container

    d = _make_probe_module()
    saved_modules = list(DYNAMIC_MODULES)
    saved_registry = dict(DYNAMIC_REGISTRY)
    _reset_config()
    try:
        sys.path.insert(0, str(d))
        register_dynamic_module(_PROBE_MODULE)

        # 显式空列表 = 什么都不 import（登记表被忽略）
        setup_dynamic_config(Container(), modules=[])

        assert _PROBE_MODULE not in sys.modules
        assert "probe_from_module" not in DYNAMIC_REGISTRY
    finally:
        _cleanup_probe_module(d)
        DYNAMIC_MODULES[:] = saved_modules
        DYNAMIC_REGISTRY.clear()
        DYNAMIC_REGISTRY.update(saved_registry)


def test_register_dynamic_module_dedupes_and_keeps_order() -> None:
    saved_modules = list(DYNAMIC_MODULES)
    try:
        DYNAMIC_MODULES.clear()
        register_dynamic_module("mod_a", "mod_b", "mod_a", "")

        assert DYNAMIC_MODULES == ["mod_a", "mod_b"]
    finally:
        DYNAMIC_MODULES[:] = saved_modules


def test_failed_module_import_does_not_break_boot() -> None:
    from albuswall.configue.bootstrap import setup_dynamic_config
    from albuswall.core import Container

    saved_modules = list(DYNAMIC_MODULES)
    _reset_config()
    try:
        DYNAMIC_MODULES.clear()
        register_dynamic_module("definitely_not_an_importable_module_xyz")

        setup_dynamic_config(Container())  # 不抛
    finally:
        DYNAMIC_MODULES[:] = saved_modules


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
