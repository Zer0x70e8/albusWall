#
"""命令执行层：``task`` 命名空间的参数绑定回归测试。

这里守的是 ``Param`` 声明与 ``Dispatcher._bind`` 之间的契约：

* ``_bind`` **不读** handler 的签名，缺参时填的是 ``Param.default``；
* 所以位置参数可选值必须写成 ``Param(..., default=...)``，签名里的
  ``subcmd: str = ""`` 只在直接调用函数时生效。

写成 ``required=False, default=None``（即漏写 default）时，缺参会让 handler
收到 ``None``，在 ``subcmd.strip()`` 处抛 ``AttributeError`` —— 对外表现为
``CommandFailed``（"命令执行失败"），而不是可预期的"参数缺失"。本文件的
用例同时锁住这两点：装配期不再有 ``UserWarning``，且裸 ``task`` 可执行。
"""

from __future__ import annotations

import warnings

import pytest

from albuswall.service.command.dispatcher import Dispatcher
from albuswall.service.command.handlers import setup as setup_handlers


def _warn_messages(records) -> list[str]:
    return [str(record.message) for record in records]


# ==================== 装配期：声明必须自洽 ====================

def test_assembly_emits_no_positional_param_warning(fresh_registry, fake_container):
    """装配所有 handler 不得产生「positional param with no default」警告。

    ``Param.__post_init__`` 在装饰器执行时就跑，所以这里包住 ``setup`` 即可
    复现启动期的那行 stderr。
    """
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        setup_handlers(fresh_registry, fake_container)

    offenders = [
        msg for msg in _warn_messages(caught)
        if "positional param with no default" in msg
    ]
    assert not offenders, f"装配期仍有参数声明问题: {offenders}"


def test_task_subcmd_param_declares_default(assembled):
    """``task`` 的 ``subcmd`` 必须带非 None 默认值（而不是靠函数签名兜底）。"""
    registry, _ = assembled
    cmd = registry.resolve("task")
    assert cmd is not None and cmd.func is not None

    subcmd = next(p for p in cmd.params if p.name == "subcmd")
    assert subcmd.kind == "pos"
    assert subcmd.required is False
    assert subcmd.default == ""


# ==================== 执行期：缺参不再崩 ====================

def test_task_without_subcmd_returns_help(assembled):
    """裸 ``task`` 打印帮助，而不是把 ``None`` 塞给 ``subcmd.strip()``。"""
    registry, _ = assembled
    result = Dispatcher(registry).dispatch("task", [], {})

    assert isinstance(result, str)
    assert "help task" in result


def test_task_unknown_subcmd_is_reported_not_raised(assembled):
    """未知子命令返回提示字符串（handler 自己兜底），不抛 CommandFailed。"""
    registry, _ = assembled
    result = Dispatcher(registry).dispatch("task", ["nope"], {})

    assert result == "Unknown subcommand: nope"


def test_task_stats_returns_mapping(assembled):
    """``task`` 的子命令分发仍工作，并且能拿到 stub service 的数据。"""
    registry, _ = assembled
    result = Dispatcher(registry).dispatch("task.stats", [], {})

    assert result["queue_size"] == 7
    assert result["global_concurrency_max"] == 8
    assert result["stats"]["submitted"] == 42


@pytest.mark.parametrize("args", ([], ["stats"]))
def test_task_binding_never_passes_none(assembled, args):
    """绑定结果里位置实参必须是 str —— ``None`` 正是本 bug 的根源。"""
    registry, _ = assembled
    cmd = registry.resolve("task")
    bound_args, _ = Dispatcher(registry)._bind(cmd, list(args), {})

    assert bound_args and isinstance(bound_args[0], str)
