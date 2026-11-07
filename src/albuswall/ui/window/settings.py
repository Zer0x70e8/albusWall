#
"""界面层的**配置使用接口**：``[window]`` 状态声明 + 读写 + 最终更新入口。

这一层是「视图 / presenter」与「配置器（``albuswall.configue``）」之间的唯一
接触面。它只做三件事，**不 import 任何 Qt 类型**（视图层自己要碰 Qt，
但配置语义不依赖控件）：

1. **声明**：:class:`WindowState` 用 ``@configclass("window", "main")`` 把
   「每次启动要恢复的界面状态」声明进配置器，因此它会自动出现在
   ``albusctl setting schema`` 里，也有声明默认值兜底；
2. **读**：:func:`window_state` 返回一份冻结快照（:class:`StateSnapshot`），
   单个字段读失败只影响该字段（退回默认值），不会因为一个坏值让整窗起不来；
3. **写**：:func:`update_settings` 是 UI 侧**唯一**的配置更新入口（先全量
   校验、后统一落盘，语义与 ``setting.set`` 完全一致），
   :func:`store_window_state` 是它在「界面状态」上的便捷包装。

为什么界面状态也放在这里
========================

「窗口几何 / 上次的相册 / 网格列数」严格说不是**用户设置**，而是**每会话
状态**（用户不会去读它，只会被程序改写）。但两者的基础设施完全一样：一个
声明、一个归属文件、一次原子写回。先把它们放进同一个 ``[window]`` 段，
设置页将来要把「记住窗口位置」做成开关时，只需要在这里加一个 ``bool``
字段即可 —— 不必再造一套状态文件。

于是它天然继承配置器的三条特性：

* 写在 ``config.ini`` 的 ``[window]`` 段（与其它配置同一份文件、同一套
  ``autosave`` 策略，本项目是 ``atexit``）；
* 用户没写过的键 = 声明默认值（``ignore_missing`` 时连文件都没有也能跑）；
* 写入走 ``resolver.set(persist=True)``，类型由声明保证（``int`` / ``bool``），
  「``tile_columns = "abc"``」这种脏值在写入口就被拒绝。

快捷键信息（:func:`shortcut_config_info`）
==========================================

快捷键**不在** ``config.ini`` 里：它是一份独立的 ``shortcuts.ini``（随包默认
表 + 用户覆盖表，见 :mod:`albuswall.ui.keymap.config`）。但「设置系统」需要
把它和配置项放在同一处展示，所以本模块再提供一个只读视图，把「默认表 /
用户覆盖 / 生效键位 / 冲突」摊平成命令层与设置页都能直接吃的字典。

它**不修改**任何东西，也不 ``persist``：写快捷键是
:meth:`albuswall.ui.keymap.ShortcutRegistry.persist` 的事。

用法
====

presenter 侧（``albuswall.ui.presenter``）::

    from albuswall.ui.window.settings import StateSnapshot, window_state

    snapshot = window_state()                 # 读：任何字段坏了都用默认值
    rect = snapshot.rect()                    # ("x, y, w, h") → (x, y, w, h)

    # 写：一条或多条一起提交；返回值与 ``setting.set`` 同形
    report = update_settings({"ui.theme": "dark"}, strict=True)

命令层（``service/command/handlers/ui.py``）::

    info = shortcut_config_info(registry=container.get("shortcuts"))
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from albuswall.configue import configclass, field
from albuswall.configue.errors import ConfigError

__all__ = [
    "DEFAULT_TILE_COLUMNS",
    "OVERLAY_PAGES",
    "SHORTCUT_SECTION",
    "StateSnapshot",
    "WindowState",
    "format_geometry",
    "geometry_text",
    "parse_geometry",
    "shortcut_config_info",
    "store_window_state",
    "update_settings",
    "window_state",
]

logger = logging.getLogger(__package__)

#: 界面状态所在的段名。单独一段，不和 ``[ui]``（界面偏好）混。
SECTION = "window"

#: 状态所在的配置文件名（与其它内建段一样是主文件）。
FILE = "main"

#: ``tile_columns = 0`` 的含义：由视图按宽度自己换算列数（默认行为）。
DEFAULT_TILE_COLUMNS = 0

#: 可以恢复的浮层页（``overlay`` 字段的合法取值；空串 = 不开浮层）。
OVERLAY_PAGES: tuple[str, ...] = ("album", "source")

#: 快捷键表所在的段名（``shortcuts.ini`` 的 ``[shortcuts]``）。
SHORTCUT_SECTION = "shortcuts"


# ======================================================================
# 声明
# ======================================================================

# noinspection bad-assignment
@configclass(SECTION, FILE)
class WindowState:
    """``[window]`` 段：跨会话保留的界面状态。

    字段全部带默认值，因此「配置文件里没有这一段」也是合法状态（首次启动 /
    ``ignore_missing=True``）：:func:`window_state` 会读出一份全默认的快照。

    类型选择的两个约定：

    * ``geometry`` 用 ``"x,y,w,h"`` 字符串而不是四个 ``int``：Qt 的
      ``QRect`` 一行就 ``str`` 化得回来，而四个字段会让设置树里多出四条
      相互依赖的叶子；
    * ``tile_columns`` 用 ``0`` 表示「不指定」，避免把 0 列当成一个合法值。
    """

    geometry: str = field(
        default="",
        label="窗口几何",
        description="最近一次的窗口位置与大小，写成 x,y,w,h（留空 = 由窗口管理器决定）",
    )
    maximized: bool = field(
        default=False,
        label="最大化",
        description="下次启动是否以最大化打开",
    )
    fullscreen: bool = field(
        default=False,
        label="全屏",
        description="下次启动是否以全屏打开",
    )
    album_uuid: str = field(
        default="",
        label="上次的相册",
        description="启动时恢复的相册 uuid（留空 = 用默认相册）",
    )
    overlay: str = field(
        default="",
        label="浮层页",
        description="启动时恢复的浮层页：空 / album / source",
    )
    tile_columns: int = field(
        default=DEFAULT_TILE_COLUMNS,
        label="网格列数",
        description="内容网格的列数（0 = 按窗口宽度自动换算）",
    )


#: ``WindowState`` 的声明默认值（字段名 → 默认值），读取失败的兜底。
#:
#: 与 :class:`WindowState` 的字段声明**必须逐条一致**（类型变了这里也改）：
#: ``@configclass`` 的默认值住在 Resolver 里，配置不可用时读不到，本模块需要
#: 一份不依赖配置器的兜底。写成写死的字典也更方便与 ``conf.ini`` 的注释对照。
_DEFAULTS: dict[str, Any] = {
    "geometry": "",
    "maximized": False,
    "fullscreen": False,
    "album_uuid": "",
    "overlay": "",
    "tile_columns": DEFAULT_TILE_COLUMNS,
}


# ======================================================================
# 配置单例
# ======================================================================

def _configue() -> Any | None:
    """取 ``Configue`` 单例；未装配（单测 / 无配置环境）时返回 ``None``。"""
    from albuswall.configue import Configue

    return Configue._instance


def _require_configue() -> Any:
    """写配置必须要有单例：没有就是「这个进程根本没装配配置」。"""
    config = _configue()
    if config is None:
        raise ConfigError(
            "configuration is not set up in this process: "
            "nothing called setup_config(container) yet"
        )
    return config


# ======================================================================
# 读
# ======================================================================

@dataclass(frozen=True)
class StateSnapshot:
    """一次读出的界面状态（全部字段都有值，坏值退回声明默认值）。"""

    geometry: str = ""
    maximized: bool = False
    fullscreen: bool = False
    album_uuid: str = ""
    overlay: str = ""
    tile_columns: int = DEFAULT_TILE_COLUMNS

    # -- 派生视图 ---------------------------------------------------
    def rect(self) -> tuple[int, int, int, int] | None:
        """``geometry`` 解析成 ``(x, y, w, h)``；没存 / 坏值 → ``None``。"""
        return parse_geometry(self.geometry)

    @property
    def has_geometry(self) -> bool:
        """是否存过一个可用的窗口几何。"""
        return self.rect() is not None

    def to_dict(self) -> dict[str, Any]:
        """JSON 友好的形态（``ui.state --format=json`` 直接返回它）。"""
        return {
            "geometry": self.geometry,
            "rect": list(self.rect()) if self.has_geometry else None,
            "maximized": self.maximized,
            "fullscreen": self.fullscreen,
            "album_uuid": self.album_uuid,
            "overlay": self.overlay,
            "tile_columns": self.tile_columns,
        }

    @property
    def empty(self) -> bool:
        """是否与声明默认值完全一致（= 还没存过任何状态）。"""
        return self.to_dict() == StateSnapshot().to_dict()


def _read_field(name: str) -> Any:
    """读一个声明字段；读失败（缺值 / 类型转不过去）返回默认值。"""
    default = _DEFAULTS[name]
    try:
        value = getattr(WindowState(), name)
    except (ConfigError, AttributeError, KeyError, TypeError, ValueError) as exc:
        logger.debug(
            "window settings: cannot read [%s]%s (%s: %s); using default %r",
            SECTION, name, type(exc).__name__, exc, default,
        )
        return default
    if value is None:
        return default
    if isinstance(default, bool) and not isinstance(value, bool):
        return bool(value)
    if isinstance(default, int) and not isinstance(default, bool):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    return value if isinstance(value, type(default)) else str(value or "")


def window_state() -> StateSnapshot:
    """读出当前的界面状态快照。

    没有配置单例 / 文件里没有 ``[window]`` / 某个值非法 —— 一律退回声明
    默认值，调用方不必自己判断（界面启动路径不允许因为状态文件坏了而失败）。
    """
    values = {name: _read_field(name) for name in _DEFAULTS}
    overlay = str(values["overlay"] or "").strip().lower()
    values["overlay"] = overlay if overlay in OVERLAY_PAGES else ""
    return StateSnapshot(**values)


# ======================================================================
# 几何文本
# ======================================================================

def parse_geometry(value: Any) -> tuple[int, int, int, int] | None:
    """``"x,y,w,h"`` / ``[x, y, w, h]`` / ``QRect`` → ``(x, y, w, h)``。

    宽高必须为正，否则视为「没存过几何」（返回 ``None``）—— 一个 0×0 的
    窗口比不恢复更糟。

    Args:
        value: 配置值（``str``）、四元序列，或任何带 ``x()`` / ``y()`` /
            ``width()`` / ``height()`` 的对象（``QRect`` / ``QRectF``；本模块
            不 import Qt，按鸭子类型读）。
    """
    if value is None:
        return None
    if isinstance(value, (str, bytes)):
        text = (value.decode() if isinstance(value, bytes) else value).strip()
        if not text:
            return None
        parts = [part for part in text.replace(";", ",").split(",") if part.strip()]
        if len(parts) != 4:
            return None
        try:
            numbers = [int(float(part)) for part in parts]
        except (TypeError, ValueError):
            return None
    else:
        getters = ("x", "y", "width", "height")
        if all(callable(getattr(value, name, None)) for name in getters):
            numbers = [int(getattr(value, name)()) for name in getters]
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            if len(value) != 4:
                return None
            try:
                numbers = [int(item) for item in value]
            except (TypeError, ValueError):
                return None
        else:
            return None

    x, y, width, height = numbers
    if width <= 0 or height <= 0:
        return None
    return x, y, width, height


def format_geometry(rect: Any) -> str:
    """把 ``(x, y, w, h)`` / ``QRect`` 写成配置文件里的文本形态。

    非法 / 空值 → 空串（= 不恢复几何）。
    """
    parsed = parse_geometry(rect)
    if parsed is None:
        return ""
    return ",".join(str(item) for item in parsed)


def geometry_text(rect: Any) -> str:
    """:func:`format_geometry` 的别名（presenter 侧读起来更顺）。"""
    return format_geometry(rect)


# ======================================================================
# 写
# ======================================================================

def update_settings(
        values: Mapping[Any, Any],
        *,
        dry_run: bool = False,
        strict: bool = False,
        collect_errors: bool = False,
        flush: bool = True,
        config: Any = None,
) -> dict[str, Any]:
    """**最终配置更新接口**：校验 → 写内存 → 统一落盘，返回报告。

    等价于 ``albuswall.configue.schema.save_values``（两遍算法：先全量校验，
    后统一写；任何一个值非法则内存与磁盘都不动），只是把「取配置单例 + 报告
    收口」这两件重复活做掉了。UI 侧的所有写配置**都应当**经过这里，而不是
    各 presenter 自己 ``Config().field = value``：

    * 拼错的键会报错（``ValueError``），不会静默写进一个没人声明的节点；
    * 一次提交的多条值要么全生效、要么全不生效；
    * 落盘目标由配置器按「归属文件」推导，UI 不拼路径。

    Args:
        values: ``{键: 值}``。键支持 ``"window.geometry"`` /
            ``("window", "geometry")`` / ``"[window]geometry"`` 三种写法
            （与 ``setting.set`` 同一套规则），值可以是已类型化的值或字符串。
        dry_run: 只校验 + 预览，不写内存也不写盘。
        strict: 写盘失败立即抛 ``ConfigError``；默认保留 dirty 节点。
        collect_errors: 一次性收集**全部**字段校验错误（不抛第一个），
            有错误时整体不写。设置页的「保存」走这个模式。
        flush: ``False`` 时只写内存、不落盘（``deferred=True``）；设置页的
            「存为草稿」走这个模式，之后由重试 / ``flush`` 再落盘。
        config: 显式 ``Configue``（默认取进程单例；测试可注入）。

    Returns:
        与 ``setting.set`` 同形的报告：``written`` / ``by_file`` / ``flushed``
        / ``applied`` / ``changes`` / ``dry_run`` / ``validation_errors`` /
        ``deferred``，另加三项 UI 关心的字段：

        * ``pending`` —— **本次提交的节点里**没落盘的（``[section]key``）：
          按本次碰过的节点判定，不是 ``dirty_nodes()`` 的前后差集（差集会
          漏掉「上次就脏、这次又写失败」的节点，把失败报成成功）；
        * ``ok`` —— ``pending`` 为空且没有校验错误即「已全部落盘」；
        * ``error`` —— 仍有残留时的 ``last_flush_error``。

    Raises:
        ConfigError: 进程没有配置单例 / ``strict=True`` 且写盘失败。
        ValueError: 键未声明 / 值转不过去（或类型与声明不符）/ 叶子是固定项
            （``editable=False``）/ 叶子没有归属文件。
    """
    from albuswall.configue.schema import save_values

    cfg = config if config is not None else _require_configue()

    result = save_values(
        dict(values), config=cfg, strict=strict, dry_run=dry_run,
        collect_errors=collect_errors, flush=flush,
    )
    report = result.to_dict()

    errors = report.get("validation_errors") or []
    if errors:
        # 有校验错误时整体不写：内存 / 磁盘 / 脏标记都不动，pending 也就
        # 不能拿「碰过的节点」去和 dirty 集合求交（那会误报上次遗留的脏）。
        report["pending"] = []
        report["ok"] = False
        report["error"] = "validation failed"
        return report
    if report.get("dry_run"):
        report["pending"] = []
        report["ok"] = True
        report["error"] = None
        return report

    # 「本次还有哪些没落盘」必须按**本次碰过的节点**判定，不能用
    # ``dirty_nodes()`` 的前后差集：本次之前就脏着的节点（上次 flush 失败
    # 留下的）不在差集里，于是「这次又写失败了」会被报成成功。
    report["pending"] = _pending_nodes(cfg, result.nodes)
    report["ok"] = not report["pending"]
    error = getattr(cfg, "last_flush_error", None)
    report["error"] = (
        str(error) if report["pending"] and error is not None else None
    )
    return report


def _pending_nodes(cfg: Any, nodes: Any) -> list[str]:
    """``nodes`` 里仍未落盘的节点（``[section]key`` 文本，已排序）。

    ``dirty_nodes()`` 拿不到（半初始化 / 已 close）时按「全都没落盘」处理：
    「不确定」不能报成「成功」。
    """
    touched = {(str(section), str(key)) for section, key in (nodes or ())}
    try:
        dirty = {tuple(node) for node in cfg.dirty_nodes()}
    except Exception:  # noqa: BLE001 - 拿不到就往坏处报
        logger.debug("window settings: dirty_nodes() unavailable")
        dirty = touched
    return [f"[{section}]{key}" for section, key in sorted(touched & dirty)]


def store_window_state(**changes: Any) -> dict[str, Any]:
    """把若干界面状态字段写回 ``[window]``（``None`` 的字段表示「不改」）。

    用法::

        store_window_state(geometry="10,20,900,700", maximized=True,
                           album_uuid="...", overlay="album", tile_columns=4)

    只提交**传了值**的字段：presenter 各自负责自己那几条，不会互相覆盖。

    Raises:
        ValueError: 传了未声明的字段名。
        ConfigError: 进程没有配置单例。
    """
    values: dict[str, Any] = {}
    for name, value in changes.items():
        if name not in _DEFAULTS:
            raise ValueError(
                f"unknown window state field {name!r}; expected one of "
                f"{', '.join(sorted(_DEFAULTS))}"
            )
        if value is None:
            continue
        values[f"{SECTION}.{name}"] = _normalize_field(name, value)
    if not values:
        return {
            "written": [], "by_file": {}, "flushed": [], "applied": [],
            "changes": [], "dry_run": False,
            "pending": [], "ok": True, "error": None,
        }
    return update_settings(values)


def _normalize_field(name: str, value: Any) -> Any:
    """把界面状态的一个字段收口成配置值（几何接受 ``QRect`` / 四元序列）。"""
    if name == "geometry":
        return format_geometry(value)
    if name in ("maximized", "fullscreen"):
        return bool(value)
    if name == "tile_columns":
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return DEFAULT_TILE_COLUMNS
    if name == "overlay":
        text = str(value or "").strip().lower()
        return text if text in OVERLAY_PAGES else ""
    return str(value or "")


# ======================================================================
# 快捷键配置信息（只读）
# ======================================================================

def _binding_keys(binding: Any) -> list[str]:
    """``ShortcutBinding`` → 键位文本列表（也接受任意可迭代对象）。"""
    if binding is None:
        return []
    keys = getattr(binding, "keys", binding)
    if keys is None:
        return []
    try:
        return [str(key) for key in keys]
    except TypeError:
        return []


def _binding_args(binding: Any) -> list[Any]:
    args = getattr(binding, "args", ()) if binding is not None else ()
    try:
        return list(args or ())
    except TypeError:
        return []


def _binding_kwargs(binding: Any) -> dict[str, Any]:
    kwargs = getattr(binding, "kwargs", {}) if binding is not None else {}
    try:
        return dict(kwargs or {})
    except (TypeError, ValueError):
        return {}


def _registry_slot_map(registry: Any) -> dict[str, Any]:
    """``registry.slots()`` → ``{id: slot}``；取不到就空表。"""
    getter = getattr(registry, "slots", None)
    if not callable(getter):
        return {}
    try:
        return {str(getattr(slot, "id", "")): slot for slot in getter()}
    except Exception:  # noqa: BLE001 - 内省失败不该让命令失败
        logger.exception("window settings: cannot read shortcut slots")
        return {}


def _registry_keys(registry: Any, command: str) -> list[str] | None:
    """注册表里某命令的生效键位；没有注册表 / 读不出来 → ``None``。"""
    getter = getattr(registry, "keys_for", None)
    if not callable(getter):
        return None
    try:
        return [str(key) for key in getter(command)]
    except Exception:  # noqa: BLE001
        return None


def _invert(table: Mapping[str, Any]) -> dict[str, list[str]]:
    """``{command: binding}`` → ``{keys: [commands]}``（一键多命令的诊断用）。"""
    inverted: dict[str, list[str]] = {}
    for command, binding in table.items():
        for keys in _binding_keys(binding):
            bucket = inverted.setdefault(keys, [])
            if command not in bucket:
                bucket.append(command)
    return inverted


def _path_is_file(path: Any) -> bool:
    """``path`` 是否存在且是文件（接受 ``Path`` / ``str`` / ``None``）。"""
    if path is None:
        return False
    checker = getattr(path, "is_file", None)
    if not callable(checker):
        try:
            from pathlib import Path

            checker = Path(str(path)).is_file
        except (OSError, TypeError, ValueError):
            return False
    try:
        return bool(checker())
    except OSError:
        return False


def shortcut_config_info(
        registry: Any = None,
        *,
        config: Any = None,
) -> dict[str, Any]:
    """快捷键配置的只读快照（默认表 / 用户覆盖 / 生效键位 / 冲突）。

    数据来源两处，互相补位：

    * **配置文件**（``albuswall.ui.keymap.config.ShortcutConfig``）：随包
      默认表 + 用户覆盖表，任何人都能读 —— 不需要 GUI、不需要注册表；
    * **运行期注册表**（``container.get("shortcuts")``，可选）：提供 ``help``
      与「最终生效」的键位（配置表里没有的命令用槽的兜底键位）。

    Args:
        registry: ``ShortcutRegistry`` 或任意鸭子类型对象（可有可无）。
        config: 显式 ``ShortcutConfig``（测试注入用；默认按标准路径新建）。

    Returns:
        JSON 友好的字典::

            {
              "section": "shortcuts",
              "files": {"defaults": "...", "user": "...",
                        "defaults_exists": true, "user_exists": false},
              "commands": [{"command": "app.quit", "help": "Quit albusWall",
                            "default_keys": ["Ctrl+Q"], "effective_keys": [...],
                            "args": [], "kwargs": {}, "overridden": false,
                            "bound": true, "source": "default"}, ...],
              "conflicts": [{"keys": "Ctrl+W", "commands": [...]}, ...],
              "unbound": ["window.album", ...],
              "errors": [...]
            }

        任何一步读失败都只是把 ``errors`` 里加一条，不抛异常 —— 这个视图是
        「诊断 / 设置页」用的，坏文件不该让调用方连别的字段都拿不到。
    """
    errors: list[str] = []
    defaults_path: Any = None
    user_path: Any = None
    defaults: dict[str, Any] = {}
    user: dict[str, Any] = {}

    try:
        from albuswall.ui.keymap.config import ShortcutConfig

        shortcut_config = config if config is not None else ShortcutConfig()
        defaults_path = getattr(shortcut_config, "defaults_path", None)
        user_path = getattr(shortcut_config, "user_path", None)
        section = str(
            getattr(shortcut_config, "section", SHORTCUT_SECTION)
            or SHORTCUT_SECTION
        )
        try:
            defaults = dict(shortcut_config.load_defaults(strict=False) or {})
        except Exception as exc:  # noqa: BLE001
            errors.append(
                f"default table: {type(exc).__name__}: {exc}"
            )
        # ``strict=False`` 把「文件不存在」降级成 WARNING，这里再补一条显式的
        # 诊断：随包默认表缺失是打包事故，值得出现在设置页的诊断区里。
        if defaults_path is not None and not _path_is_file(defaults_path):
            errors.append(f"default table not found: {defaults_path}")
        try:
            user = dict(shortcut_config.load_user(strict=False) or {})
        except Exception as exc:  # noqa: BLE001
            errors.append(f"user table: {type(exc).__name__}: {exc}")
    except Exception as exc:  # noqa: BLE001 - 资源 / Qt 缺失
        section = SHORTCUT_SECTION
        errors.append(f"shortcut config: {type(exc).__name__}: {exc}")

    merged: dict[str, Any] = dict(defaults)
    merged.update(user)

    slots = _registry_slot_map(registry)
    runtime_commands: tuple[str, ...] = ()
    getter = getattr(registry, "commands", None)
    if callable(getter):
        try:
            runtime_commands = tuple(str(name) for name in getter())
        except Exception:  # noqa: BLE001
            runtime_commands = ()

    commands: list[dict[str, Any]] = []
    for command in sorted(set(merged) | set(slots) | set(runtime_commands)):
        binding = merged.get(command)
        slot = slots.get(command)
        configured = _binding_keys(binding)
        slot_defaults = [
            str(key) for key in (getattr(slot, "default_keys", ()) or ())
        ]
        default_keys = (
            _binding_keys(defaults.get(command)) if command in defaults
            else slot_defaults
        )
        effective = _registry_keys(registry, command)
        if effective is None:
            effective = configured or slot_defaults
        if command in user:
            source = "user"
        elif command in defaults:
            source = "default"
        elif slot is not None:
            source = "slot"
        else:
            source = "none"
        commands.append({
            "command": command,
            "help": str(getattr(slot, "help", "") or ""),
            "default_keys": default_keys,
            "configured_keys": configured,
            "effective_keys": effective,
            "args": _binding_args(binding),
            "kwargs": _binding_kwargs(binding),
            "overridden": command in user,
            "bound": bool(effective),
            "source": source,
        })

    conflicts: list[dict[str, Any]] = []
    conflict_getter = getattr(registry, "conflicts", None)
    shadow: Mapping[str, list[str]] | None = None
    if callable(conflict_getter):
        try:
            raw = conflict_getter() or {}
            shadow = {
                str(keys): [str(name) for name in names]
                for keys, names in raw.items()
                if len(names) > 1
            }
        except Exception:  # noqa: BLE001
            shadow = None
    if shadow is None:
        shadow = {
            keys: names for keys, names in _invert(merged).items()
            if len(names) > 1
        }
    for keys in sorted(shadow):
        conflicts.append({"keys": keys, "commands": shadow[keys]})

    unbound = [entry["command"] for entry in commands if not entry["bound"]]

    return {
        "section": section,
        "files": {
            "defaults": None if defaults_path is None else str(defaults_path),
            "user": None if user_path is None else str(user_path),
            "defaults_exists": _path_is_file(defaults_path),
            "user_exists": _path_is_file(user_path),
        },
        "commands": commands,
        "conflicts": conflicts,
        "unbound": unbound,
        "errors": errors,
    }
