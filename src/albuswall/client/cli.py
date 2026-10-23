#
"""``albusctl``：命令行前端。

定位：**薄客户端**。它不 import 应用本体，只把一条命令发给正在运行的
AlbusWall 进程。因此 ``albusctl ping`` 的启动成本 ≈ Python 解释器启动。

退出码（约定，脚本可依赖）
--------------------------
===== ==============================================
0     成功
2     用法错误
3     应用没在跑（或 IPC 被配置关闭）
4     token 不匹配
5     服务端返回错误（JSON-RPC error）
6     超时（主循环卡住 / 命令执行过久）
130   被 Ctrl+C 中断
===== ==============================================
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import Any

from albuswall.__about__ import __title__, __version__
from albuswall.client.core import (
    AuthFailed,
    CallTimeout,
    IpcClient,
    NotRunning,
    ProtocolError,
)
from albuswall.common.ipc import DEFAULT_CALL_TIMEOUT, RemoteError

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_NOT_RUNNING = 3
EXIT_AUTH_FAILED = 4
EXIT_REMOTE = 5
EXIT_TIMEOUT = 6
EXIT_INTERRUPTED = 130


# --------------------------------------------------------------------------- #
# 参数
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="albusctl",
        description=f"控制正在运行的 {__title__} 实例（IPC 客户端）",
        epilog=(
            "示例：albusctl ping / albusctl sources / albusctl task.stats / "
            "albusctl settings / albusctl --json settings / "
            "albusctl settings set thumbnails.batch_size=64 / "
            "albusctl settings save / "
            "albusctl source.list '{}' / albusctl watch source.scan_finished\n"
            "任何 albusctl list 打印出来的命令名都可以直接当子命令用。"
        ),
    )
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_CALL_TIMEOUT,
        help=f"单次调用超时（秒），默认 {DEFAULT_CALL_TIMEOUT:g}",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="输出紧凑 JSON（便于管道处理）",
    )
    parser.add_argument(
        "--version", action="version",
        version=f"albusctl (albuswall) {__version__}",
    )

    sub = parser.add_subparsers(dest="command", metavar="<command>")

    sub.add_parser("ping", help="探测应用是否在运行")
    sub.add_parser("version", help="打印应用版本")
    sub.add_parser("list", help="列出可调用的命令")

    p_call = sub.add_parser("call", help="调用任意命令")
    p_call.add_argument("method", help="命令名，如 source.list")
    p_call.add_argument(
        "params", nargs="?", default="{}",
        help="JSON 对象形式的参数，默认 {}",
    )

    sub.add_parser("sources", help="列出被跟踪的导入源")
    sub.add_parser("import", help="唤醒导入 worker")
    sub.add_parser("thumbs", help="提交一批缩略图补齐任务")
    sub.add_parser("stats", help="打印任务服务统计")
    sub.add_parser("status", help="打印 IPC 服务状态")

    p_settings = sub.add_parser(
        "settings",
        help="查看 / 修改已登记的配置结构",
        description=(
            "无 action 时 dump albuswall.configue.setting_schema 里登记的配置"
            "结构（默认树形文本，--json 输出完整 JSON）。\n"
            "action=set 修改设置并立即落盘，action=save 把已标脏的节点落盘。\n"
            "示例：\n"
            "  albusctl settings\n"
            "  albusctl settings set thumbnails.batch_size=64\n"
            "  albusctl settings set --group thumbnails batch_size=64 "
            "--dry-run\n"
            "  albusctl settings set '[thumbnail]specs={\"small\": 64}'\n"
            "  albusctl settings save"
        ),
    )
    p_settings.add_argument(
        "action", nargs="?", choices=("set", "save"), default=None,
        help="set=修改并落盘；save=把已标脏的节点落盘；留空=dump 结构",
    )
    p_settings.add_argument(
        "assignments", nargs="*", metavar="KEY=VALUE",
        help=(
            "给 set 用：group.item=值 / [section]key=值 / 唯一叶子名=值。"
            "值是字符串时按声明类型转换（64 / false / {\"a\": 1} 都行）"
        ),
    )
    p_settings.add_argument(
        "--group", default=None,
        help="给 set 用：只在某个分组内生效（如 thumbnails）",
    )
    p_settings.add_argument(
        "--dry-run", action="store_true",
        help="只校验 + 预览（返回 old/new/将写入的 INI 文本），不写盘",
    )
    p_settings.add_argument(
        "--strict", action="store_true",
        help="写盘失败时直接报错，而不是保留 dirty",
    )
    p_settings.add_argument(
        "--view",
        choices=("text", "tree", "basic", "by_file", "flat"), default=None,
        help=(
            "dump 视图：text=树形文本（默认，非 --json 时）；tree=完整嵌套 JSON；"
            "basic=只要常规设置（去掉 advanced 子树）；"
            "by_file=按归属文件；flat=扁平叶子。默认随 --json 切换"
        ),
    )

    p_watch = sub.add_parser("watch", help="持续接收事件推送")
    p_watch.add_argument(
        "topics", nargs="*",
        help="要订阅的 topic；留空表示全部",
    )
    p_watch.add_argument(
        "--duration", type=float, default=None,
        help="最多监听多少秒（默认一直监听）",
    )

    p_quit = sub.add_parser("quit", help="请求应用优雅退出")
    p_quit.add_argument(
        "--reason", default="albusctl", help="记录到日志的退出原因",
    )

    return parser


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #
def _emit(value: Any, *, compact: bool) -> None:
    if isinstance(value, str):
        print(value)
        return
    if compact:
        print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    else:
        print(json.dumps(value, ensure_ascii=False, indent=2))


def _fail(message: str, code: int) -> int:
    print(f"{__title__}: {message}", file=sys.stderr)
    return code


# --------------------------------------------------------------------------- #
# 子命令
# --------------------------------------------------------------------------- #
_SHORTCUTS: dict[str, tuple[str, dict[str, Any]]] = {
    # 子命令 -> (远端命令, 参数)
    "ping": ("app.ping", {}),
    "sources": ("source.list", {}),
    "import": ("import.trigger", {}),
    "thumbs": ("thumbnail.scan", {}),
    "stats": ("task.stats", {}),
    "status": ("ipc.stats", {}),
}


def _run_simple(
        client: IpcClient,
        method: str,
        params: dict[str, Any],
        args: argparse.Namespace,
) -> int:
    result = client.call(method, params, timeout=args.timeout)
    if method == "app.ping" and not args.json:
        state = "running" if result.get("ok") else "unknown"
        print(f"{result.get('name', __title__)} {result.get('version', '?')} "
              f"{state} (pid {result.get('pid', '?')})")
        return EXIT_OK
    if method == "ipc.stats" and not args.json:
        limits = result.get("limits") or {}
        state = "listening" if result.get("running") else "not listening"
        print(
            f"ipc {state} @ {result.get('address') or '-'}  "
            f"clients={result.get('clients', 0)}  "
            f"accepted={result.get('accepted', 0)}  "
            f"handled={result.get('handled', 0)}  "
            f"errors={result.get('errors', 0)}  "
            f"timeout={limits.get('call_timeout', '?')}s  "
            f"dispatcher={'bound' if result.get('dispatcher_bound') else 'unbound'}"
        )
        return EXIT_OK
    _emit(result, compact=args.json)
    return EXIT_OK


def _run_list(client: IpcClient, args: argparse.Namespace) -> int:
    result = client.call("command.list", {}, timeout=args.timeout)
    commands: list[str] = list(result.get("commands") or [])
    if args.json:
        _emit(result, compact=True)
        return EXIT_OK
    descriptions = result.get("descriptions")
    if not isinstance(descriptions, dict):
        descriptions = {}
    width = max((len(name) for name in commands), default=0)
    for name in commands:
        doc = descriptions.get(name) or ""
        print(f"{name:<{width}}  {doc}".rstrip())
    # 提示走 stderr：stdout 保持「纯命令名」方便管道处理
    print(
        "\nThese names can be used directly as subcommands, e.g. "
        "albusctl source.list '{}'\n"
        "Shorter aliases: sources / import / thumbs / stats / status / "
        "settings / ping / quit",
        file=sys.stderr,
    )
    return EXIT_OK


def _run_call(client: IpcClient, args: argparse.Namespace) -> int:
    try:
        params = json.loads(args.params or "{}")
    except json.JSONDecodeError as exc:
        return _fail(f"invalid JSON params: {exc}", EXIT_USAGE)
    if not isinstance(params, dict):
        return _fail("params must be a JSON object", EXIT_USAGE)
    result = client.call(args.method, params, timeout=args.timeout)
    _emit(result, compact=args.json)
    return EXIT_OK


def _parse_assignments(pairs: list[str]) -> dict[str, Any]:
    """``KEY=VALUE`` → dict；值能当 JSON 解析就用解析结果（64 / false / {...}）。"""
    values: dict[str, Any] = {}
    for pair in pairs:
        key, sep, raw = pair.partition("=")
        key = key.strip()
        if not sep or not key:
            raise ValueError(f"expected KEY=VALUE, got {pair!r}")
        if key in values:
            raise ValueError(f"duplicate key: {key!r}")
        text = raw.strip()
        try:
            values[key] = json.loads(text)
        except json.JSONDecodeError:
            values[key] = raw  # 普通字符串（主题名、路径、${...} 模板）
    return values


def _run_settings(client: IpcClient, args: argparse.Namespace) -> int:
    """``albusctl settings``：dump 结构 / 修改（set）/ 落盘（save）。"""
    action = getattr(args, "action", None)

    if action == "save":
        result = client.call(
            "setting.save",
            {"dry_run": args.dry_run, "strict": args.strict},
            timeout=args.timeout,
        )
        _emit(result, compact=args.json)
        return EXIT_OK if result.get("ok", True) else EXIT_REMOTE

    if action == "set":
        try:
            values = _parse_assignments(args.assignments or [])
        except ValueError as exc:
            return _fail(str(exc), EXIT_USAGE)
        if not values:
            return _fail(
                "settings set 需要至少一个 KEY=VALUE，"
                "例如 albusctl settings set thumbnails.batch_size=64",
                EXIT_USAGE,
            )
        params: dict[str, Any] = {
            "values": values,
            "dry_run": args.dry_run,
            "strict": args.strict,
        }
        if args.group:
            params["group"] = args.group
        result = client.call("setting.set", params, timeout=args.timeout)
        _emit(result, compact=args.json)
        return EXIT_OK if result.get("ok", True) else EXIT_REMOTE

    view = args.view or ("tree" if args.json else "text")
    result = client.call("setting.schema", {"view": view}, timeout=args.timeout)
    _emit(result, compact=args.json)
    return EXIT_OK


def _run_watch(client: IpcClient, args: argparse.Namespace) -> int:
    subscribed = client.subscribe(*args.topics)
    if not args.json:
        label = ", ".join(subscribed) if subscribed else "all"
        print(f"listening for events: {label} (Ctrl+C to stop)", file=sys.stderr)
    for event in client.events(timeout=args.duration):
        _emit(event, compact=args.json)
        sys.stdout.flush()
    return EXIT_OK


def _run_quit(client: IpcClient, args: argparse.Namespace) -> int:
    result = client.call("app.quit", {"reason": args.reason}, timeout=args.timeout)
    _emit(result, compact=args.json)
    return EXIT_OK


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
#: 全部一级子命令。**不在**这个集合里的第一个词会被当成远端命令名，
#: 自动补成 ``call <name>``——这样 ``albusctl list`` 打印出来的
#: ``task.stats`` 可以原样敲一遍，不需要记住 CLI 的别名。
_SUBCOMMANDS = frozenset({
    "ping", "version", "list", "call",
    "sources", "import", "thumbs", "stats", "status", "settings",
    "watch", "quit",
})

#: 需要跟一个取值的全局选项；规范化 argv 时要跳过它们的值。
_VALUE_OPTIONS = frozenset({"--timeout"})


def _normalize_argv(argv: Sequence[str]) -> list[str]:
    """把 ``albusctl task.stats '{}'`` 改写成 ``albusctl call task.stats '{}'``。

    只处理「子命令位置」上的第一个非选项词，其余原样透传。
    """
    args = list(argv)
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--":
            index += 1
            break
        if not token.startswith("-"):
            break
        if token in _VALUE_OPTIONS:
            index += 2
            continue
        index += 1

    if index < len(args) and args[index] not in _SUBCOMMANDS:
        args.insert(index, "call")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(_normalize_argv(sys.argv[1:] if argv is None else argv))
    if not args.command:
        parser.print_help()
        return EXIT_USAGE

    try:
        with IpcClient(timeout=args.timeout) as client:
            if args.command in _SHORTCUTS:
                method, params = _SHORTCUTS[args.command]
                return _run_simple(client, method, params, args)
            if args.command == "version":
                result = client.call("app.ping", {}, timeout=args.timeout)
                print(result.get("version", "unknown"))
                return EXIT_OK
            if args.command == "list":
                return _run_list(client, args)
            if args.command == "settings":
                return _run_settings(client, args)
            if args.command == "call":
                return _run_call(client, args)
            if args.command == "watch":
                return _run_watch(client, args)
            if args.command == "quit":
                return _run_quit(client, args)
            return _fail(f"unknown command: {args.command}", EXIT_USAGE)

    except NotRunning as exc:
        return _fail(f"{exc}\n"
                     f"提示：AlbusWall 需要正在运行，且 [ipc] enabled = true。",
                     EXIT_NOT_RUNNING)
    except AuthFailed as exc:
        return _fail(f"{exc}\n"
                     f"提示：删除残留 token 后重试，或确认连的是自己的实例。",
                     EXIT_AUTH_FAILED)
    except CallTimeout as exc:
        return _fail(str(exc), EXIT_TIMEOUT)
    except RemoteError as exc:
        return _fail(f"remote error {exc.code}: {exc.message}", EXIT_REMOTE)
    except ProtocolError as exc:
        return _fail(str(exc), EXIT_REMOTE)
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
