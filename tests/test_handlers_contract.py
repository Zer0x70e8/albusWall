#
"""分层契约测试：handler 只依赖 service、service 由容器装配。

这里断言的是**结构**（谁 import 谁、容器里有什么 key），不是业务行为。

只读观测命令（album/asset/tag/thumb/trash/source）那批"最小接口"已退役；
业务 handler 目前是 ``task.py`` / ``source.py`` / ``import_.py``，本文件的结构
断言自动覆盖 ``_BUSINESS_HANDLERS`` 里的每个模块。
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
_BUSINESS_HANDLERS = (
    "task.py", "source.py", "import_.py",
    "asset.py", "album.py", "tag.py", "trash.py", "thumbnail.py",
)


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
    # 允许的后缀都是 **service 层对象**：
    #   ``*_service``   普通服务（含 ``SourceRuntime`` / ``ViewService`` …）
    #   ``*_runtime``   运行控制面（``SourceRuntime`` 持有扫描任务表）
    #   ``*_scheduler`` 定时投递器（``TrashCleanupScheduler``：只投递不执行）
    #   ``*_cleaner``   缓存维护器（``ThumbnailCleaner``：只碰磁盘）
    # 禁止的是仓储（repo）与基础设施（ipc/db/task 本体）。
    assert all(
        key.endswith(("_service", "_runtime", "_scheduler", "_cleaner"))
        for key in required
    ), f"{filename}: 依赖的 key 不全是 service 层对象: {required}"


def test_register_service_registers_every_domain(clean_hooks):
    """``register_service`` 装配全部业务域（键名即 ``Services`` 表的键）。"""
    container = Container()
    container.register("command_registry", CommandRegistry, returns=CommandRegistry)
    container.set("ipc_service", object())

    register_service(container)

    assert isinstance(container.require("command_service"), CommandService)
    assert isinstance(container.require("ipc_request_service"), IpcRequestService)
    # 单例语义：同一个 key 取两次是同一个对象
    assert container.require("command_service") is container.require("command_service")

    # 迁移完成后（source / importer / asset / album / tag / thumbnail / view）
    # 每个域的 key 都必须真的可解析，否则等于"装配层声明了但没人注册"。
    from albuswall.service import Services

    for key in Services.__annotations__:
        assert key in container, f"{key} 未注册"
    assert "source_service" in container
    assert "thumbnail_service" in container


def test_modules_registers_every_domain(assembled):
    """装配后每个业务域都有自己的命令；旧命名（``thumb.*`` 等）不得复活。"""
    registry, _ = assembled
    names = set(registry.names(include_hidden=True, subject=frozenset({"admin"})))

    assert {"ping", "help", "parse", "version", "task.stats"} <= names
    assert {
        "source.list", "source.get", "source.create", "source.update",
        "source.delete", "source.enable", "source.disable",
        "source.refresh", "source.mount", "source.tasks",
        "source.scan", "source.stop",
    } <= names
    # 新迁入的五个域都要有入口（只查"每条命令都在"是不够的：漏注册整个模块
    # 时上面的断言照样通过）
    assert {
        "asset.list", "asset.get", "asset.favorite", "asset.trash",
        "asset.restore", "asset.add-to-album", "asset.tags", "asset.tag-add",
    } <= names
    assert {
        "album.list", "album.get", "album.create", "album.update",
        "album.rename", "album.cover", "album.reorder", "album.delete",
        "album.restore", "album.count", "album.assets",
    } <= names
    assert {
        "tag.list", "tag.get", "tag.create", "tag.rename", "tag.move",
        "tag.delete", "tag.restore", "tag.search", "tag.assets",
        "tag.alias-list", "tag.alias-create", "tag.alias-resolve",
        "tag.alias-delete",
    } <= names
    assert {
        "trash.list", "trash.count", "trash.state", "trash.restore",
        "trash.empty", "trash.cleanup", "trash.policy",
    } <= names
    assert {
        "thumbnail.stats", "thumbnail.paths", "thumbnail.request",
        "thumbnail.regenerate", "thumbnail.scan", "thumbnail.clean",
    } <= names
    # 退役命名不得复活（``thumb.*`` 那批是旧缩略图命令的命名，新域统一用
    # ``thumbnail.*``；``album.remove`` 是旧文档里的示例命名）
    assert {"thumb.stats", "thumb.missing", "album.remove"}.isdisjoint(names)
