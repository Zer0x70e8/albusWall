#
"""动态子树懒加载：``DynamicConfig.__getattr__`` 的 DYNAMIC_REGISTRY fallback。

覆盖阶段 5（可选、低优先）：

  1. 不调用 setup_dynamic_config，``config.dynamic.<name>`` 也能加载
  2. 登记本身不读盘（访问前不可见）
  3. 并发：两个线程首次访问同一 name → 返回同一个对象、不抛异常
  4. 未知 name → AttributeError
  5. mount_lazy 语义不变（显式工厂优先）
  6. setup_dynamic_config：显式 modules 列表只 import 那些，preload 立即加载

不依赖 pytest：直接 ``python test/test_dynamic_lazy.py`` 运行，
装了 pytest 的话 ``pytest test/test_dynamic_lazy.py`` 也能跑。
"""

from __future__ import annotations

import sys
import tempfile
import threading
import traceback
from pathlib import Path
from textwrap import dedent

try:  # 允许直接以文件路径运行
    import albuswall  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from albuswall.configue.dynamic_declaration import (
    DYNAMIC_REGISTRY, dynamic_state, reset_registry,
)
from albuswall.configue.state import StatefulNamespace, StateField


# ══════════════════════════════════════════════════════════════
# 登记（模块级，模拟业务模块被 import 的时机）
# ══════════════════════════════════════════════════════════════
@dynamic_state(
    "lazy_user", ("path", "temp"), "albuswall-lazy-user.json",
    autosave=False, replace=True,
)
class LazyUserState(StatefulNamespace):
    user_id = StateField(str, default="anon")


# ══════════════════════════════════════════════════════════════
# 工具
# ══════════════════════════════════════════════════════════════
def _reset_config():
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

    d = Path(tempfile.mkdtemp(prefix="albuswall-dyn-"))
    main = d / "main.ini"
    main.write_text(dedent("""
        [ui]
        width = 1
    """).lstrip("\n"), encoding="utf-8")
    setup_static_config(Container(), main)
    return config


def _fresh_dynamic(config):
    """清掉已缓存的挂载，让 __getattr__ 重新走一遍加载。"""
    for name in list(DYNAMIC_REGISTRY):
        if name in config.dynamic.__dict__:
            config.dynamic.__dict__.pop(name, None)
    return config


# ══════════════════════════════════════════════════════════════
# fallback 加载
# ══════════════════════════════════════════════════════════════
def test_registry_entry_alone_does_not_load() -> None:
    config = _fresh_dynamic(_reset_config())
    assert "lazy_user" not in config.dynamic.__dict__

    assert "lazy_user" not in config.dynamic.to_dict()


def test_getattr_falls_back_to_registry() -> None:
    """没调用 setup_dynamic_config 也能访问。"""
    config = _fresh_dynamic(_reset_config())

    state = config.dynamic.lazy_user

    assert isinstance(state, LazyUserState)
    assert state.user_id == "anon"
    # 加载后缓存到 __dict__，并出现在视图里
    assert config.dynamic.__dict__["lazy_user"] is state
    assert "lazy_user" in config.dynamic.to_dict()


def test_second_access_returns_same_object() -> None:
    config = _fresh_dynamic(_reset_config())

    assert config.dynamic.lazy_user is config.dynamic.lazy_user


def test_unknown_name_raises_attribute_error() -> None:
    config = _fresh_dynamic(_reset_config())

    try:
        config.dynamic.definitely_not_registered
    except AttributeError as exc:
        assert "definitely_not_registered" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected AttributeError")


def test_concurrent_first_access_returns_same_object() -> None:
    config = _fresh_dynamic(_reset_config())

    results: list[object] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def worker() -> None:
        try:
            barrier.wait()
            results.append(config.dynamic.lazy_user)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, errors
    assert len(results) == 2
    assert results[0] is results[1], "同名挂载必须只返回一个对象"


# ══════════════════════════════════════════════════════════════
# 旧语义不变
# ══════════════════════════════════════════════════════════════
def test_mount_lazy_still_wins() -> None:
    config = _fresh_dynamic(_reset_config())

    sentinel = LazyUserState(autosave=False)
    config.dynamic.mount_lazy("explicit", lambda: sentinel)

    assert config.dynamic.explicit is sentinel


def test_setup_dynamic_config_preload() -> None:
    from albuswall.configue.bootstrap import setup_dynamic_config
    from albuswall.core import Container

    config = _fresh_dynamic(_reset_config())
    setup_dynamic_config(Container(), modules=[], preload=["lazy_user"])

    assert isinstance(config.dynamic.lazy_user, LazyUserState)
    assert "lazy_user" in config.dynamic.__dict__


def test_dynamic_view_filters_internal_attrs() -> None:
    config = _fresh_dynamic(_reset_config())
    config.dynamic.lazy_user

    public = dict(config.dynamic.items())

    assert "lazy_user" in public
    assert not [k for k in public if k.startswith("_")]


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
