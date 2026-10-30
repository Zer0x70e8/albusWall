#! /usr/bin/python3
# -*- coding: utf-8 -*-
"""Command line user interface.

Architecture
------------
The command registry lives on the running instance (server side).
The CLI knows nothing about which business commands exist; it only adapts
argv into an IPC request, ships it, and renders the reply.

    argv ──► 硬编码: version / help / -h / --help
         ──► 解析:    method + args + kwargs  ──►  IPC  ──►  echo

参数解析规则（客户端）：
    value           -> args.append(typed(value))
    --key=value     -> kwargs[key] = typed(value)
    --key           -> kwargs[key] = True
    --no-key        -> kwargs[key] = False
    --              -> 之后全部按位置参数处理

typed() 优先 ast.literal_eval（支持 True/False/None/数字/容器），
失败则保留原始字符串。业务命令注册表在服务端，客户端不做校验。
"""

from __future__ import annotations

import ast
import json
import sys
from typing import Any

import albuswall
from albuswall.infrastructure.ipc import (
    PROTOCOL_VERSION,
    IpcClient,
    IpcError,
    IpcRemoteError,
    IpcTimeout,
    IpcUnavailable,
)
from albuswall.log import getLogger, setup_cli_logging

_log = getLogger(__name__)

_HELP_FLAGS = ("help", "-h", "--help")
_VERSION_FLAGS = ("version", "-v", "--version")

_HELP_METHOD = "help"

_DEFAULT_TIMEOUT = 3.0


# ---- 参数解析 ----

def _typed(raw: str) -> Any:
    """把字符串还原成 Python 值；失败则保留原始字符串。"""
    if raw == "":
        return ""
    try:
        return ast.literal_eval(raw)
    except (ValueError, SyntaxError):
        return raw


def parse_cli_args(tokens: list[str]) -> tuple[list, dict]:
    """把 Unix 风格 token 解析为 (positional_args, keyword_args)。"""
    args: list = []
    kwargs: dict = {}
    end_of_flags = False

    for tok in tokens:
        if end_of_flags:
            args.append(_typed(tok))
            continue
        if tok == "--":
            end_of_flags = True
            continue
        if tok.startswith("--") and len(tok) > 2:
            body = tok[2:]
            if "=" in body:
                key, _, raw = body.partition("=")
                kwargs[key] = _typed(raw)
            elif body.startswith("no-") and len(body) > 3:
                kwargs[body[3:]] = False
            else:
                kwargs[body] = True
        else:
            args.append(_typed(tok))

    return args, kwargs


# ---- 入口 ----

def run_cli(argv: list[str]) -> int:
    """入口：version / help 特殊处理，其余 argv 解析后转发。"""
    setup_cli_logging()

    if not argv or argv[0] in _HELP_FLAGS:
        return _run_help(argv[1:])

    if argv[0] in _VERSION_FLAGS:
        _log.info(
            "project_version: %s\n"
            "cli_protocol_version: %s",
            albuswall.__version__,
            PROTOCOL_VERSION
        )
        return 0

    args, kwargs = parse_cli_args(argv[1:])
    return _run_call(argv[0], args, kwargs)


# ---- 执行 ----

def _run_help(rest: list[str]) -> int:
    """向主实例要命令列表；拿不到就退回基础用法。"""
    args, kwargs = parse_cli_args(rest)
    params = {"args": args, "kwargs": kwargs}

    try:
        with IpcClient(timeout=1.5) as c:
            registry = c.call(_HELP_METHOD, params, timeout=1.5)
    except IpcRemoteError as e:
        if e.code == "no_handler":
            _log.info("主实例在线，但业务层尚未就绪；仅 version / ping 可用。")
            return 0
        _log.info("%s", _basic_help())
        return 0
    except IpcUnavailable:
        _log.info("主实例未运行。")
        return 0
    except IpcError:
        _log.info("%s", _basic_help())
        return 0

    _emit_result(registry)
    return 0


def _run_call(method: str, args: list, kwargs: dict) -> int:
    params = {"args": args, "kwargs": kwargs}
    try:
        with IpcClient(timeout=_DEFAULT_TIMEOUT) as c:
            result = c.call(method, params, timeout=_DEFAULT_TIMEOUT)
    except IpcUnavailable as e:
        _log.error("无实例可连接: %s", e)
        return 4
    except IpcTimeout as e:
        _log.error("请求超时: %s", e)
        return 3
    except IpcRemoteError as e:
        # IPC 层语义（不是业务异常）：code 由 _service.py 直接产生，
        # 保留专门映射。
        if e.code == "no_handler":
            _log.error("主程序已启动但业务层尚未就绪，请稍候重试。")
            return 7
        # 业务异常：退出码由**服务端**决定（异常类自己声明，随响应下发到
        # ``IpcRemoteError.exit_code``）。客户端不再维护 code → exit_code
        # 的映射表，也不再 import 服务端的异常模块。
        exit_code = getattr(e, "exit_code", None)
        if exit_code is not None:
            _log.error("命令失败 [%s]: %s", e.code, e)
            return int(exit_code)
        # 服务端没给建议（纯 IPC 层错误 / 未知 code）：保持 5。
        _log.error("远端错误 [%s]: %s", e.code, e)
        return 5
    except IpcError as e:
        _log.error("IPC 错误: %s", e)
        return 6

    _emit_result(result)
    return 0


# ---- 帮助 / 输出 ----

def _basic_help() -> str:
    return "\n".join([
        "用法: albuswall <command> [args...] [--key=value ...]",
        "",
        f"  {'version':<10} 协议版本",
        f"  {'ping':<10} 检查连接",
        "  （主实例未运行或未就绪，无法获取命令列表）",
        "",
        f"协议版本: {PROTOCOL_VERSION}",
    ])


def _emit_result(result) -> None:
    if isinstance(result, (dict, list)):
        _log.info("%s", json.dumps(result, ensure_ascii=False, indent=2))
    else:
        _log.info("%s", result)


if __name__ == "__main__":
    raise SystemExit(run_cli(sys.argv[1:]))
