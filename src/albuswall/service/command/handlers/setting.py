#
"""配置读写命令：``setting.schema`` / ``setting.set`` / ``setting.save``。

三条命令都只是 ``albuswall.configue.schema`` 的薄适配层：枚举结构、按结构
安全改配置、把脏节点落盘。业务判定（值怎么转、写到哪个文件、校验失败怎么
回滚）全部在配置层，这里只做**入参收口 + 异常翻译**。

为什么不在 ``setup`` 里取 ``config``
-----------------------------------
``tests/conftest.py::fake_container`` 里**没有** ``config`` 键——装配层的
``setup_handlers`` 会跑在那种容器上（所有用 ``assembled`` fixture 的用例都
如此）。若在 ``setup`` 里 ``require("config")``，整个 handler 装配会在测试
环境直接 ``MissingDependencyError``。所以配置实例一律在**每个命令函数体内**
懒取，缺了就抛 ``BadRequest``（调用方的错，退出码 64），而不是让装配失败。

副作用：``setup`` 里对 ``container`` 只做保存，不做解析——这是本模块**唯一**
一处与 ``tests/test_handlers_contract.py::test_handler_requires_only_service_keys``
的白名单冲突的地方，因此 ``setting.py`` 刻意不进 ``_BUSINESS_HANDLERS``。

CLI 示例（``python -m albuswall setting ...`` 会启动 GUI，请用 ``albusctl``）::

    albusctl setting schema                      # 树形文本
    albusctl setting schema --view=flat          # 脚本友好的扁平叶子列表
    albusctl setting schema --view=by_file
    albusctl setting set thumbnail.batch_size=64 --dry-run
    albusctl setting set 'ui.theme=dark' 'log.level=DEBUG'
    albusctl setting set --group thumbnail batch_size=64 --strict
    albusctl setting set --values='{"thumbnail.batch_size": 64}'
    albusctl setting save --dry-run

与旧实现的一处**有意差异**：``setting.schema`` 的默认视图从 ``tree`` 改成
``text``——新 CLI 没有 ``--json`` 开关，``text`` 才是「人敲一下就能看」的
形态；脚本请显式 ``--view=flat``。
"""

from __future__ import annotations

import ast
import json
from collections.abc import Mapping
from typing import Any

from albuswall.command import CommandRegistry
from albuswall.command.exceptions import BadRequest
from albuswall.core import Container

from ._namespace import register_namespace
from ._params import as_bool, flag, opt, pos

__all__: list[str] = []

#: ``setting.schema --view`` 的合法取值（顺序即文档顺序）。
_VIEWS: tuple[str, ...] = ("text", "tree", "basic", "by_file", "flat")


# ==================== 容器 / 入参辅助 ====================

def _config(container: Container) -> Any:
    """懒取配置单例；拿不到就是调用方的错（``BadRequest`` → 退出码 64）。"""
    config = container.get("config")
    if config is None:
        raise BadRequest(
            "configuration is unavailable in this context "
            "(container has no 'config')"
        )
    return config


def _literal(raw: str) -> Any:
    """``64`` / ``true`` / ``'dark'`` → Python 值；解析不了就保留字符串。

    与 ``albuswall/cli.py::parse_cli_args`` 同一套规则（那里 ``ast.literal_eval``
    失败也保留原串）：``dark`` 这种裸词会原样交给配置层按声明类型转换。
    """
    text = raw.strip()
    if text == "":
        return ""
    try:
        return ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return raw


def _parse_assignments(tokens: list[Any]) -> dict[str, Any]:
    """``["a=1", "b=x"]`` → ``{"a": 1, "b": "x"}``（同一键出现两次即报错）。"""
    out: dict[str, Any] = {}
    for token in tokens:
        if not isinstance(token, str):
            raise BadRequest(
                f"expected KEY=VALUE string, got {type(token).__name__}"
            )
        text = token.strip()
        if not text:
            continue
        if "=" not in text:
            raise BadRequest(f"expected KEY=VALUE, got {token!r}")
        key, _, raw = text.partition("=")
        key = key.strip()
        if not key:
            raise BadRequest(f"expected KEY=VALUE, got {token!r}")
        if key in out:
            raise BadRequest(f"duplicate setting key {key!r}")
        out[key] = _literal(raw)
    return out


def _parse_values_option(raw: Any) -> dict[str, Any]:
    """``--values`` 收口：接受 JSON 字符串或已经解析好的映射。"""
    if raw is None or raw == "":
        return {}
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BadRequest(f"--values: invalid JSON: {exc}") from exc
    else:
        parsed = raw
    if not isinstance(parsed, Mapping):
        raise BadRequest(
            f"--values: expected an object mapping key -> value, "
            f"got {type(parsed).__name__}"
        )
    return dict(parsed)


def _dirty_report(
        config: Any,
        payload: dict[str, Any],
) -> dict[str, Any]:
    """给 ``setting.set`` 的返回值补上「本次还有哪些没落盘」。

    判定依据是**本次碰过的节点**（``SettingWriteResult.nodes``）里仍在 dirty
    的那些。**不能**用调用前后的 ``dirty_nodes()`` 差集：某节点上次写失败就
    一直脏着（``flush`` 非 ``strict`` 时只留 dirty），这次又写失败时它不在
    差集里，于是命令报 ``ok=True`` —— 用户看到「成功」，磁盘没变。
    """
    report = dict(payload)
    touched = {tuple(node) for node in report.get("nodes") or ()}
    dirty = {tuple(node) for node in config.dirty_nodes()}
    leftover = sorted(touched & dirty)
    report["pending"] = [f"[{section}]{key}" for section, key in leftover]
    report["ok"] = not leftover
    report["error"] = (
        str(config.last_flush_error)
        if leftover and config.last_flush_error is not None
        else None
    )
    return report


def _apply_language_change(container: Container, result: Any) -> None:
    """``[ui] language`` 被改掉时，顺带通知 i18n（拿不到就静默跳过）。

    命令行改配置也要能动态生效，所以旁路动作放在命令层。翻译器属于 GUI：
    容器里没有 ``i18n`` 键、或它没有 ``set_language`` 时**静默返回**——
    本函数绝不能因为「没装 Qt」而让 ``setting set`` 失败。
    """
    applied = tuple(getattr(result, "applied", ()) or ())
    if not any(str(key).endswith("language") for key in applied):
        return

    i18n = container.get("i18n")
    set_language = getattr(i18n, "set_language", None)
    if not callable(set_language):
        return

    config = container.get("config")
    try:
        language = config.resolver.get_typed("ui", "language")
        set_language(str(language))
    except Exception:  # noqa: BLE001  没有这个节点 / 翻译器异常都不影响写盘
        return


# ==================== 装配 ====================

def setup(registry: CommandRegistry, container: Container) -> None:
    """注册三条命令 + ``setting`` 命名空间入口。

    这里**不**解析任何依赖（见模块 docstring）：``container`` 只被闭包捕获，
    真正的 ``config`` 在函数体里懒取。
    """

    @registry.command(
        "setting.schema",
        help_="列出已声明的配置结构（结构 / 当前值 / 归属文件）",
        params=(
            opt(
                "view",
                default="text",
                help="视图：text（默认，树形文本）/ tree / basic / "
                     "by_file / flat",
            ),
            flag(
                "undeclared",
                help="只列出未声明节点（文件里有、设置树枚举不到的）",
            ),
        ),
    )
    def setting_schema(view: str = "text", undeclared: bool = False) -> Any:
        """导出配置结构。

        ``view`` 语义：``text`` 返回可直接打印的字符串，``flat`` 是叶子列表，
        ``by_file`` 按归属文件分组，``tree`` / ``basic`` 是嵌套 dict（``basic``
        只留非高级项）。声明未 ``load()``、值读失败、无归属文件都只是降级
        输出，**不会**抛错。

        ``--undeclared``：只列**未声明节点**（``[shortcuts]`` 这类 load hook
        补类型的段、文件里手写且没人声明的 key）。它们不在设置树里，改不了，
        但仍然是文件里真实存在的内容——这项让「覆盖不到的东西」至少可见。

        数据来自 ``configue.dump_schema_dict``（只给声明事实 + 值 + 状态），
        展示策略（``widget`` / 控件约束 / 选项文案 / ``advanced`` 分级）与
        视图装配、排版都在本侧：``_present.apply_present`` + ``render_view``。
        """
        from albuswall.configue.schema import dump_schema_dict

        from ._present import apply_present, render_view, uncovered_view

        text = str(view or "text").strip().lower()
        if text not in _VIEWS:
            raise BadRequest(
                f"unknown view {text!r}; expected text/tree/basic/by_file/flat"
            )
        dump = apply_present(
            dump_schema_dict(config=container.get("config")),
            show_advanced=(text != "basic"),
        )
        if as_bool(undeclared, field="undeclared"):
            return uncovered_view(dump, text)
        return render_view(dump, text)

    @registry.command(
        "setting.set",
        help_="修改配置并立即落盘（先全量校验，再统一写）",
        # pass_raw：位置参数是任意多个 KEY=VALUE，无法用 Param 表达；
        # 下面的 params 只为 ``albuswall help setting.set`` 可读。
        params=(
            pos(
                "assignments",
                required=False,
                default="",
                help="KEY=VALUE，可给多个（值先 ast.literal_eval，失败则原样）",
            ),
            opt("values", default="", help='等价写法 {"group.item": 值}（IPC/GUI）'),
            opt("group", default="", help="只在某组内解析键（如 thumbnail）"),
            flag("dry-run", help="只校验 + 预览，不写内存也不写盘"),
            flag("strict", help="写盘失败立刻报错（默认保留 dirty）"),
        ),
        pass_raw=True,
    )
    def setting_set(args: list, kwargs: dict) -> dict:
        """改配置并落盘。

        位置参数 ``KEY=VALUE`` 与 ``--values='{...}'`` 会合并且**位置参数
        优先**；同一个键同时出现在两处直接报错（不做静默覆盖）。

        返回值是 ``SettingWriteResult.to_dict()`` 再加三个字段：

        * ``pending``：**本次提交的节点里**没落盘的（``[section]key``）；
        * ``ok``：``pending`` 为空即成功；
        * ``error``：仍有残留时的 ``last_flush_error``（否则 ``None``）。
        """
        from albuswall.configue.errors import ConfigValidationError
        from albuswall.configue.schema import save_group, save_values

        known = {"values", "group", "dry-run", "dry_run", "strict"}
        unknown = sorted(set(kwargs) - known)
        if unknown:
            raise BadRequest(f"unknown option(s): {', '.join(unknown)}")

        dry_run = as_bool(
            kwargs.get("dry-run", kwargs.get("dry_run", False)),
            field="dry-run",
        )
        strict = as_bool(kwargs.get("strict", False), field="strict")
        group = str(kwargs.get("group") or "").strip()

        merged = _parse_values_option(kwargs.get("values"))
        for key, value in _parse_assignments(list(args or [])).items():
            if key in merged:
                raise BadRequest(
                    f"duplicate setting key {key!r} "
                    f"(given by both KEY=VALUE and --values)"
                )
            merged[key] = value
        if not merged:
            raise BadRequest(
                "setting set requires at least one value, e.g. "
                "albusctl setting set thumbnail.batch_size=64"
            )

        config = _config(container)
        try:
            if group:
                result = save_group(
                    group, merged, config=config,
                    strict=strict, dry_run=dry_run,
                )
            else:
                result = save_values(
                    merged, config=config, strict=strict, dry_run=dry_run,
                )
        except ConfigValidationError as exc:
            # 校验失败（类型对了、语义不合法）：仍然是**调用方**的错 →
            # BadRequest（64），文案原样透出（含 ``[section]key``），不要
            # 重新包装成「命令执行失败」。必须写在通用分支之前：它是
            # ``ConfigError`` 的子类、**不是** ``ValueError``，漏在这里就会
            # 落到 Dispatcher 的 ``CommandFailed``（70）兜底。
            raise BadRequest(str(exc)) from exc
        except (KeyError, TypeError, ValueError) as exc:
            # 参数级问题（分组不存在 / 键未声明 / 值转不过去 / 无处可写）
            # 都是调用方的错 → BadRequest（64）。写盘失败是另一条路：
            # strict=True 时 ConfigError 从 flush 直接冒上去，由 Dispatcher
            # 包成 CommandFailed（70）。
            raise BadRequest(str(exc)) from exc

        _apply_language_change(container, result)
        return _dirty_report(config, result.to_dict())

    @registry.command(
        "setting.save",
        help_="把已标脏的配置节点落盘",
        # 与 setting.set 同一取舍：**pass_raw**。
        # 原因是选项名带连字符（``--dry-run``）——``Dispatcher._bind`` 会把
        # 选项名当作关键字参数名原样传给 handler，``--dry-run`` 因此只能
        # 走 ``pass_raw`` 自己收口（顺带兼容 ``--dry_run`` 的旧写法）。
        params=(
            flag("dry-run", help="只列出待写节点，不写盘"),
            flag("strict", help="写盘失败立刻报错"),
            opt("file", default="", help="只回写该注册名的文件（如 main）"),
        ),
        pass_raw=True,
    )
    def setting_save(args: list, kwargs: dict) -> dict:
        """落盘当前 dirty 节点。

        ``setting.set`` 自己会落盘；本命令用于「别的代码路径改了值、又想
        立刻持久化」的场景（动态赋值，或 ``autosave=manual`` 策略下）——
        也可以用来重试一次之前写失败的节点。

        选项：``--dry-run`` / ``--dry_run``（只列待写节点）、``--strict``、
        ``--file=<reg name>``。返回值：``dry_run`` / ``written`` / ``pending``
        / ``ok`` / ``error``。
        """
        if args:
            raise BadRequest(
                "setting save takes no positional arguments, "
                f"got {list(args)!r}"
            )
        known = {"dry-run", "dry_run", "strict", "file", "help"}
        unknown = sorted(set(kwargs) - known)
        if unknown:
            raise BadRequest(f"unknown option(s): {', '.join(unknown)}")

        config = _config(container)
        dry_run = as_bool(
            kwargs.get("dry-run", kwargs.get("dry_run", False)),
            field="dry-run",
        )
        strict = as_bool(kwargs.get("strict", False), field="strict")
        name = str(kwargs.get("file") or "").strip()

        if dry_run:
            pending = sorted(config.dirty_nodes())
            return {
                "dry_run": True,
                "written": [],
                "pending": [f"[{section}]{key}" for section, key in pending],
                "ok": not pending,
                "error": None,
            }

        try:
            written = config.flush(strict=strict, file=name or None)
        except (KeyError, RuntimeError, ValueError) as exc:
            # 未登记 / 未 load / 与 only= 冲突：都是调用方把 --file 传错了，
            # 不能让它变成退出码 70 的「命令执行失败」。
            raise BadRequest(str(exc)) from exc

        pending = sorted(config.dirty_nodes())
        return {
            "dry_run": False,
            "written": [str(path) for path in written],
            "pending": [f"[{section}]{key}" for section, key in pending],
            "ok": not pending,
            "error": (
                str(config.last_flush_error)
                if pending and config.last_flush_error is not None
                else None
            ),
        }

    register_namespace(
        registry,
        "setting",
        help_="配置读写（结构 / 修改 / 落盘）",
        hint="例: albuswall setting schema --view=flat",
    )
