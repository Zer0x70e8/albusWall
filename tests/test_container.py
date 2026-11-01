#
"""``Container`` 的注册语义回归。

守三条边界：

* **last-wins**：后注册覆盖先注册（没有 ``override`` 参数），覆盖只记 debug；
* ``_wants_container`` 的「形参名无意义」陷阱：非 ``type`` 的 callable 只要
  有必填位置参数，收到的就是 ``Container`` 本身——所以 ``def make(config)``
  拿到的**不是** config；
* ``has_instance`` 只反映「有没有缓存实例」：``set`` 立即缓存，
  ``singleton=False`` 永远不缓存。
"""

from __future__ import annotations

import logging

from albuswall.core.container import Container

# ==================== last-wins ====================

def test_register_is_last_wins():
    """连续注册同名依赖，``require`` 返回最后一次的值。"""
    container = Container()
    container.register("answer", lambda: 1)
    assert container.require("answer") == 1

    container.register("answer", lambda: 2)
    assert container.require("answer") == 2


def test_set_is_last_wins():
    container = Container()
    container.set("answer", 1)
    container.set("answer", 2)
    assert container.require("answer") == 2


def test_override_logs_debug(caplog):
    """覆盖时打一条 ``overriding dependency``（只 debug，不 warning）。"""
    container = Container()
    container.register("svc", lambda: object())
    with caplog.at_level(logging.DEBUG, logger="albuswall.core"):
        container.register("svc", lambda: object())

    messages = [record.getMessage() for record in caplog.records]
    assert any("overriding dependency 'svc'" in msg for msg in messages), messages
    assert all(record.levelno == logging.DEBUG for record in caplog.records
               if "overriding dependency" in record.getMessage())


def test_override_invalidates_cached_instance():
    """覆盖后旧缓存作废：再次 ``require`` 要拿到新工厂的结果。"""
    container = Container()
    container.register("svc", lambda: "first")
    assert container.require("svc") == "first"

    container.register("svc", lambda: "second")
    assert container.require("svc") == "second"


# ==================== _wants_container 陷阱 ====================

def test_factory_with_positional_param_receives_container():
    """``def make(config)`` 的形参名毫无意义：收到的一定是 ``Container``。"""
    container = Container()
    received = []

    def make(config):
        received.append(config)
        return "value"

    container.register("thing", make)
    assert container.require("thing") == "value"
    assert received == [container]


def test_zero_arg_factory_receives_nothing():
    container = Container()
    called = []

    def make():
        called.append(True)
        return "value"

    container.register("thing", make)
    assert container.require("thing") == "value"
    assert called == [True]


def test_class_factory_is_constructed_without_injection():
    """``type`` 永远零参构造，即使 ``__init__`` 有形参也不注入。"""
    container = Container()

    class Service:
        def __init__(self) -> None:
            self.ready = True

    container.register("service", Service)
    assert container.require("service").ready is True


# ==================== has_instance ====================

def test_has_instance_true_after_set():
    container = Container()
    container.set("answer", 42)
    assert container.has_instance("answer") is True


def test_has_instance_false_for_unregistered():
    container = Container()
    assert container.has_instance("nope") is False


def test_has_instance_false_for_non_singleton():
    """``singleton=False``：解析过了也没有缓存，``has_instance`` 恒为 False。"""
    container = Container()
    container.register("fresh", lambda: object(), singleton=False)
    container.require("fresh")
    assert container.has_instance("fresh") is False


def test_has_instance_true_for_singleton_after_require():
    container = Container()
    container.register("once", lambda: object())
    container.require("once")
    assert container.has_instance("once") is True
