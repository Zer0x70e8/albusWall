#
"""python -m albuswall.infrastructure.ipc —— IPC 自诊断入口。

不做这些：
  - 不 bind 端点、不启动服务
  - 不注册业务命令、不做命令路由
  - 不改动任何状态

只做这些（只读诊断）：
  probe      检查当前是否有实例在跑（默认动作）
  call       向运行中实例发一次请求（便利接口 METHOD --params，
             或原始接口 --request，走 call_raw）
  endpoints  打印端点 / runtime / authkey 路径信息
"""
from __future__ import annotations

import argparse
import json
import sys

from ._client import IpcClient
from ._constants import PROTOCOL_VERSION
from ._endpoints import get_endpoint, get_authkey, _runtime_dir
from ._errors import (
    IpcTimeout, IpcUnavailable, IpcRemoteError, IpcProtocolError,
)
from ._probe import probe_instance


# ---- 子命令 ----

def _cmd_probe(args) -> int:
    info = probe_instance(timeout=args.timeout)
    if info is None:
        print("no instance")
        return 1
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0


# noinspection broad-exception
def _run_and_print(call_fn, args) -> int:
    """执行一次请求并把结果/错误打到 stdout/stderr，返回退出码。"""
    try:
        with IpcClient(timeout=args.timeout) as c:
            result = call_fn(c)
    except IpcTimeout as e:
        print(f"timeout: {e}", file=sys.stderr)
        return 3
    except IpcUnavailable as e:
        print(f"unavailable: {e}", file=sys.stderr)
        return 4
    except IpcRemoteError as e:
        print(f"remote error [{e.code}]: {e}", file=sys.stderr)
        return 5
    except IpcProtocolError as e:
        print(f"protocol error: {e}", file=sys.stderr)
        return 6
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _cmd_call(args) -> int:
    # --request 模式：原样透传 request dict，走 call_raw
    if args.request is not None:
        if args.method:
            print("call: METHOD and --request are mutually exclusive",
                  file=sys.stderr)
            return 2
        if args.params is not None:
            print("call: --params and --request are mutually exclusive",
                  file=sys.stderr)
            return 2
        try:
            req = json.loads(args.request)
        except json.JSONDecodeError as e:
            print(f"invalid --request JSON: {e}", file=sys.stderr)
            return 2
        if not isinstance(req, dict):
            print("--request must be a JSON object", file=sys.stderr)
            return 2
        return _run_and_print(lambda c: c.call_raw(req, timeout=args.timeout),
                              args)

    # 便利模式：METHOD + --params，走 call
    if not args.method:
        print("call: need METHOD or --request", file=sys.stderr)
        return 2
    try:
        params = json.loads(args.params) if args.params else {}
    except json.JSONDecodeError as e:
        print(f"invalid --params JSON: {e}", file=sys.stderr)
        return 2
    if not isinstance(params, dict):
        print("--params must be a JSON object", file=sys.stderr)
        return 2
    return _run_and_print(
        lambda c: c.call(args.method, params, timeout=args.timeout), args,
    )


def _cmd_endpoints(args) -> int:
    print(f"endpoint = {get_endpoint()}")
    print(f"runtime  = {_runtime_dir()}")
    print(f"authkey  = {'present' if get_authkey(create=False) else 'absent'}")
    print(f"protocol = {PROTOCOL_VERSION}")
    return 0


# ---- argparse ----

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m albuswall.infrastructure.ipc",
        description="AlbusWall IPC 自诊断（不启动服务、不注册业务命令）。",
    )
    p.add_argument("--timeout", type=float, default=1.5,
                   help="请求/探活超时秒数，默认 1.5")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("probe", help="探活（默认动作）")
    c = sub.add_parser(
        "call",
        help="调用运行中实例：METHOD --params JSON，或 --request JSON（原始 request）",
    )
    c.add_argument("method", nargs="?",
                   help="方法名，例如 ping；与 --request 互斥")
    c.add_argument("--params", default=None,
                   help="JSON 对象参数；缺省为 {}；与 --request 互斥")
    c.add_argument("--request", "-r", default=None,
                   help="整个 request 的 JSON 对象，走 call_raw；"
                        "与 METHOD/--params 互斥")
    sub.add_parser("endpoints", help="打印端点/runtime/authkey 信息")
    return p


def main_(args) -> int:
    cmd = args.cmd or "probe"  # 裸调用 = probe
    if cmd == "probe":
        return _cmd_probe(args)
    if cmd == "call":
        return _cmd_call(args)
    if cmd == "endpoints":
        return _cmd_endpoints(args)
    return 2


# noinspection broad-exception
def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return main_(args)
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
