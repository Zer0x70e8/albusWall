#
"""分层契约测试：handler 只依赖 service、service 由容器装配。

这里断言的是**结构**（谁 import 谁、容器里有什么 key），不是业务行为。

只读观测命令（album/asset/tag/thumb/trash/source）那批"最小接口"已退役，
业务 handler 目前只剩 ``task.py``；等新契约落地时再把新模块加回
``_BUSINESS_HANDLERS``，本文件的结构断言会自动覆盖它们。
"""

from __future__ import annotations

import ast
import inspect
import pathlib

import pytest

from albuswall.command import CommandRegistry
from albuswall.core import Container
from albuswall.service import (
    CommandService,
    IpcRequestService,
    register_service,
)

_HANDLERS_DIR = pathlib.Path(
    inspect.getfile(__import__(
        "albuswall.service.command.handlers", fromlist=["x"]
    ))
).parent

# 只检查业务 handler；builtin 是诊断域，用不到 repo/service 之外的依赖。
_BUSINESS_HANDLERS = ("task.py",)


def _setup_sources():
    for name in _BUSINESS_HANDLERS:
        path = _HANDLERS_DIR / name
        yield path.name, path.read_text(encoding="utf-8")


@pytest.mark.parametrize("filename,source", list(_setup_sources()))
def test_setup_body_never_mentions_repo(filename, source):
    """``setup`` 的函数体里不得出现 ``repo`` 字样（模块 docstring 不受限）。"""
    tree = ast.parse(source)
    setup = next(
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "setup"
    )
    body = ast.get_source_segment(source, setup) or ""
    assert "repo" not in body.lower(), f"{filename}: setup 体内出现 repo"


@pytest.mark.parametrize("filename,source", list(_setup_sources()))
def test_handler_module_does_not_import_repositories(filename, source):
    """整个 handler 模块都不得 import ``albuswall.repositories.*``。

    比字串匹配更硬：即使改了变量名，只要真去拿 repo 就会命中 import 语句。
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert "repositories" not in (node.module or ""), (
                f"{filename}: import 了 {node.module}"
            )
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "repositories" not in alias.name, (
                    f"{filename}: import 了 {alias.name}"
                )


@pytest.mark.parametrize("filename,source", list(_setup_sources()))
def test_handler_requires_only_service_keys(filename, source):
    """``setup`` 里只用 ``container.require("xxx_service")`` 取依赖。"""
    tree = ast.parse(source)
    setup = next(
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "setup"
    )
    required: list[str] = []
    for node in ast.walk(setup):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("require", "get")
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "container"
        ):
            assert node.func.attr == "require", (
                f"{filename}: setup 里用了 container.{node.func.attr}"
            )
            assert node.args and isinstance(node.args[0], ast.Constant)
            required.append(node.args[0].value)

    assert required, f"{filename}: setup 没有解析任何依赖"
    assert all(key.endswith("_service") for key in required), (
        f"{filename}: 依赖的 key 不全是 service: {required}"
    )


def test_register_service_only_declares_command_services(clean_hooks):
    """``register_service`` 只装配命令层两道门面，业务 service 槽位已清空。"""
    container = Container()
    container.register("command_registry", CommandRegistry, returns=CommandRegistry)
    container.set("ipc_service", object())

    register_service(container)

    assert isinstance(container.require("command_service"), CommandService)
    assert isinstance(container.require("ipc_request_service"), IpcRequestService)
    assert container.require("command_service") is container.require("command_service")

    for key in (
        "view_service", "tag_service", "thumbnail_service",
        "trash_service", "source_service",
    ):
        assert key not in container, f"{key} 不该再被注册"


def test_modules_registers_only_remaining_handlers(assembled):
    """装配后只剩 builtin + task 的命令，退役的只读命令不得复活。"""
    registry, _ = assembled
    names = set(registry.names(include_hidden=True, subject=frozenset({"admin"})))

    assert {"ping", "help", "parse", "version", "task.stats"} <= names
    assert {
        "trash.count", "trash.list", "asset.path", "asset.get",
        "thumb.stats", "thumb.missing", "tag.list", "tag.search", "tag.get",
        "album.get", "album.count", "album.assets", "source.get", "source.list",
    }.isdisjoint(names)
