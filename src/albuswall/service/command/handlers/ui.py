#
"""界面配置内省命令：``ui.shortcuts`` / ``ui.state``（都只读）。

为什么放在命令层
================

「设置系统」给普通用户看的第一屏，是「我能配什么、现在配的是什么」。配置项
本身已经有 ``setting.schema``，但界面还有两块配置不在 ``config.ini`` 里：

* **快捷键** —— 独立的 ``shortcuts.ini``（随包默认表 + 用户覆盖表，见
  :mod:`albuswall.ui.keymap.config`）；
* **界面状态** —— ``[window]`` 段（窗口几何 / 上次相册 / 网格列数），由
  presenter 在退出时回写。

两条命令都站在 :mod:`albuswall.ui.window.settings`（界面层的配置使用接口）
上，不自己拼路径、不自己读 INI：命令层只做「取数据 + 排版 + 收口格式」。

依赖纪律
========

keymap 注册表由 UI 启动流程经 ``registry_shortcut_registry`` 写进容器的
``"shortcuts"`` 键，晚于命令装配 —— 所以这里**每个命令体内懒取**，并把它
（可能为 ``None``）原样交给接口：没有注册表时接口照样能读出配置文件里的
内容，只是少了 ``help`` 与「运行期生效键位」。同理，配置接口本身也在函数体
里 import：命令模块的 ``setup`` 不引入 Qt / UI 依赖。

``pass_raw`` 的理由与 ``keymap.slots`` 相同：``--format json`` 这种宽松写法
只有拿原始 ``(args, kwargs)`` 才收得干净。

CLI 示例::

    albusctl ui shortcuts                 # 表格：命令 / 默认键 / 生效键 / 来源
    albusctl ui shortcuts --verbose       # 多一列 help
    albusctl ui shortcuts --format=json   # 机器可读（设置页直接吃）
    albusctl ui state                     # 已保存的界面状态
    albusctl ui state --format=json
"""

from __future__ import annotations

from typing import Any

from albuswall.command import CommandRegistry
from albuswall.command.exceptions import BadRequest
from albuswall.core import Container

from ._namespace import register_namespace
from ._render import render_table, split_common_args

__all__: list[str] = []

#: 容器里 keymap 注册表的 key（由 ``registry_shortcut_registry`` 写入）。
SHORTCUT_REGISTRY_KEY = "shortcuts"

#: ``--source`` 的合法取值（``ui.shortcuts`` 的过滤开关）。
SOURCES: tuple[str, ...] = ("all", "default", "user", "slot", "none")


def _settings_module() -> Any:
    """懒取界面层配置接口（命令模块 import 期不引入 UI / Qt）。"""
    from albuswall.ui.window import settings

    return settings


def _shortcuts(container: Container) -> Any | None:
    """懒取 keymap 注册表；本进程没有就是 ``None``（不是错误）。"""
    return container.get(SHORTCUT_REGISTRY_KEY)


def _keys_text(keys: list[str] | tuple[str, ...] | None) -> str:
    """键位列表 → ``F11, Ctrl+Shift+F``；空 → ``-``。"""
    values = [str(key) for key in (keys or ())]
    return ", ".join(values) if values else "-"


def _take_source(kwargs: dict[str, Any], positional: list[str]) -> str:
    """``--source=<name>``：只留某一来源的条目（默认 ``all``）。"""
    raw = kwargs.get("source")
    if raw is True:
        if not positional:
            raise BadRequest("--source requires a value (use --source=VALUE)")
        raw = positional.pop(0)
    if raw is None or raw is False:
        return "all"
    text = str(raw).strip().lower()
    if text not in SOURCES:
        raise BadRequest(
            f"unknown --source {text!r}; expected one of {', '.join(SOURCES)}"
        )
    return text


def setup(registry: CommandRegistry, container: Container) -> None:
    # 不在 setup 里解析任何依赖：keymap 注册表由 UI 启动流程后注册，
    # 配置接口在命令体内才 import（见模块 docstring）。
    _ = container

    @registry.command(
        "ui.shortcuts",
        help_="列出快捷键配置（默认表 / 用户覆盖 / 生效键位 / 冲突）",
        params=(),
        pass_raw=True,
    )
    def ui_shortcuts(args: list, kwargs: dict) -> Any:
        """输出快捷键配置信息。

        数据来自 :func:`albuswall.ui.window.settings.shortcut_config_info`：
        配置文件（默认表 + 用户覆盖）与运行期注册表（若本进程起过界面）
        合并后的视图。``--source=user`` 只看用户改过的条目，
        ``--source=none`` 看还没绑键位的命令。
        """
        fmt, verbose, positional = split_common_args(args, kwargs)
        source_filter = _take_source(kwargs, positional)
        if positional:
            raise BadRequest(
                f"ui shortcuts takes no positional arguments, "
                f"got {list(positional)!r}"
            )

        info = _settings_module().shortcut_config_info(_shortcuts(container))
        entries = list(info.get("commands") or ())
        if source_filter != "all":
            entries = [
                entry for entry in entries
                if str(entry.get("source")) == source_filter
            ]

        if fmt == "json":
            payload = dict(info)
            payload["commands"] = entries
            payload["filter"] = source_filter
            return payload

        lines: list[str] = []
        files = info.get("files") or {}
        lines.append(
            f"default table: {files.get('defaults') or '-'}"
            f"{'' if files.get('defaults_exists') else ' (missing)'}"
        )
        lines.append(
            f"user table:    {files.get('user') or '-'}"
            f"{'' if files.get('user_exists') else ' (not created yet)'}"
        )
        lines.append("")

        headers = ["command", "default_keys", "effective_keys", "source"]
        if verbose:
            headers.insert(1, "help")
        rows = []
        for entry in entries:
            row = [
                str(entry.get("command", "?")),
                _keys_text(entry.get("default_keys")),
                _keys_text(entry.get("effective_keys")),
                str(entry.get("source", "")),
            ]
            if verbose:
                row.insert(1, str(entry.get("help", "") or "-"))
            rows.append(row)
        lines.append(render_table(headers, rows) if rows else "no commands")

        conflicts = info.get("conflicts") or ()
        lines.append("")
        if conflicts:
            lines.append("conflicts (same keys → several commands):")
            for item in conflicts:
                lines.append(
                    f"  {_keys_text([item.get('keys', '')])}  →  "
                    f"{', '.join(str(name) for name in item.get('commands', ()))}"
                )
        else:
            lines.append("conflicts: none")

        unbound = [str(name) for name in (info.get("unbound") or ())]
        lines.append("")
        lines.append(
            "unbound (no key): " + (", ".join(unbound) if unbound else "none")
        )

        errors = [str(text) for text in (info.get("errors") or ())]
        if errors:
            lines.append("")
            lines.append("errors:")
            lines.extend(f"  {text}" for text in errors)
        if verbose:
            lines.append("")
            lines.append(
                "sources: default/effective/source 来自 "
                "albuswall.ui.window.settings.shortcut_config_info；"
                "生效键位在界面已启动时取自快捷键注册表，否则取自配置表。"
            )
        return "\n".join(lines)

    @registry.command(
        "ui.state",
        help_="输出已保存的界面状态（窗口几何 / 上次相册 / 网格列数）",
        params=(),
        pass_raw=True,
    )
    def ui_state(args: list, kwargs: dict) -> Any:
        """输出 ``[window]`` 段里的界面状态（presenter 退出时回写的那份）。

        只读：想改状态请用 ``setting set 'window.maximized=true'``，想清掉
        窗口几何就 ``setting set 'window.geometry='``。
        """
        fmt, verbose, positional = split_common_args(args, kwargs)
        if positional:
            raise BadRequest(
                f"ui state takes no positional arguments, "
                f"got {list(positional)!r}"
            )
        _ = verbose

        snapshot = _settings_module().window_state()
        payload = snapshot.to_dict()
        if fmt == "json":
            return payload

        rect = payload.get("rect")
        lines = [
            render_table(
                ("field", "value"),
                [
                    ["geometry", snapshot.geometry or "-"],
                    ["rect", ", ".join(str(n) for n in rect) if rect else "-"],
                    ["maximized", "true" if snapshot.maximized else "false"],
                    ["fullscreen", "true" if snapshot.fullscreen else "false"],
                    ["album_uuid", snapshot.album_uuid or "-"],
                    ["overlay", snapshot.overlay or "-"],
                    ["tile_columns", snapshot.tile_columns],
                ],
            )
        ]
        if snapshot.empty:
            lines.append("")
            lines.append(
                "nothing saved yet: every field still has its declared default"
            )
        return "\n".join(lines)

    register_namespace(
        registry,
        "ui",
        help_="界面配置内省（快捷键 / 界面状态；只读）",
        hint="加 --format=json 给脚本 / 设置页用；改配置请用 setting set。",
    )
