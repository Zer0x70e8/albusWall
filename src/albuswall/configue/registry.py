#
"""声明 → 信息登记层。

只负责数据结构、全局表、``reg()``、``@configclass``、``ROOT``、校验与覆盖语义，
以及挂在这些数据结构上的 ``_ConfigSlot`` 描述符的运行时取值 / 赋值。
``load()`` / ``unload()`` / ``check()`` / ``tree`` / ``flush()`` / ``close()``
的运行期实现全部在 ``configue.py`` / ``tree.py``。

命名约定：
- ``path``      —— 配置访问路径，类型 ``tuple[str, ...]``，如 ``("core", "debug")``
- ``file_path`` —— 文件系统路径，类型 ``str``

**声明共享**：同一个 ``(section, key)``（节点）可以被任意多个 ``@configclass``
声明——它们读到 / 写到的是同一个 Resolver 节点。共享的前提是声明**等价**：

- 类型相同（``_same_converter`` 语义：``is`` 或 ``==``）；
- 默认值相同（都写了且相等，或都没写）；
- ``label`` / ``description`` 不参与判定（纯展示信息，谁非空用谁的）。

任一不一致 → 装饰期抛 ``ConfigDeclarationConflict``，不让「同一个格子两种解释」
悄悄变成后写者赢。

契约（下游实现依赖，改动前请先读）：

- ``order`` 由调用方保证为 ``int``；本层只做透传，不做类型校验。
- ``FieldInfo.default`` 是**声明值**（类体里写的那个对象，``MISSING`` 表示未声明）。
  实例化时的复制 / 注入由访问层负责——不可变默认值可共享，可变默认值必须
  由写值方自行拷贝，本层不替任何人复制。
- ``_ConfigSlot.type`` / ``_ConfigSlot.default`` 是**装饰时快照**；装饰完成后
  它们可能过期（重复装饰、类级覆盖）。运行时路由一律以 ``_CLASSES[type(instance)]``
  为准，slot 上的两个字段仅作调试 / 文档用途。
- ``_CLASSES`` 以**类对象**为键，因此同名 section 的多个类并存；``_PATHS``
  以 ``(file, class_path)`` 为键，值是**类列表**（同一 section 的多份声明都
  在里面，``classes_in`` 只是它的派生视图）。
"""

from __future__ import annotations

import importlib
import logging
import types
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import (
    Any,
    Callable,
    ClassVar,
    Literal,
    Union,
    get_args,
    get_origin,
)

from .errors import (
    ConfigClassNotRegistered,
    ConfigDeclarationConflict,
    ConfigError,
    ConfigFileNotFound,
    ConfigMissingError,
)
from .utils.annotation import resolve_annotation

__all__ = [
    "ROOT",
    "reg",
    "configclass",
    "field",
    "classes_in",
    "declarations",
    "registered_files",
    "field_owner",
    "ConfigFile",
    "AutosavePolicy",
    "FileInfo",
    "FieldInfo",
    "ClassInfo",
    "ConfigError",
    "ConfigFileNotFound",
    "ConfigDeclarationConflict",
    "MISSING",
]

_logger = logging.getLogger(__name__)

#: 本模块新建的哨兵，避免与 declaration.py 的循环 import。
_MISSING = object()
#: 公开哨兵别名。访问层用它判断 ``FieldInfo.default`` / ``_ConfigSlot.default``。
MISSING = _MISSING

#: 默认根路径名。
ROOT = "__default__"

# ---------------------------------------------------------------------------
# 枚举 / 类型别名 / 错误
# ---------------------------------------------------------------------------

# noinspection SpellCheckingInspection
AutosavePolicy = Literal["off", "manual", "atexit", "debounce", "now"]


class ConfigFile(str, Enum):
    INI = "ini"
    JSON = "json"
    TOML = "toml"
    YAML = "yaml"


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FileInfo:
    """一个已 ``reg`` 的配置文件。

    ``resolved_path`` 是 ``load()`` 成功读盘后写入的绝对路径；``unload()``
    会把它置回 ``None``。未 ``load`` 时它恒为 ``None``——不要用它判断文件
    是否存在，用 ``loaded``。

    ``optional=True``（``reg(optional=True)``）：文件不存在**不算错误**，
    ``load()`` 会以 ``base_dir / file_path`` 作为占位路径把它标成已加载，
    于是默认值照常注入、``tree.<file>.save()`` 也能在首次写回时创建文件。
    注意 ``optional`` 只豁免「文件不存在」，**不豁免**歧义（多候选同时存在
    仍然是用户放错了文件）。
    """

    name: str
    backend: ConfigFile
    file_path: str
    autosave: AutosavePolicy
    debounce: float
    order: int
    loaded: bool = False
    resolved_path: Path | None = None
    optional: bool = False


@dataclass(frozen=True)
class FieldInfo:
    """一个已声明的配置字段。

    ``default`` 是声明值：``MISSING`` 表示「没写默认值」。访问层负责在实例化
    时决定是否复制它（本层不复制）。
    """

    path: tuple[str, ...]
    type: Any
    default: Any
    label: str = ""
    description: str = ""

    @property
    def has_default(self) -> bool:
        """访问层可据此判断是否需要走「无默认值」分支，无需导入 MISSING。"""
        return self.default is not MISSING


@dataclass(frozen=True)
class ClassInfo:
    cls: type
    path: tuple[str, ...]
    file: str
    fields: tuple[FieldInfo, ...]
    order: int = 0


@dataclass(frozen=True)
class _FieldSpec:
    """``field()`` 的返回值，仅在 ``configclass`` 内被消费。"""

    default: Any = _MISSING
    label: str = ""
    description: str = ""


def field(
        *,
        default: Any = _MISSING,
        label: str = "",
        description: str = "",
) -> Any:
    """声明字段元数据，用法类似 ``dataclasses.field``。

    示例::

        @configclass("core", "main")
        class Core:
            debug: bool = field(default=False, label="调试", description="打开调试日志")
            port: int = 8000  # 也可直接用普通默认值
    """
    return _FieldSpec(default=default, label=label, description=description)


# ---------------------------------------------------------------------------
# 全局表
# ---------------------------------------------------------------------------

_FILES: dict[str, FileInfo] = {}
_CLASSES: dict[type, ClassInfo] = {}
#: ``(file, class_path) → 声明该 section 的类列表``（登记顺序）。
#:
#: **同名 section 允许多份声明**：两个模块各自 ``@configclass("core", "main")``
#: 时会同时存在于此表里，只要字段声明等价（见模块 docstring 的「声明共享」）。
#: 同一个类被重复装饰则原地替换它自己的条目（幂等）。
_PATHS: dict[tuple[str, tuple[str, ...]], list[ClassInfo]] = {}
#: ``(section, key) → file name`` 反查表，``configclass`` 装饰期填充。
#: 访问层在「节点没有 origin」时靠它决定兜底写回哪个文件，避免每次 flush
#: 都遍历 ``_CLASSES``（热路径）。多个类声明同一节点时**后装饰者覆盖**
#: （跨文件共享时决定「默认值写回哪个文件」，与 ``_CLASSES`` 的迭代顺序一致）。
_FIELD_OWNER: dict[tuple[str, str], str] = {}


def classes_in(file: str) -> tuple[type, ...]:
    """返回登记到指定文件 name 下的所有类，按 ``ClassInfo.order`` **降序**。

    大者在前：``order`` 越大越靠前（与「同文件多类、大者优先」的排序约定一致）。
    同 ``order`` 时按注册顺序（稳定排序）。

    这是 ``_CLASSES`` 的派生视图，不引入第三张表；同一个 section 的多份声明
    都会出现在这里（它们共享取值，只是各自的字段集合可能不同）。
    """
    return tuple(sorted(
        (cls for cls, info in _CLASSES.items() if info.file == file),
        key=lambda cls: _CLASSES[cls].order,
        reverse=True,
    ))


def declarations() -> tuple[ClassInfo, ...]:
    """返回全部已登记声明的 ``ClassInfo``，按「稳定的全局顺序」排序。

    排序键（依次比较）：

    1. 所属 ``FileInfo.order`` **升序**——与 ``load()`` 的合并顺序一致，
       低优先级文件在前；
    2. ``ClassInfo.order`` **降序**——同文件内大者优先（同 ``classes_in``）；
    3. 登记顺序（稳定排序兜底）。

    ``ClassInfo.file`` 引用了一个尚未 ``reg()`` 的名字时按 ``order=0`` 参与
    排序而不是抛错：schema / 文档生成这类只读消费方必须能在「声明先于
    注册」的中间态下工作。
    """
    def file_order(info: ClassInfo) -> int:
        finfo = _FILES.get(info.file)
        return 0 if finfo is None else finfo.order

    indexed = list(enumerate(_CLASSES.values()))
    indexed.sort(key=lambda item: (
        file_order(item[1]), -item[1].order, item[0],
    ))
    return tuple(info for _index, info in indexed)


def registered_files() -> tuple[FileInfo, ...]:
    """返回全部已 ``reg()`` 的配置文件，按 ``order`` **升序**。

    未 ``load()`` 的文件同样在里面（``FileInfo.loaded`` 为 ``False``）；
    需要「只列已加载」的调用方自己过滤 ``loaded`` / ``resolved_path``。
    """
    return tuple(sorted(_FILES.values(), key=lambda f: f.order))


def field_owner(section: str, key: str) -> str | None:
    """``(section, key) → 声明它的文件 name``；无人声明时返回 ``None``。

    ``_FIELD_OWNER`` 的薄包装：把「节点属于哪个文件」这条规则收在一个地方，
    避免外部调用方直接摸私有表（``flush`` 的兜底规则就是它）。
    """
    return _FIELD_OWNER.get((section, key))


# ---------------------------------------------------------------------------
# _ConfigSlot 描述符
# ---------------------------------------------------------------------------

class _ConfigSlot:
    """类属性占位描述符。

    ``@configclass`` 把字段默认值替换为本类实例，用于拦截
    ``Config().debug = x`` 这类**实例级**赋值。

    重要边界：Python 没有 metaclass 拦截时，**类级赋值 ``Config.debug = x``
    会直接替换掉本描述符**。因此访问层不应依赖「类属性始终是 slot」这一前提，
    路由与状态一律以 ``_CLASSES[type(instance)]`` 为准。

    ``type`` / ``default`` 在 ``configclass`` 构造时即回填，是**装饰时快照**；
    访问层可直接 ``Config.debug.type`` / ``Config.debug.default`` 读取
    （在未被类级覆盖前）。运行时路由不走这两个字段，走 ``_CLASSES``。
    """

    __slots__ = ("path", "type", "default")

    def __init__(
            self,
            path: tuple[str, ...],
            *,
            type: Any = None,
            default: Any = _MISSING,
    ) -> None:
        self.path = path  # 完整访问路径，如 ("core", "debug")
        self.type = type
        self.default = default

    def __get__(self, instance, owner=None):
        if instance is None:
            return self

        cls = type(instance)
        info = _CLASSES.get(cls)
        if info is None:
            raise ConfigClassNotRegistered(
                f"{cls.__qualname__} is not registered in _CLASSES "
                f"(is @configclass applied to it?)"
            )

        config = _get_configue()
        section, key = self.path[0], self.path[-1]
        if (section, key) not in config.resolver:
            finfo = _FILES.get(info.file)
            loaded = finfo is not None and finfo.loaded
            hint = (
                "call Configue.load() first"
                if not loaded
                else "field has no default and is not present in config files"
            )
            raise ConfigMissingError(
                f"[{info.file}] {'.'.join(self.path)} has no value: {hint}",
                path=self.path,
                file=info.file,
            )
        return config.resolver.get_typed(section, key)

    def __set__(self, instance, value):
        cls = type(instance)
        info = _CLASSES.get(cls)
        if info is None:
            raise ConfigClassNotRegistered(
                f"{cls.__qualname__} is not registered in _CLASSES "
                f"(is @configclass applied to it?)"
            )

        config = _get_configue()
        section, key = self.path[0], self.path[-1]

        expected = self.type
        if expected is None or expected is Any:
            pass  # 无类型约束
        elif value is None:
            if not _is_optional(expected):
                raise TypeError(
                    f"{info.file}.{'.'.join(self.path)}: "
                    f"expected {expected}, got None"
                )
        elif not _type_matches(value, expected):
            raise TypeError(
                f"{info.file}.{'.'.join(self.path)}: "
                f"expected {expected}, got {type(value).__name__}"
            )

        config.resolver.set(section, key, value, persist=True)
        config._schedule_persist(info.file)


# ---------------------------------------------------------------------------
# 访问层辅助（延迟 import 避免 configue ↔ registry 循环依赖）
# ---------------------------------------------------------------------------

# noinspection protected-member
def _get_configue():
    """返回当前 ``Configue`` 单例；未构造时抛 ``ConfigMissingError``。

    延迟 import：``configue.py`` 依赖本模块，本模块只能在函数体里反向取。
    """
    from .configue import Configue

    inst = Configue._instance
    if inst is None:
        raise ConfigMissingError(
            "Configue not initialized; construct Configue(base_dir=...) first"
        )
    return inst


def _is_optional(type_: Any) -> bool:
    """``Optional[X]`` / ``X | None`` 判定。"""
    origin = get_origin(type_)
    if origin is Union or origin is getattr(types, "UnionType", Union):
        return type(None) in get_args(type_)
    return False


def _type_matches(value: Any, expected: Any) -> bool:
    """严格 ``isinstance`` 校验；无法静态判定的类型 fail-open。

    - ``Union`` / ``X | None``：任一分支命中即可（``Optional[bool]`` 不会被
      误判成「任意值都行」）；
    - 泛型别名（``list[str]``）不能直接 ``isinstance``，退化成对 ``origin``
      的判定（只校验容器种类，不校验元素类型——元素级校验交给读取期的
      ``_convert``）；
    - 其它无法 ``isinstance`` 的（``Any`` / 特殊形式）fail-open。
    """
    origin = get_origin(expected)
    if origin is Union or origin is getattr(types, "UnionType", Union):
        return any(_type_matches(value, arg) for arg in get_args(expected))

    probe = origin if origin is not None else expected
    if probe is type(None):
        return value is None
    try:
        return isinstance(value, probe)
    except TypeError:
        return True


# ---------------------------------------------------------------------------
# 路径归一
# ---------------------------------------------------------------------------

def _normalize_path(path: str | tuple[str, ...]) -> tuple[str, ...]:
    if isinstance(path, str):
        return (path,)
    return tuple(path)


# ---------------------------------------------------------------------------
# 延迟解析 Configue.base_dir（运行时，不参与导入期）
# ---------------------------------------------------------------------------

# noinspection broad-exception,pep8-naming
def _resolve_base_dir() -> Path | None:
    """尽力查找 Configue._instance.base_dir；不可用时返回 None。

    仅在 ``exists_check=True`` 时调用。默认路径（``False``）下导入期零 IO。
    """
    pkg = __name__.rsplit(".", 1)[0]  # "albuswall.configue"
    for modname in (pkg, pkg + ".configue"):
        try:
            mod = importlib.import_module(modname)
        except Exception:  # noqa: BLE001  import 失败等同于「没有单例」
            continue
        Configue = getattr(mod, "Configue", None)
        if Configue is None:
            continue
        inst = getattr(Configue, "_instance", None)
        if inst is None:
            continue
        base_dir = getattr(inst, "base_dir", None)
        if base_dir is None:
            return None
        # noinspection bad-argument-type
        return Path(base_dir)
    return None


# ---------------------------------------------------------------------------
# reg()
# ---------------------------------------------------------------------------

def reg(
        backend: ConfigFile,
        file_path: str,
        *,
        name: str,
        autosave: AutosavePolicy = "debounce",
        debounce: float = 0.3,
        order: int = 0,
        exists_check: bool = False,
        optional: bool = False,
) -> None:
    """登记一个配置文件。重名覆盖，loaded 重置为 False。

    默认 ``exists_check=False`` 时不做任何 IO，可在模块顶层安全调用；
    显式传入 ``exists_check=True`` 会立即检查文件系统，路径相对
    ``Configue.base_dir`` 解析。**此时必须已经有活动的 ``Configue`` 单例**，
    否则抛 ``ConfigError``——不再回退到 cwd，避免「相对谁的路径」这种
    隐式歧义（cwd 与 base_dir 不同时，静默回退会在别的地方炸）。

    参数：
        optional: ``True`` 时「文件不存在」不算错误（见 ``FileInfo``）；
            与 ``exists_check=True`` 互斥（一个要求现在就存在，一个要求
            不存在也行），同时传抛 ``ValueError``。**歧义不受豁免**。
    """
    if not name:
        raise ValueError("reg: name must be non-empty")
    if name.startswith("_"):
        raise ValueError(f"reg: name must not start with '_': {name!r}")

    valid_autosave = get_args(AutosavePolicy)
    if autosave not in valid_autosave:
        raise ValueError(
            f"reg: invalid autosave {autosave!r}; expected one of {valid_autosave}"
        )

    if debounce < 0:
        raise ValueError(f"reg: debounce must be >= 0, got {debounce!r}")

    if exists_check and optional:
        # 语义冲突：exists_check 要求「现在就必须存在」，optional 要求
        # 「不存在也行」。显式报错，别让调用方以为其中一个生效了。
        raise ValueError(
            "reg: exists_check=True and optional=True are mutually exclusive"
        )

    if exists_check:
        base_dir = _resolve_base_dir()
        if base_dir is None:
            raise ConfigError(
                "Configue not initialized; exists_check requires an active Configue"
            )
        check_path = base_dir / file_path
        if not check_path.exists():
            raise ConfigFileNotFound(f"config file not found: {file_path}")

    _FILES[name] = FileInfo(
        name=name,
        backend=backend,
        file_path=file_path,
        autosave=autosave,
        debounce=debounce,
        order=order,
        loaded=False,
        resolved_path=None,
        optional=optional,
    )


# ---------------------------------------------------------------------------
# @configclass
# ---------------------------------------------------------------------------

def configclass(
        path: str | tuple[str, ...],
        file: str,
        *,
        order: int = 0,
) -> Callable[[type], type]:
    """登记一个配置类。

    只读取 ``cls.__dict__["__annotations__"]``（自身注解），**不收集父类字段**。
    需要复用字段请显式在子类重新声明。``file`` 此时不校验是否已 ``reg``，
    未注册引用留到 ``load()`` 时报错。

    **同名声明共享**：``(path, file)`` 相同的多个类可以同时存在，它们读写的
    是同一个 Resolver 节点，因此「多个模块各自声明同一 section」是合法写法：

    - 字段集合可以不同（各声明各的，合并成一个 section 的字段集）；
    - 同一个 ``(section, key)`` 被两处声明时，两处的**类型与默认值必须等价**，
      否则装饰期抛 ``ConfigDeclarationConflict``（不静默让后写者赢）；
    - 同一个类被重复装饰是幂等的：用新注解 / 默认值原地刷新它自己的登记。

    ``order`` 用于同文件多类的稳定排序（大者优先），与 ``FileInfo.order`` 独立；
    ``order`` 由调用方保证为 ``int``，本层只透传。

    装饰时机影响默认值注入：

    - ``Configue`` **未**构造：默认值等 ``load()`` 的 Step 4 注入（遍历
      ``_CLASSES``，覆盖「声明先于加载」的常规路径）；
    - ``Configue`` **已**构造：装饰当下立即注入
      （``inject_raw(overwrite=False)``），**不覆盖**文件里写的值 / 运行期
      ``set`` 过的值。这是「插件 / UI 在 ``load()`` 之后才 import」的兜底，
      否则这些类的字段默认值永远进不了 Resolver。
    """
    class_path = _normalize_path(path)

    def decorator(cls: type) -> type:
        # 注意：**不能**用 ``cls.__dict__["__annotations__"]``——
        # 3.14 起类注解是 PEP 649 的惰性值（``__annotate_func__``），类字典里
        # 根本没有这个 key；必须走属性访问触发求值（3.11+ 对无注解的类返回
        # 空 dict，不会把父类注解带进来）。
        #
        # TODO(PEP 649 粒度)：惰性求值是**整体**求值——任意一个注解里的名字
        # import 失败会在这里整体抛异常，而不是「该字段退化为 Any」。目前退化为
        # 「整个类没有字段」并告警；将来要用 annotationlib 做逐字段求值，才能
        # 只让坏字段 fail-open。
        try:
            annotations_ = getattr(cls, "__annotations__", None) or {}
        except Exception as exc:  # noqa: BLE001  注解求值失败不能炸导入期
            _logger.warning(
                "configclass: failed to evaluate annotations for %s: %s; "
                "treating the class as having no fields",
                cls.__qualname__, exc,
            )
            annotations_ = {}

        fields: list[FieldInfo] = []
        for name, raw in annotations_.items():
            if name.startswith("_"):
                continue

            if isinstance(raw, str):
                resolved = resolve_annotation(raw, owner=cls)
                if resolved is None:
                    _logger.warning(
                        "configclass: failed to resolve annotation %r for %s.%s; "
                        "treating as Any",
                        raw, cls.__qualname__, name,
                    )
                    resolved = Any
            else:
                resolved = raw

            # 裸 ClassVar 的 get_origin 是 None，需要单独判断 is ClassVar
            if resolved is ClassVar or get_origin(resolved) is ClassVar:
                continue

            raw_default = cls.__dict__.get(name, _MISSING)
            if isinstance(raw_default, _FieldSpec):
                default = raw_default.default
                label = raw_default.label
                description = raw_default.description
            elif isinstance(raw_default, _ConfigSlot):
                # 重复装饰幂等：从已有 slot 恢复 default；label/description 退化为空
                default = raw_default.default
                label = ""
                description = ""
            else:
                default = raw_default
                label = ""
                description = ""

            field_path = class_path + (name,)
            fields.append(FieldInfo(field_path, resolved, default, label, description))

        # 撞方法名检测
        for f in fields:
            last = f.path[-1]
            existing = cls.__dict__.get(last, _MISSING)
            if (
                    existing is not _MISSING
                    and callable(existing)
                    and not isinstance(existing, _ConfigSlot)
            ):
                raise ValueError(
                    f"configclass: field {last!r} on {cls.__qualname__} "
                    f"shadows an existing method"
                )

        # ── 共享声明校验 ────────────────────────────────────────────
        # 同名 section 允许多份声明（它们共享同一个 Resolver 节点），但同一个
        # (section, key) 的**类型与默认值必须等价**。校验在任何全局表变更之前
        # 完成，冲突抛错时登记表保持原样——不留半登记状态。
        conflicts = _collect_declaration_conflicts(cls, file, fields)
        if conflicts:
            raise ConfigDeclarationConflict("\n".join(conflicts))

        # 替换类属性为 _ConfigSlot。
        # 重复装饰时**不能** `continue`：注解类型 / 默认值可能已经变了，
        # 旧 slot 的快照必须跟着刷新（path 可能因类换 namespace 而变）。
        for f in fields:
            last = f.path[-1]
            existing = cls.__dict__.get(last)
            if isinstance(existing, _ConfigSlot):
                existing.path = f.path
                existing.type = f.type
                existing.default = f.default
                continue
            slot = _ConfigSlot(f.path, type=f.type, default=f.default)
            setattr(cls, last, slot)

        class_info = ClassInfo(cls, class_path, file, tuple(fields), order)
        key = (file, class_path)

        # 同一个类换过 (file, path) 或重复装饰时先把它自己摘干净，
        # 保证一个类只出现在一个 section 的声明列表里，且位置反映最新一次装饰。
        # **不再**把同名 section 的其它类挤出去：它们是并存的多份声明。
        for bucket in _PATHS.values():
            bucket[:] = [ci for ci in bucket if ci.cls is not cls]
        for stale in [k for k, bucket in _PATHS.items() if not bucket]:
            del _PATHS[stale]

        _PATHS.setdefault(key, []).append(class_info)
        _CLASSES[cls] = class_info

        # 字段 → 文件 的反查表：访问层在「节点没有 origin」时靠它兜底写回。
        # 同一节点被多处声明时后装饰者覆盖（跨文件时决定默认值写回哪个文件）。
        for f in fields:
            _FIELD_OWNER[(f.path[0], f.path[-1])] = file

        # 调试用 __repr__：只在用户没自定义时注入（用户写的永远优先）。
        # 重复装饰时替换掉我们自己上一次注入的那个（字段可能变了）。
        existing_repr = cls.__dict__.get("__repr__")
        if existing_repr is None or getattr(
                existing_repr, "_configue_generated", False,
        ):
            cls.__repr__ = _make_config_repr(cls)

        # 晚注册兜底：Configue 已构造时立即注入类型/默认值；早注册让
        # load() 的 Step 4 处理。必须在 _FIELD_OWNER 更新之后——注入的值没有
        # origin，flush 要靠 _FIELD_OWNER 回查归属文件。
        _inject_defaults_for_late_class(fields)

        return cls

    return decorator


# ---------------------------------------------------------------------------
# 共享声明：一致性校验
# ---------------------------------------------------------------------------

def _defaults_equal(one: Any, other: Any) -> bool:
    """两个声明默认值是否等价（``MISSING`` 只与 ``MISSING`` 等价）。"""
    if one is other:
        return True
    if one is _MISSING or other is _MISSING:
        return False
    try:
        return bool(one == other)
    except Exception:  # noqa: BLE001  比较不出来就当不一致
        return False


def _describe_declaration(cls: type, file: str, f: FieldInfo) -> str:
    """``Core (main): bool = False`` 形式的一行声明摘要。"""
    type_name = getattr(f.type, "__name__", None) or repr(f.type)
    default = "<no default>" if f.default is _MISSING else repr(f.default)
    return f"{cls.__qualname__} ({file}): {type_name} = {default}"


def _collect_declaration_conflicts(
        cls: type,
        file: str,
        fields: list[FieldInfo],
) -> list[str]:
    """新声明与已登记声明之间的冲突描述（空列表 = 全部等价，可以共享）。

    比较维度只有两个：**类型** 与 **默认值**（``label`` / ``description`` 是
    纯展示信息，不参与判定）。同一个类自己的旧声明不参与比较——重复装饰
    允许改动注解与默认值，那是在刷新自己。
    """
    conflicts: list[str] = []
    reported: set[tuple[str, str]] = set()
    for f in fields:
        node = (f.path[0], f.path[-1])
        for other_cls, other_info in list(_CLASSES.items()):
            if other_cls is cls:
                continue
            for other_f in other_info.fields:
                if (other_f.path[0], other_f.path[-1]) != node:
                    continue
                problems: list[str] = []
                if not _same_converter(f.type, other_f.type):
                    problems.append(
                        f"type {other_f.type!r} != {f.type!r}"
                    )
                if not _defaults_equal(f.default, other_f.default):
                    problems.append(
                        f"default {other_f.default!r} != {f.default!r}"
                    )
                if not problems:
                    continue
                # 同一节点只报第一个对不上的声明：多个类共享同一节点时，
                # 逐个罗列只会把同一条信息重复 N 遍。
                if node in reported:
                    break
                reported.add(node)
                conflicts.append(
                    f"configclass: conflicting declarations for "
                    f"[{file}] {'/'.join(node)} ({'; '.join(problems)})\n"
                    f"  + {_describe_declaration(cls, file, f)}\n"
                    f"  - {_describe_declaration(other_cls, other_info.file, other_f)}"
                )
    return conflicts


def _make_config_repr(cls: type) -> Callable[[Any], str]:
    """生成 ``Config()`` 的 ``__repr__``：列出每个字段的当前值。

    - ``Configue`` 未构造 → ``Config(<not initialized>)``（repr 永不抛异常）
    - 字段没有值 → ``<missing>``
    - 取值 / 转换失败 → ``<error: ...>``
    - 值用 ``!r``（和 ``dataclass`` 一致；``bool`` / ``None`` 才看得出来）
    """

    def __repr__(self) -> str:
        try:
            config = _get_configue()
        except ConfigMissingError:
            return f"{cls.__qualname__}(<not initialized>)"

        info = _CLASSES.get(type(self)) or _CLASSES.get(cls)
        if info is None:
            return f"{cls.__qualname__}(<unregistered>)"

        parts: list[str] = []
        for field_info in info.fields:
            section, key = field_info.path[0], field_info.path[-1]
            if (section, key) not in config.resolver:
                parts.append(f"{key}=<missing>")
                continue
            try:
                value = config.resolver.get_typed(section, key)
            except Exception as exc:  # noqa: BLE001  repr 不能因为坏值炸掉
                parts.append(f"{key}=<error: {exc}>")
                continue
            parts.append(f"{key}={value!r}")

        return f"{cls.__qualname__}({', '.join(parts)})"

    __repr__._configue_generated = True  # type: ignore[attr-defined]
    return __repr__


def _same_converter(current: Any, wanted: Any) -> bool:
    """两个转换器是否等价。

    ``is`` 不够：``list[str]`` 这类泛型别名每次求值都是**新对象**，``is``
    永远为假，会让每次装饰都触发一次无意义的 ``register_type``（连带清缓存）。
    所以先比 ``is`` 再比 ``==``，比较本身抛异常就当作「不同」（宁可多注册一次，
    不能漏注册）。
    """
    if current is wanted:
        return True
    try:
        return bool(current == wanted)
    except Exception:  # noqa: BLE001  特殊形式比较不出来
        return False


def _inject_defaults_for_late_class(fields: list[FieldInfo]) -> None:
    """``configclass`` 在 ``Configue`` 已构造时，把字段的类型/默认值立即注入
    Resolver，覆盖「晚于 ``load()`` 才注册的类」这一场景。

    ``load()`` 的 Step 4 只遍历**当时**的 ``_CLASSES``；插件 / UI 模块通常在
    ``load()`` 之后才被 import，装饰时若不做点什么，字段的默认值永远到不了
    Resolver。

    - ``Configue._instance`` 不存在：什么也不做，``load()`` 会处理；
    - 已存在值（文件里写了 / 运行期 ``set`` 过）：``overwrite=False``
      不覆盖；
    - 类型相同：不重复 ``register_type``，避免无意义的缓存失效。
    """
    try:
        from .configue import Configue
    except ImportError:  # pragma: no cover  循环 import 未就绪（极罕见）
        return
    inst = getattr(Configue, "_instance", None)
    if inst is None:
        return

    resolver = inst.resolver
    for f in fields:
        section, key = f.path[0], f.path[-1]
        if not _same_converter(resolver.converter_of(section, key), f.type):
            resolver.register_type(section, key, f.type)
        # overwrite=False：文件值 / 运行期 set 过的值优先。没有 origin 的
        # 默认值与 load() 的 Step 4 注入完全一致（flush 靠 _FIELD_OWNER 回查）。
        if f.has_default and (section, key) not in resolver:
            resolver.inject_raw(section, key, f.default)

