#
"""DynamicDeclaration: 动态配置子树的登记 / 声明。

和 ``ConfigField`` 对齐的「导入期登记」契约
===========================================

``ConfigField`` 的声明（``__set_name__``）只往 ``ConfigDeclaration._registry``
写一条 meta，值的解析推迟到 ``ConfigDeclaration.load(resolver)``。本模块保持
同样的分层：

- **导入期**（配置 phase 之前、甚至静态配置还没影子时）只往 ``DYNAMIC_REGISTRY``
  写一条 ``DynamicMountSpec``，**不读配置、不读盘、不 import 状态类的模块**；
- **访问期**（``config.dynamic.<name>`` 首次访问）：类、``base_key``、``filename``
  才依次解析成真实对象 / 路径。

两种登记方式
============

装饰器（状态类所在的模块能在导入期被 import 时用）::

    @dynamic_state("user", ("path", "cache"), "user.json")
    class UserState(StatefulNamespace):
        user_id = StateField(str, default="anon")

手动指定（声明点想放在比状态类更早被 import 的模块里时用）::

    register_dynamic_state(
        "window",
        "albuswall.ui.builtin.presenter.window_presenter:WindowState",  # 延迟 import
        ("path", "cache"),
        WindowPresenterConfs.window_state_file_name,  # ConfigField → 取 .path
        autosave=False,
    )

``cls`` 支持三种形态：状态类本身 / ``"pkg.mod:Class"`` 字符串（访问期才 import）
/ 无参工厂。UI 这类「导入时机晚于配置加载」的模块只能走字符串或工厂。

``filename`` 支持：字面文件名 / 静态配置路径（如 ``("files", "window_state")``）/
带 ``.path`` 的声明（如 ``ConfigField``）；后两种访问期才解析。

配置期统一导入
==============

``register_dynamic_module("pkg.mod")`` 在导入期登记模块，配置期
``setup_dynamic_config``（``modules=None``）把它们全部 import 一遍。注意这
和上面的 ``cls`` 字符串是两件事：前者保证 ``@dynamic_state`` 装饰器跑过，
后者只是延迟取类。
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Sequence, Union

from .exceptions import ConfigError, ConfigueErrorSource
from .state import StatefulNamespace

__all__ = [
    "DynamicMountSpec",
    "dynamic_state",
    "register_dynamic_state",
    "DYNAMIC_REGISTRY",
    "DYNAMIC_MODULES",
    "register_dynamic_module",
    "reset_registry",
]

#: filename 可以写的形态：字面文件名 / 静态配置路径 / ConfigField 实例
FilenameSpec = Union[str, Path, Sequence[str], Any]

#: cls 可以写的形态：状态类 / "pkg.mod:Class" / 无参工厂
ClsSpec = Union[type[StatefulNamespace], str, Callable[[], type[StatefulNamespace]]]


# ── 形态归一 ────────────────────────────────────────
def _filename_key(filename: FilenameSpec) -> Optional[tuple[str, ...]]:
    """把 ``filename`` 归一成「静态配置路径」；字面文件名返回 ``None``。

    - ``str`` / ``Path``      → 字面文件名
    - ``ConfigField`` 等带 ``.path`` 的声明 → 用它的 path（1 或 2 段）
    - ``tuple`` / ``list``     → 直接当配置路径（元素必须是 str）

    其余形态抛 ``TypeError``（在导入期就炸，别拖到访问期）。
    """
    if isinstance(filename, (str, Path)):
        return None

    declared = getattr(filename, "path", None)
    if isinstance(declared, (tuple, list)) and declared:
        return tuple(declared)

    if isinstance(filename, (tuple, list)):
        parts = tuple(filename)
        if parts and all(isinstance(part, str) for part in parts):
            return parts
        raise TypeError(
            f"dynamic_state filename path must be non-empty str parts, "
            f"got {filename!r}"
        )

    raise TypeError(
        f"dynamic_state filename must be str / Path / config path / ConfigField, "
        f"got {type(filename).__name__}"
    )


def _import_cls(spec: str) -> type:
    """``"pkg.mod:Class"`` → 类（访问期才 import）。"""
    module_name, _, attr = spec.partition(":")
    if not module_name or not attr:
        raise TypeError(
            f"dynamic_state cls string must look like 'pkg.mod:Class', got {spec!r}"
        )
    module = importlib.import_module(module_name)
    try:
        return getattr(module, attr)
    except AttributeError as exc:
        raise TypeError(
            f"dynamic_state cls {spec!r}: {module_name!r} has no attribute {attr!r}"
        ) from exc


def _check_cls(target: Any, *, name: str) -> type[StatefulNamespace]:
    if not isinstance(target, type) or not issubclass(target, StatefulNamespace):
        raise TypeError(
            f"dynamic_state({name!r}): expected StatefulNamespace subclass, "
            f"got {target!r}"
        )
    return target


@dataclass(frozen=True)
class DynamicMountSpec:
    """一条动态子树登记：访问期才解析成类 / 路径。"""

    name: str
    # 状态类 / "pkg.mod:Class" / 无参工厂（三者都在访问期归一成类）
    cls: ClsSpec
    base_key: tuple[str, ...]  # 相对 StaticConfig 的路径，例如 ("path", "cache")
    # 字面文件名（filename_key 为空时使用）
    filename: Optional[str] = None
    # 静态配置里的路径（1 或 2 段），访问期解析，例如 ("files", "window_state")
    filename_key: Optional[tuple[str, ...]] = None
    autosave: bool = True
    overrides: dict[str, Any] = field(default_factory=dict)

    # ── 访问期解析 ─────────────────────────────────
    def resolve_cls(self) -> type[StatefulNamespace]:
        """取状态类：字符串 → 延迟 import，工厂 → 调用，类 → 原样。"""
        target = self.cls
        if isinstance(target, str):
            target = _import_cls(target)
        if not isinstance(target, type) and callable(target):
            target = target()
        return _check_cls(target, name=self.name)

    def resolve_filename(self, static) -> str:
        """把 filename 解析成文件名（静态配置就绪后才能调用）。"""
        if self.filename_key is None:
            if not self.filename:
                raise ConfigError(
                    f"dynamic_state({self.name!r}) has no filename",
                    source=ConfigueErrorSource.FILE,
                    key=self.name,
                )
            return str(self.filename)

        # 延迟 import：bootstrap 反向依赖本模块的 DYNAMIC_REGISTRY
        from .bootstrap import _walk_static

        value = _walk_static(
            static, self.filename_key, what="filename",
        )
        name = "" if value is None else str(value).strip()
        if not name:
            raise ConfigError(
                f"dynamic_state({self.name!r}) filename is empty: "
                f"[{'.'.join(self.filename_key)}]",
                source=ConfigueErrorSource.FILE,
                key=".".join(self.filename_key),
                value=value,
            )
        return name

    def resolve_path(self, static) -> Path:
        # 复用 bootstrap._resolve_path：base_key 打错时抛带完整 key 的
        # ConfigError（裸 AttributeError 不知道是哪个挂载点挂的）。
        from .bootstrap import _resolve_path

        return _resolve_path(static, self.base_key) / self.resolve_filename(static)


# name -> spec
DYNAMIC_REGISTRY: dict[str, DynamicMountSpec] = {}

#: 配置期需要 import 的模块（``register_dynamic_module`` 写入，去重保序）
DYNAMIC_MODULES: list[str] = []


# ── 登记 API ────────────────────────────────────────
def register_dynamic_state(
        name: str,
        cls: ClsSpec,
        base_key: Sequence[str],
        filename: FilenameSpec,
        *,
        autosave: bool = True,
        replace: bool = False,
        **overrides: Any,
) -> DynamicMountSpec:
    """手动登记一条动态子树（导入期安全，不 import 状态类所在模块）。

    Args:
        name: 挂载名，对应 ``config.dynamic.<name>``
        cls: 状态类 / ``"pkg.mod:Class"``（访问期才 import）/ 无参工厂
        base_key: 相对 StaticConfig 的目录路径，例如 ``("path", "cache")``
        filename: 字面文件名 / 静态配置路径 / 带 ``.path`` 的声明
        autosave: 修改时是否自动落盘
        replace: 重名时是否覆盖，默认 False 抛错
        overrides: 透传给 ``cls.load`` 的额外关键字，会覆盖文件里的值

    Returns:
        登记进去的 ``DynamicMountSpec``。

    适用于「声明点比状态类所在模块更早被 import」的场合（例如 UI 配置声明
    在插件阶段就被 import，而 UI 实现要等配置就绪后）。``cls`` 传字符串时，
    只有真正访问该子树才会 import 对应模块。
    """
    if isinstance(cls, type):
        # 类可以直接校验；字符串 / 工厂留到访问期再解析
        _check_cls(cls, name=name)
    elif isinstance(cls, str):
        if not cls.strip():
            raise TypeError(f"dynamic_state({name!r}): cls string is empty")
    elif not callable(cls):
        raise TypeError(
            f"dynamic_state({name!r}): cls must be a class, "
            f"'pkg.mod:Class' string or a factory, got {cls!r}"
        )

    filename_key = _filename_key(filename)
    literal = None if filename_key is not None else str(filename)

    if name in DYNAMIC_REGISTRY and not replace:
        raise RuntimeError(f"dynamic state {name!r} already registered")

    spec = DynamicMountSpec(
        name=name,
        cls=cls,
        base_key=tuple(base_key),
        filename=literal,
        filename_key=filename_key,
        autosave=autosave,
        overrides=dict(overrides),
    )
    DYNAMIC_REGISTRY[name] = spec
    return spec


def dynamic_state(
        name: str,
        base_key: Sequence[str],
        filename: FilenameSpec,
        *,
        autosave: bool = True,
        replace: bool = False,
        **overrides: Any,
):
    """装饰器形态的登记（等价于 ``register_dynamic_state`` + 返回类本身）。

    用法::

        @dynamic_state("user", ("path", "cache"), "user.json", autosave=True)
        class UserState(StatefulNamespace):
            user_id = StateField(str, default="anon")

    参数：
      name        : 挂载名，对应 _config.dynamic.<name>
      base_key    : 相对 StaticConfig 的目录路径，例如 ("path", "cache")
      filename    : 字面文件名 / 静态配置路径（如 ``("files", "window_state")``）/
                    任意带 ``.path`` 的声明（如 ``ConfigField``）；后两种到访问期才解析
      autosave    : 修改时是否自动落盘
      replace     : 重名时是否覆盖，默认 False 抛错
      overrides   : 透传给 cls.load 的额外关键字，会覆盖文件里的值

    注意：本装饰器只写登记表，不读配置也不读盘——可以在任何 import 期调用；
    但状态类必须在**同一个模块**里定义，所以该模块本身得在导入期被 import。
    模块被 import 得太晚（例如 UI 实现）时改用 ``register_dynamic_state`` +
    ``cls="pkg.mod:Class"``。
    """
    # 导入期就把 filename 形态校验掉，别等装饰类时才发现写错了
    _filename_key(filename)

    def deco(cls: type[StatefulNamespace]):
        register_dynamic_state(
            name, cls, base_key, filename,
            autosave=autosave, replace=replace, **overrides,
        )
        return cls

    return deco


def register_dynamic_module(*modules: str) -> None:
    """导入期登记「配置期要 import 的模块」（去重、保序）。

    适用于状态子树的定义模块不在插件声明模块同一条 import 链上的场景：

    .. code-block:: python

        @declare(PLUGIN_ID)
        def setup(container):
            from albuswall.configue import register_dynamic_module
            register_dynamic_module("albuswall_foo.state")

    配置期 ``setup_dynamic_config``（不传 ``modules``）会把这些模块全部 import，
    让其中的 ``@dynamic_state`` 跑过；``modules=[...]`` 显式传入时以显式为准。
    """
    for module in modules:
        if module and module not in DYNAMIC_MODULES:
            DYNAMIC_MODULES.append(module)


def reset_registry() -> None:
    """测试用：清空登记表（含待导入模块）。"""
    DYNAMIC_REGISTRY.clear()
    DYNAMIC_MODULES.clear()
