#
"""命令层：``@command`` 注册表 + 跨线程 ``Dispatcher``。

分两层，各自只做一件事：

* **注册表**（``@command``）—— 声明「进程外能调什么」。
  与 ``@declare`` / ``@register_ui`` 同一套声明式风格：装饰器即注册，
  不需要维护任何中心列表。
* **Dispatcher** —— 把调用搬到主循环线程执行。

为什么必须回主循环线程
----------------------
``services/`` 里的 ``SourceService.state``、各 ``ObservableNamespace``、
以及 UI 的 Qt model 都有单线程属主约定（见 ``services/__init__`` 的注释：
"绝不能在非属主线程写 state"）。IPC 的连接线程是**别人**的线程，
直接在其中调服务就是踩这条约定。因此一律 ``MainLoop.call_soon`` 排队，
执行完再把结果交回连接线程。

命令签名
--------
::

    @command("source.list")
    def _(container, *, limit: int = 20) -> dict: ...

* 第一个位置参数固定是容器，由 Dispatcher 注入；
* 其余参数**只能**来自 ``params``（keyword-only 是好习惯，不是强制）；
* 返回值必须是 JSON 可编码的（``common.ipc.encode_frame`` 有兜底，
  但别依赖它把关键数据喂进去）。

长任务约定
----------
命令**不得**在调用线程里同步跑长任务（会卡住 GUI 主线程）。
正确做法是丢给 ``task_service`` 并立刻返回一个句柄，见 ``thumbnail.scan``。
"""

from __future__ import annotations

import concurrent.futures as _futures
import dataclasses
import inspect
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from albuswall.common.ipc import (
    DEFAULT_CALL_TIMEOUT,
    CallTimeout,
    InvalidParams,
    MethodNotFound,
    Unavailable,
)
from albuswall.log import getLogger

if TYPE_CHECKING:
    from albuswall.core import Container
    from albuswall.core.main_loop import MainLoop

__all__ = [
    "Dispatcher",
    "command",
    "list_commands",
    "register_user_settings",
    "resolve",
]

_logger = getLogger(__name__)

#: 命令处理器：``fn(container, **params) -> json-able``
Handler = Callable[..., Any]

_handlers: dict[str, Handler] = {}


# --------------------------------------------------------------------------- #
# 注册
# --------------------------------------------------------------------------- #
def command(name: str) -> Callable[[Handler], Handler]:
    """声明一个可被进程外调用的命令。

    重复注册直接抛异常（与 ``PluginManager.registry`` 一致）：
    静默覆盖会让「谁赢了」变成 import 顺序的副产品。
    """
    if not name or name.startswith("."):
        raise ValueError(f"invalid command name: {name!r}")

    def deco(fn: Handler) -> Handler:
        if name in _handlers:
            raise ValueError(f"duplicate command: {name!r}")
        _handlers[name] = fn
        return fn

    return deco


def list_commands() -> list[str]:
    return sorted(_handlers)


def resolve(name: str) -> Handler | None:
    return _handlers.get(name)


# --------------------------------------------------------------------------- #
# 分发
# --------------------------------------------------------------------------- #
class Dispatcher:
    """把命令调用搬到主循环线程，同步等结果。

    ``loop`` 通过 :meth:`bind` 后置绑定，因为容器在 ``boot`` 阶段就被
    填充，而主循环要等到 ``on_loop`` 才拿得到。
    """

    def __init__(self, container: Container) -> None:
        self._container = container
        self._loop: MainLoop | None = None
        self._closed = False

    # ---------------- 绑定 ----------------
    def bind(self, loop: MainLoop) -> None:
        call_soon = getattr(loop, "call_soon", None)
        if callable(call_soon) is False:
            raise RuntimeError(
                f"{type(loop).__name__} has no call_soon(); "
                f"cross-thread command dispatch is unavailable"
            )
        self._loop = loop

    @property
    def bound(self) -> bool:
        return self._loop is not None and not self._closed

    def close(self) -> None:
        """停止接受新调用（在 on_final 里先调用）。

        已经在主线程排队但还没执行的调用会看到 cancelled 的 Future 并放弃，
        不会在拆机过程中再去碰已关闭的服务。
        """
        self._closed = True

    # ---------------- 调用 ----------------
    def call(
            self,
            method: str,
            params: Mapping[str, Any] | None = None,
            *,
            timeout: float | None = DEFAULT_CALL_TIMEOUT,
    ) -> Any:
        """执行一个命令并返回结果。

        :raises Unavailable: 已关闭 / 尚未绑定主循环
        :raises MethodNotFound: 没有该命令
        :raises InvalidParams: params 与签名不匹配
        :raises CallTimeout: 主循环在 ``timeout`` 内没有给出响应
        """
        if self._closed:
            raise Unavailable("command dispatcher is shutting down")
        loop = self._loop
        if loop is None:
            raise Unavailable("command dispatcher is not bound to a main loop")

        handler = _handlers.get(method)
        if handler is None:
            raise MethodNotFound(f"method not found: {method!r}")

        payload = dict(params or {})
        # 签名预检放在这里而不是主线程里：非法参数不必占用主循环一个回合，
        # 也能给出比 TypeError 更明确的信息。
        self._check_params(method, handler, payload)

        result: _futures.Future[Any] = _futures.Future()

        def _run() -> None:
            if result.cancelled():
                return
            try:
                result.set_result(handler(self._container, **payload))
            except BaseException as exc:
                # 非预期异常在服务端留一份完整栈：调用方只拿到 message，
                # 排查还得靠应用自己的日志。
                _logger.exception("command %r failed", method)
                if not result.cancelled():
                    result.set_exception(exc)

        try:
            loop.call_soon(_run)
        except Exception as exc:  # noinspection PyBroadException
            raise Unavailable(f"cannot reach main loop: {exc}") from exc

        try:
            return result.result(timeout=timeout)
        except _futures.TimeoutError as exc:
            result.cancel()
            raise CallTimeout(
                f"no response for {method!r} within {timeout}s; "
                f"the main loop may be blocked"
            ) from exc

    @staticmethod
    def _check_params(
            method: str,
            handler: Handler,
            params: Mapping[str, Any],
    ) -> None:
        try:
            signature = inspect.signature(handler)
        except (TypeError, ValueError):
            return
        try:
            signature.bind(None, **params)
        except TypeError as exc:
            raise InvalidParams(f"invalid params for {method!r}: {exc}") from exc


# --------------------------------------------------------------------------- #
# 内置命令
# --------------------------------------------------------------------------- #
# 说明：这里的 handler 只做「转发 + 序列化」，不含业务逻辑。
# 业务永远留在 services 层，命令层改起来才不需要回归业务测试。


def _jsonable(value: Any) -> Any:
    """DTO → dict 的宽容转换；失败时退回 ``repr``，不中断调用。"""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in value]
    return value


@command("app.ping")
def _app_ping(container: Container) -> dict:
    """存活探测：CLI 用它判断「应用在跑且能响应」。"""
    from albuswall.__about__ import __title__, __version__

    return {
        "ok": True,
        "name": __title__,
        "version": __version__,
        "pid": os.getpid(),
    }


@command("command.list")
def _command_list(container: Container) -> dict:
    """自省：CLI 的自动补全 / 帮助都基于它。"""
    return {
        "commands": list_commands(),
        "descriptions": describe_handlers(),
    }


@command("setting.schema")
def _setting_schema(container: Container, *, view: str = "tree") -> Any:
    """Dump 已登记的配置结构分组。

    view:
      - ``tree``（默认）：完整嵌套 dict，含 ``groups`` / ``items`` /
        ``by_file`` / ``unbound`` / ``summary``
      - ``basic``：只要常规分组（丢掉 ``advanced`` 子树），给「简单设置」页
      - ``by_file``：只按归属文件分组
      - ``flat``：扁平叶子列表
      - ``text``：树形文本，适合终端直接看

    登记表来自 ``albuswall.configue.setting_schema``，内容由
    :func:`register_user_settings` 提供；本命令只做转发，不参与任何业务
    逻辑。
    """
    from albuswall.configue.setting_schema import (
        dump_setting_schema,
        dump_setting_schema_by_file,
        format_setting_schema_dump,
    )

    register_user_settings()

    if view == "tree":
        return dump_setting_schema()
    if view == "basic":
        dump = dump_setting_schema()
        dump["groups"] = [
            group for group in dump["groups"] if group["id"] != _ADVANCED_GROUP_ID
        ]
        dump["items"] = [
            item for item in dump["items"]
            if not item["group"].startswith(f"{_ADVANCED_GROUP_ID}.")
        ]
        dump["summary"] = {
            **dump["summary"],
            "group_count": _count_groups(dump["groups"]),
            "item_count": len(dump["items"]),
        }
        # by_file / unbound 也按过滤后的叶子重算，别留 advanced 的残留
        by_file: dict[str, list[str]] = {}
        unbound: list[str] = []
        for item in dump["items"]:
            target = item["target_file"]
            if target is None:
                unbound.append(item["key"])
            else:
                by_file.setdefault(target, []).append(item["key"])
        dump["by_file"] = by_file
        dump["unbound"] = unbound
        dump["summary"]["file_count"] = len(by_file)
        return dump
    if view == "by_file":
        return dump_setting_schema_by_file()
    if view == "flat":
        return dump_setting_schema()["items"]
    if view == "text":
        return format_setting_schema_dump()
    raise ValueError(
        f"unknown view {view!r}; expected tree / basic / by_file / flat / text"
    )


@command("setting.set")
def _setting_set(
        container: Container,
        *,
        values: Mapping[str, Any],
        group: str | None = None,
        dry_run: bool = False,
        strict: bool = False,
) -> dict:
    """修改设置并落盘（改动立即写入各自的归属文件）。

    params:
      - ``values``：``{键: 值}``。键可以是 ``group.item``（如
        ``thumbnails.batch_size``）、``[section]key``（如
        ``[thumbnail]batch_size``），或在本次范围内唯一的叶子名；
        值可以给字符串（按声明类型转换，如 ``"64"`` / ``"false"``）或已类型化
        的值（``64`` / ``false``）。
      - ``group``：只在某个分组内生效（如 ``thumbnails``）；不给则对整张表。
      - ``dry_run``：只校验 + 转换 + 预览，不写内存也不落盘；
        返回的 ``changes`` 里有旧值 / 新值 / 将写入的 INI 文本。
      - ``strict``：写盘失败时直接报错，而不是保留 dirty 继续。

    返回 ``SettingWriteResult.to_dict()`` 再加上 ``pending`` / ``ok``
    （本次调用结束后仍没落盘的节点）。写盘失败不会静默：``ok=false`` 且
    携带 ``error``。
    """
    if not values:
        raise InvalidParams(
            "setting.set requires a non-empty 'values' object, "
            'e.g. {"thumbnails.batch_size": 64}'
        )
    if not isinstance(values, Mapping):
        raise InvalidParams("'values' must be an object mapping key -> value")

    from albuswall.configue.declaration import get_config
    from albuswall.configue.setting_schema import (
        save_setting_group,
        save_setting_schema,
    )

    register_user_settings()
    config = get_config()
    before = frozenset(config.resolver.dirty_nodes())

    try:
        if group is None:
            result = save_setting_schema(
                values, config=config, strict=strict, dry_run=dry_run,
            )
        else:
            result = save_setting_group(
                group, values, config=config, strict=strict,
                dry_run=dry_run,
            )
    except (KeyError, TypeError, ValueError) as exc:
        # 参数级问题（分组不存在 / 值转不过去 / 无处可写）→ 调用方的错
        raise InvalidParams(str(exc)) from exc

    _apply_language_change(result)
    return _dirty_report(config, result.to_dict(), before)


def _apply_language_change(result) -> None:
    """``appearance.language``（``[ui] language``）被改掉时，立刻切语言。

    命令行改配置（``albusctl setting.set``）也要能动态生效，所以旁路动作
    放在命令层而不是设置页里。翻译器属于 GUI：没装 PySide6 / 没有
    ``QApplication`` 时静默跳过（``install`` 自己会判断）。
    """
    applied = tuple(getattr(result, "applied", ()) or ())
    if not any(str(key).endswith("language") for key in applied):
        return

    from albuswall.configue.declaration import get_config

    try:
        language = get_config().resolver.get_typed("ui", "language")
    except Exception:  # noqa: BLE001  没有这个节点就当没改
        return

    try:
        from albuswall.ui.builtin.i18n import translation_manager

        translation_manager().install(str(language))
    except Exception:  # noqa: BLE001  headless / 无 Qt 环境：无需重设文案
        _logger.debug("translation manager unavailable", exc_info=True)


@command("setting.save")
def _setting_save(
        container: Container,
        *,
        dry_run: bool = False,
        strict: bool = False,
) -> dict:
    """把当前已标脏的配置节点落盘。

    ``setting.set`` 自己会落盘；本命令用于「别的代码路径改了值、又想立刻
    持久化」的场景（例如动态赋值，或 ``autosave=manual`` 策略下）。

    params:
      - ``dry_run``：只列出待写节点，不写盘。
      - ``strict``：写盘失败时直接报错。
    """
    from albuswall.configue.declaration import get_config

    config = get_config()
    pending = sorted(config.resolver.dirty_nodes())

    if dry_run:
        return {
            "dry_run": True,
            "written": [],
            "pending": [f"[{s}]{k}" for s, k in pending],
            "ok": not pending,
        }

    written = config.flush(strict=strict)
    remaining = sorted(config.resolver.dirty_nodes())
    payload: dict[str, Any] = {
        "dry_run": False,
        "written": [str(path) for path in written],
        "pending": [f"[{s}]{k}" for s, k in remaining],
        "ok": not remaining,
    }
    if remaining and config.last_flush_error is not None:
        payload["error"] = str(config.last_flush_error)
    return payload


def _dirty_report(
        config: Any,
        payload: dict[str, Any],
        before: frozenset,
) -> dict[str, Any]:
    """给 ``setting.set`` 的返回值补上「本次还有哪些没落盘」。"""
    leftover = sorted(frozenset(config.resolver.dirty_nodes()) - before)
    payload["pending"] = [f"[{s}]{k}" for s, k in leftover]
    payload["ok"] = not leftover
    if leftover and config.last_flush_error is not None:
        payload["error"] = str(config.last_flush_error)
    return payload


@command("source.list")
def _source_list(container: Container) -> dict:
    """当前被调度跟踪的导入源快照。"""
    service = container.get("source_service")
    sources = service.sources
    return {
        "count": len(sources),
        "sources": [_jsonable(src) for src in sources.values()],
    }


@command("import.trigger")
def _import_trigger(container: Container) -> dict:
    """唤醒导入 worker 处理候选（非阻塞）。"""
    container.get("import_service").trigger()
    return {"accepted": True}


@command("thumbnail.scan")
def _thumbnail_scan(container: Container) -> dict:
    """提交一批缺失缩略图的补齐任务（非阻塞），返回本次提交数。"""
    submitted = container.get("thumbnail_service").scan_and_submit()
    return {"submitted": submitted}


@command("task.stats")
def _task_stats(container: Container) -> dict:
    """任务服务的背压 / 计数快照。"""
    stats = container.get("task_service").get_stats()
    return {"stats": _jsonable(stats)}


@command("app.quit")
def _app_quit(container: Container, *, reason: str = "ipc") -> dict:
    """请求优雅退出。

    经由主线程执行是有意的：``request_quit`` 会跑所有 ``on_quit`` 钩子，
    它们由 UI / 服务注册，未必线程安全。
    """
    from albuswall.core import Application

    Application.request_quit(reason=reason)
    return {"quitting": True}


def describe_handlers() -> dict[str, str]:
    """命令 → 首行 docstring，供 ``albusctl list`` 展示。"""
    described: dict[str, str] = {}
    for name in list_commands():
        doc = inspect.getdoc(_handlers[name]) or ""
        described[name] = doc.splitlines()[0] if doc else ""
    return described


# --------------------------------------------------------------------------- #
# 用户设置树（`setting.schema` 的数据源）
# --------------------------------------------------------------------------- #
# 这一节回答的是「用户能改什么、怎么展示、写回哪个文件」，不读也不写任何
# 配置值：取值 / 归属文件在 dump / save 时才解析（见 setting_schema）。
#
# 分组约定（前端只需认这两类）：
#
# * 常规分组（appearance / thumbnails / import / trash）—— 用户日常会改的，
#   label / description 必须能独立看懂，不放「调优」类参数；
# * ``advanced``（``order=90``）—— 路径、文件名、服务调优、任务 / IPC 参数。
#   改错可能让应用行为异常，前端应把它折叠或收进「高级」页；``setting.schema``
#   的 ``basic`` 视图就是「tree 去掉 advanced」。
#
# 默认值一律用 ``SettingItem.from_field`` 从各模块的 ``ConfigField`` 继承，
# 这里不重复写任何默认值（唯一例外是 ``setup_static_config`` 注入的
# ``[path]`` 内建节点，它们没有 ConfigField 对象，直接复用同一批 helper）。

_ADVANCED_GROUP_ID = "advanced"
_SETTINGS_REGISTERED = False


def _count_groups(groups: list[dict[str, Any]]) -> int:
    """递归统计分组数（与 ``summary.group_count`` 的口径一致）。"""
    return sum(1 + _count_groups(group["groups"]) for group in groups)


def _path_defaults() -> dict[str, Path]:
    """``[path]`` 内建节点的默认值，与 ``setup_static_config`` 同源。"""
    from albuswall import __author__, __title__
    from albuswall.configue.utils import (
        get_cache_dir,
        get_temp_dir,
        get_user_config_dir,
        get_user_data_dir,
    )

    return {
        "config": get_user_config_dir(__title__, __author__),
        "data": get_user_data_dir(__title__, __author__),
        "cache": get_cache_dir(__title__, __author__),
        "temp": get_temp_dir(__title__),
    }


def _build_setting_groups() -> list[Any]:
    """组装设置树；任何声明模块 import 失败都由调用方兜住。"""
    from albuswall.configue.bootstrap import _ConfigueConfig
    from albuswall.configue.setting_schema import SettingGroup, setting_item
    from albuswall.infrastructure.database.bootstrap import Config as DbConfig
    from albuswall.infrastructure.ipc.config import Config as IpcConfig
    from albuswall.infrastructure.task.bootstrap import Config as TaskConfig
    from albuswall.log.bootstrap import LogConf
    from albuswall.services.import_ import ImportConfig
    from albuswall.services.source_trash.bootstrap import _Config as TrashConfig
    from albuswall.services.thumbnail.config import ThumbnailConfig
    from albuswall.ui.bootstrap import UIConfig
    from albuswall.ui.builtin.config.static import (
        AlbumPresenterConfs,
        WindowPresenterConfs,
    )
    # 纯数据注册表：不 import PySide6，headless 安装也能登记设置树
    from albuswall.ui.languages import LANGUAGES

    # ── 常规：外观 ────────────────────────────────
    # 这一组的顺序本身就是「注册」的一部分：设置页按 order 把叶子与子分组
    # 混排（见 albuswall.ui.builtin.setting.layout），所以这里给出的
    # order 就是界面上的上下顺序：
    #   10 主题选择 → 15 界面语言 → 20 窗口标题 →
    #   30 主题详情（样式表 / 图标 / 目录）→ 40 缺失补全
    appearance = SettingGroup(
        "appearance", "Appearance", order=10,
        description="Theme selection, interface language, window title, theme "
                    "files and missing-file completion.",
    ).add(
        setting_item(
            "theme", UIConfig.theme, label="Theme", order=10,
            description="Theme name; also selects resources/themes/<name> "
                        "as the source directory.",
        ),
        setting_item(
            "language", UIConfig.language, label="Language",
            widget="choice", order=15,
            choices=tuple(language.code for language in LANGUAGES),
            choice_labels={
                language.code: language.native_name for language in LANGUAGES
            },
            description="Interface language. Switching applies immediately; "
                        "texts without a translation keep their source "
                        "wording.",
        ),
        setting_item(
            "window_title", AlbumPresenterConfs.window_title,
            label="Window title", order=20,
            description="Overrides the window title. Empty = follow the "
                        "album name.",
        ),
        SettingGroup(
            "theme_details", "Theme details", order=30,
            description="Files and directories of the selected theme. They "
                        "are filled in automatically when a theme is picked "
                        "above, so they rarely need a manual edit.",
        ).add(
            setting_item(
                "style_sheet", WindowPresenterConfs.qss, label="Stylesheet",
                widget="file", order=10,
                description="QSS file applied to the window. Empty = completed "
                            "from the bundled theme.",
            ),
            setting_item(
                "icon_dir", WindowPresenterConfs.icon_dir,
                label="Icon directory", widget="dir", order=20,
                description="Absolute icon directory. Empty = derived from the "
                            "stylesheet's parent.",
            ),
            setting_item(
                "theme_path", UIConfig.theme_path, label="Theme directory",
                widget="dir", order=30,
                description="Root directory for themes.",
            ),
        ),
        SettingGroup(
            "theme_completion", "Missing completion", order=40,
            description="Copy missing QSS / icons from the bundled theme "
                        "into the user theme directory.",
        ).add(
            setting_item(
                "auto_complete", WindowPresenterConfs.auto_complete,
                label="Complete missing theme files", order=10,
                description="Master switch; false = leave the theme alone.",
            ),
            setting_item(
                "auto_complete_qss", WindowPresenterConfs.auto_complete_qss,
                label="Copy missing stylesheet", order=20,
            ),
            setting_item(
                "auto_complete_icons", WindowPresenterConfs.auto_complete_icons,
                label="Copy missing icons", order=30,
            ),
            setting_item(
                "auto_complete_only_once",
                WindowPresenterConfs.auto_complete_only_once,
                label="Only once", order=40,
                description="true = write a marker into the theme dir and "
                            "never complete again; delete "
                            ".albuswall_theme_initialized to re-arm.",
            ),
        ),
    )


    # ── 常规：缩略图 ──────────────────────────────
    thumbnails = SettingGroup(
        "thumbnails", "Thumbnails", order=20,
        description="Where preview images are generated and cached.",
    ).add(
        setting_item(
            "xdg_enabled", ThumbnailConfig.xdg_enabled,
            label="Share thumbnails with the desktop",
            description="true = write into $XDG_CACHE_HOME/thumbnails so "
                        "other freedesktop apps reuse them; false = keep them "
                        "in albuswall's own cache.",
        ),
        setting_item(
            "thumb_root", ThumbnailConfig.thumb_root,
            label="Thumbnail cache directory", widget="dir",
            description="Used when sharing with the desktop is off. "
                        "~ is expanded.",
        ),
        setting_item(
            "batch_size", ThumbnailConfig.batch_size,
            label="Thumbnails per batch",
            minimum=1, maximum=512, step=1,
        ),
    )

    # ── 常规：导入 ────────────────────────────────
    import_group = SettingGroup(
        "import", "Import", order=30,
        description="How aggressively new files are picked up.",
    ).add(
        setting_item(
            "batch_size", ImportConfig.batch_size,
            label="Candidates per round",
            minimum=1, maximum=5000, step=1,
            description="Lower it if importing makes the UI stutter; raise it "
                        "to drain a large library faster.",
        ),
    )

    # ── 常规：回收站 / 清理 ───────────────────────
    trash = SettingGroup(
        "trash", "Trash & cleanup", order=40,
        description="Removing thumbnails that no longer belong to any file.",
    ).add(
        setting_item(
            "enabled", TrashConfig.xdg_compat,
            label="Clean orphaned thumbnails", widget="choice",
            choices=("true", "false"), value_type=bool,
            description="true / false forces cleanup on or off; leave the "
                        "value empty (default) to let the platform decide.",
        ),
        setting_item(
            "auto_cleanup_on_dirty", TrashConfig.auto_cleanup_on_dirty,
            label="Clean up after an unclean shutdown",
            description="On boot, purge orphan thumbnails if the last run "
                        "did not shut down cleanly.",
        ),
        setting_item(
            "prune_empty_dirs", TrashConfig.prune_empty_dirs,
            label="Remove empty cache directories",
            description="Trim now-empty spec directories after deleting "
                        "thumbnails.",
        ),
    )

    # ── 高级：路径 ────────────────────────────────
    path_defaults = _path_defaults()
    paths = SettingGroup(
        "paths", "Paths", order=10,
        description="Where albuswall keeps its files. Changes take effect on "
                    "the next start.",
    ).add(
        *(
            setting_item(
                name, "path", name, label=label, widget="dir",
                default=path_defaults[name], value_type=Path,
            )
            for name, label in (
                ("config", "Config directory"),
                ("data", "Data directory"),
                ("cache", "Cache directory"),
                ("temp", "Temp directory"),
            )
        ),
    )

    # ── 高级：文件名 ──────────────────────────────
    files = SettingGroup(
        "files", "File names", order=20,
        description="Relative to the directory above.",
    ).add(
        setting_item(
            "qss", WindowPresenterConfs.qss_file_name,
            label="Stylesheet file name",
        ),
        setting_item(
            "preference", WindowPresenterConfs.preference_file_name,
            label="Preference file name",
        ),
        setting_item(
            "window_state", WindowPresenterConfs.window_state_file_name,
            label="Window state file name",
        ),
        setting_item("db", DbConfig.db_file, label="Database file name"),
        setting_item(
            "log", LogConf.log_file, label="Log config file name",
        ),
    )

    # ── 高级：运行时开关 ──────────────────────────
    runtime = SettingGroup(
        "runtime", "Runtime", order=30,
        description="Low-level switches; wrong values can break startup.",
    ).add(
        setting_item(
            "debug", "__default__", "debug", label="Debug mode",
            default=False, value_type=bool,
            description="Print extra diagnostics on start. Does not change "
                        "the log level.",
        ),
        setting_item(
            "ui", UIConfig.active, label="UI implementation",
            description="Name of the UI plugin to load (restart required).",
        ),
        setting_item(
            "no_builtin", UIConfig.no_builtin,
            label="Do not load bundled UI resources",
        ),
        setting_item(
            "disable_type_check", _ConfigueConfig.disable_type_check,
            label="Skip type check on load",
            description="Only useful when a hand-written value cannot be "
                        "converted and you want the app to start anyway.",
        ),
    )

    # ── 高级：日志 ────────────────────────────────
    logging_group = SettingGroup(
        "logging", "Logging", order=40,
    ).add(
        setting_item(
            "level", LogConf.level, label="Log level", widget="choice",
            value_type=str,
            choices=("TRACE", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
        ),
        setting_item(
            "config", LogConf.log_file_path, label="Log config path",
            widget="file",
            description="Empty = <config dir>/<files.log>; consumed by "
                        "logging.config.fileConfig.",
        ),
    )

    # ── 高级：缩略图调优 ──────────────────────────
    thumbnail_tuning = SettingGroup(
        "thumbnail_tuning", "Thumbnail tuning", order=50,
    ).add(
        setting_item(
            "failure_cooldown_sec", ThumbnailConfig.failure_cooldown_sec,
            label="Failure cooldown (s)", minimum=0, step=10,
            description="How long a permanently failed file is skipped "
                        "before it is tried again.",
        ),
        setting_item(
            "write_max_retries", ThumbnailConfig.write_max_retries,
            label="Write retries", minimum=0, maximum=10, step=1,
        ),
        setting_item(
            "write_retry_backoff_sec",
            ThumbnailConfig.write_retry_backoff_sec,
            label="Write retry backoff (s)", minimum=0, step=0.1,
        ),
        setting_item(
            "image_format", ThumbnailConfig.image_format,
            label="Image format", editable=False,
            description="Reserved: output is always PNG.",
        ),
        setting_item(
            "quality", ThumbnailConfig.quality, label="Quality",
            editable=False,
            description="Reserved: PNG output ignores this.",
        ),
        setting_item(
            "specs", ThumbnailConfig.specs, label="Spec sizes",
            widget="multiline",
            description='JSON object: spec name -> pixel size, e.g. '
                        '{"small": 128, "medium": 512, "large": 1024}. '
                        'Keys must be existing spec names.',
        ),
        setting_item(
            "xdg_spec_map", ThumbnailConfig.xdg_spec_map,
            label="XDG spec directory map", widget="multiline",
            description='JSON object: spec name -> share directory name '
                        '(normal / x-large / xx-large). Empty = built-in map.',
        ),
    )

    # ── 高级：导入调优 ────────────────────────────
    import_tuning = SettingGroup(
        "import_tuning", "Import tuning", order=60,
    ).add(
        setting_item(
            "process_timeout", ImportConfig.process_timeout,
            label="Per-file timeout (s)", minimum=1, step=30,
            description="The worker stops waiting after this; the child "
                        "process is not killed.",
        ),
        setting_item(
            "initial_backoff", ImportConfig.initial_backoff,
            label="Initial backoff (s)", minimum=0, step=0.5,
        ),
        setting_item(
            "max_backoff", ImportConfig.max_backoff,
            label="Max backoff (s)", minimum=0, step=5,
        ),
    )

    # ── 高级：回收站调优 ──────────────────────────
    trash_tuning = SettingGroup(
        "trash_tuning", "Trash tuning", order=70,
    ).add(
        setting_item(
            "reconciler_name", TrashConfig.reconciler_name,
            label="Reconciler name",
        ),
        setting_item(
            "priority", TrashConfig.priority, label="Scheduling priority",
            minimum=0, maximum=10, step=1,
        ),
        setting_item(
            "debounce", TrashConfig.debounce, label="Debounce (s)",
            minimum=0, step=0.5,
        ),
        setting_item(
            "orphan_min_age_seconds", TrashConfig.orphan_min_age_seconds,
            label="Orphan minimum age (s)", minimum=0, step=60,
            description="Protects thumbnails written moments ago.",
        ),
        setting_item(
            "session_marker_path", TrashConfig.session_marker_path,
            label="Session marker path", widget="file",
            description="Empty = <platform cache>/albuswall/.session.",
        ),
    )

    # ── 高级：任务服务 ────────────────────────────
    task_group = SettingGroup(
        "task", "Task service", order=80,
        description="Concurrency and back-pressure of the task pool.",
    ).add(
        setting_item(
            "initial_max_concurrency", TaskConfig.initial_max_concurrency,
            label="Initial concurrency", minimum=1, maximum=64, step=1,
        ),
        setting_item(
            "min_concurrency", TaskConfig.min_concurrency,
            label="Minimum concurrency", minimum=1, maximum=64, step=1,
        ),
        setting_item(
            "max_concurrency", TaskConfig.max_concurrency,
            label="Maximum concurrency", minimum=1, maximum=64, step=1,
            description="Empty = derive from the CPU count.",
        ),
        setting_item(
            "process_workers", TaskConfig.process_workers,
            label="Process workers", minimum=0, maximum=64, step=1,
            description="Empty = derive from the CPU count.",
        ),
        setting_item(
            "cpu_high_threshold", TaskConfig.cpu_high_threshold,
            label="CPU high watermark (%)", minimum=1, maximum=100, step=5,
        ),
        setting_item(
            "cpu_low_threshold", TaskConfig.cpu_low_threshold,
            label="CPU low watermark (%)", minimum=0, maximum=100, step=5,
        ),
        setting_item(
            "mem_high_threshold", TaskConfig.mem_high_threshold,
            label="Memory high watermark (%)", minimum=1, maximum=100, step=5,
        ),
        setting_item(
            "mem_low_threshold", TaskConfig.mem_low_threshold,
            label="Memory low watermark (%)", minimum=0, maximum=100, step=5,
        ),
        setting_item(
            "check_interval", TaskConfig.check_interval,
            label="Load check interval (s)", minimum=0.5, step=1,
        ),
        setting_item(
            "scheduler_threads", TaskConfig.scheduler_threads,
            label="Scheduler threads", minimum=1, maximum=16, step=1,
        ),
        setting_item(
            "max_queue_size", TaskConfig.max_queue_size,
            label="Max queued tasks", minimum=1, step=100,
        ),
        setting_item(
            "submit_timeout", TaskConfig.submit_timeout,
            label="Submit timeout (s)", minimum=0, step=1,
        ),
        setting_item(
            "low_load_streak", TaskConfig.low_load_streak,
            label="Low-load streak",
            minimum=1, maximum=60, step=1,
            description="Consecutive idle checks before concurrency drops.",
        ),
        setting_item(
            "wait_warn_threshold", TaskConfig.wait_warn_threshold,
            label="Wait warning threshold (s)", minimum=0, step=0.5,
        ),
    )

    # ── 高级：IPC ─────────────────────────────────
    ipc_group = SettingGroup(
        "ipc", "IPC", order=90,
        description="Cross-process control endpoint (albusctl).",
    ).add(
        setting_item(
            "enabled", IpcConfig.enabled,
            label="Enable the IPC endpoint",
        ),
        setting_item(
            "call_timeout", IpcConfig.call_timeout,
            label="Call timeout (s)", minimum=0.1, step=5,
        ),
        setting_item(
            "max_message_bytes", IpcConfig.max_message_bytes,
            label="Max message size (bytes)", minimum=1024, step=1024,
        ),
        setting_item(
            "max_connections", IpcConfig.max_connections,
            label="Max connections", minimum=1, maximum=64, step=1,
        ),
        setting_item(
            "event_bridge", IpcConfig.event_bridge,
            label="Bridge internal signals as events",
        ),
    )

    # ── 高级：数据库 ──────────────────────────────
    database = SettingGroup(
        "database", "Database", order=100,
    ).add(
        setting_item(
            "must_exist", DbConfig.must_exist,
            label="Fail if the database is missing",
            description="false = create it on first start.",
        ),
    )

    advanced = SettingGroup(
        _ADVANCED_GROUP_ID, "Advanced", order=90,
        description="Tuning knobs and paths. Wrong values can make the app "
                    "behave badly; fold this group behind an 'Advanced' page.",
    ).add(
        paths, files, runtime, logging_group, thumbnail_tuning,
        import_tuning, trash_tuning, task_group, ipc_group, database,
    )

    return [appearance, thumbnails, import_group, trash, advanced]


def register_user_settings() -> bool:
    """把「用户可编辑的设置」登记进 ``SETTING_SCHEMA``（幂等）。

    只登记「有哪些设置、怎么展示、写回哪个文件」，不读写任何配置值。
    返回是否登记成功（失败只记日志，不抛——设置树缺失不该拖垮 IPC）。

    前端约定：根分组 id 为 ``advanced`` 的那棵子树是「高级」，
    其余是用户日常设置；``setting.schema`` 的 ``basic`` 视图即
    「tree 去掉 advanced」。
    """
    global _SETTINGS_REGISTERED
    if _SETTINGS_REGISTERED:
        return True

    from albuswall.configue.setting_schema import register_setting_group

    try:
        groups = _build_setting_groups()
    except Exception:  # noqa: BLE001  声明模块缺失时降级为空设置树
        _logger.exception(
            "failed to build the user setting tree; "
            "albusctl settings will be empty"
        )
        return False

    for group in groups:
        register_setting_group(group, replace=True)
    _SETTINGS_REGISTERED = True
    _logger.debug(
        "user setting tree registered: %d root groups", len(groups),
    )
    return True


# 导入即登记（IPC 命令层被 import 时跑一次）；失败留给
# ``setting.schema`` 调用时重试，不影响其它命令。
register_user_settings()

