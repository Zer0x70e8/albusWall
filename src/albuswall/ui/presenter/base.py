#
"""Presenter 基类 —— UI 表现层的公共骨架。

为什么要有 presenter
====================

``albuswall.ui.window`` / ``albuswall.ui.widgets`` 是**纯视图**：它们只发意图
信号、只暴露 ``set_model`` 这类直接接口，既不读配置、也不认识 service / DTO。
把「窗口 + 数据 + 服务」接在一起的那一层就是 presenter：

===============  ==================================================
层                职责
===============  ==================================================
window/widgets    画界面、发意图信号（不认识配置 / 服务）
presenter         读配置、调服务、把结果写回视图（**本包**）
service           业务规则（不认识任何 Qt 类型）
===============  ==================================================

两级结构
========

:class:`~albuswall.ui.presenter.main.MainPresenter` 是**唯一的根**，其余
presenter（相册页、详情页、导入源页……）都是它的子节点。子节点不自己管理
启动 / 停止：父节点 ``start()`` / ``stop()`` 时按登记顺序级联，退出顺序与
启动顺序相反（先起上游、先停下游）。

子 presenter **不在 import 期实例化**，而是登记一个工厂
（:func:`register_presenter`，签名 ``factory(container, parent) -> Presenter``），
由根 presenter 在启动时用容器构造 —— 这样每个 presenter 都能拿到完整的依赖
注入，而不用互相 import。

生命周期约定
============

* ``start()``：先 ``on_start()``，再按登记顺序启动子节点；重复调用是 no-op。
* ``stop()``：先按**逆序**停止子节点，再 ``on_stop()``；重复调用是 no-op。
* ``on_start()`` / ``on_stop()`` 里抛异常：``start()`` 回滚已启动的子节点后
  把异常原样抛出（启动失败不留下半死不活的树），``stop()`` 只记日志不抛
  （退出路径不允许崩）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, ClassVar

from PySide6.QtCore import QObject

if TYPE_CHECKING:
    from albuswall.core.container import Container

__all__ = [
    "PRESENTER_FACTORIES",
    "Presenter",
    "PresenterFactory",
    "register_presenter",
    "registered_presenters",
]

logger = logging.getLogger(__package__)


class Presenter(QObject):
    """所有 presenter 的基类：父子关系 + 幂等生命周期。

    ``name`` 在**同一父节点下**必须唯一（父子定位、去重、日志都用它）。

    继承 ``QObject`` 是为了让子类能声明 Qt 信号（presenter 之间的跨对象
    通信全部走信号，由 :class:`~albuswall.ui.presenter.main.MainPresenter`
    统一接线）。构造发生在 GUI 线程，因此信号默认就是主线程语义。
    """

    #: 子节点定位用的名字；子类必须覆盖。
    name: ClassVar[str] = "presenter"

    def __init__(
            self,
            container: Container,
            *,
            parent: Presenter | None = None,
    ) -> None:
        super().__init__(parent)
        self._container = container
        self._parent: Presenter | None = None
        self._children: dict[str, Presenter] = {}
        self._started = False
        #: 启动过程中（``on_start`` 里挂上的子节点由父的启动循环接手，
        #: 不在 ``add_child`` 里立刻启动 —— 那时子节点的构造还没完成）。
        self._starting = False
        if parent is not None:
            parent.add_child(self)

    # ------------------------------------------------------------------ #
    # 依赖
    # ------------------------------------------------------------------ #
    @property
    def container(self) -> Container:
        """构造期注入的依赖容器（**唯一**取 service 的入口）。"""
        return self._container

    def service(self, name: str) -> Any:
        """从容器取一个服务；未注册返回 ``None``。

        用 ``get`` 而不是 ``require``：presenter 树在 headless / 单测 /
        插件裁剪的环境里也要能构造出来，缺服务时该功能退化为空操作，
        由各 presenter 自己记警告。
        """
        try:
            return self._container.get(name)
        except Exception:  # noqa: BLE001 - 取依赖失败不该炸装配
            logger.exception("presenter: cannot resolve dependency %r", name)
            return None

    # ------------------------------------------------------------------ #
    # 父子关系
    # ------------------------------------------------------------------ #
    @property
    def parent(self) -> Presenter | None:
        return self._parent

    @property
    def children(self) -> tuple[Presenter, ...]:
        """直接子节点（按登记顺序）。"""
        return tuple(self._children.values())

    def child(self, name: str) -> Presenter | None:
        """按名字取直接子节点；不存在返回 ``None``。"""
        return self._children.get(name)

    def require_child(self, name: str) -> Presenter:
        """按名字取直接子节点；不存在抛 ``KeyError``。"""
        child = self._children.get(name)
        if child is None:
            raise KeyError(f"{type(self).__name__!r} has no child {name!r}")
        return child

    def add_child(self, child: Presenter) -> Presenter:
        """挂载子节点并返回它。

        * 父节点已启动时，立即启动子节点（热插拔）；
        * 同一对象重复挂载是 no-op；同名不同对象抛 ``ValueError``；
        * 已经有别的父节点时抛 ``ValueError``（presenter 树是单亲结构）。
        """
        if not isinstance(child, Presenter):
            raise TypeError(
                f"child must be a Presenter, got {type(child).__name__}")
        if child is self:
            raise ValueError("a presenter cannot be its own child")
        owner = child.parent
        if owner is not None and owner is not self:
            raise ValueError(
                f"presenter {child.name!r} already belongs to {owner.name!r}")
        existing = self._children.get(child.name)
        if existing is child:
            return child
        if existing is not None:
            raise ValueError(
                f"duplicate presenter name {child.name!r} under {self.name!r}")
        child._parent = self
        self._children[child.name] = child
        logger.debug(
            "presenter: %r attached to %r", child.name, self.name)
        # 启动中挂上的子节点由父的启动循环负责启动：此刻子节点的构造还没
        # 完成（``parent=`` 在基类 ``__init__`` 里就挂了），提前 start 会让
        # 它的 ``on_start`` 读到还没赋值的字段。
        if self._started and not self._starting:
            child.start()
        return child

    def remove_child(self, name: str) -> Presenter | None:
        """卸载子节点（先停后摘）；不存在返回 ``None``。"""
        child = self._children.pop(name, None)
        if child is None:
            return None
        try:
            child.stop()
        finally:
            child._parent = None
        logger.debug("presenter: %r detached from %r", name, self.name)
        return child

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    @property
    def is_started(self) -> bool:
        return self._started

    def start(self) -> None:
        """幂等启动：``on_start()`` → 子节点（登记顺序）。"""
        if self._started:
            return
        self._starting = True
        try:
            self.on_start()
            for child in tuple(self._children.values()):
                child.start()
        except BaseException:
            # 启动失败不留半棵树：把已启动的子节点收回去，再原样抛出。
            self._started = False
            self._stop_children()
            raise
        finally:
            self._starting = False
        self._started = True
        logger.debug("presenter: %r started", self.name)

    def stop(self) -> None:
        """幂等停止：子节点（**逆序**）→ ``on_stop()``。"""
        if not self._started:
            return
        try:
            self._stop_children()
            try:
                self.on_stop()
            except Exception:  # noqa: BLE001 - 退出路径不允许崩
                logger.exception(
                    "presenter: error while stopping %r", self.name)
        finally:
            self._started = False
        logger.debug("presenter: %r stopped", self.name)

    def on_start(self) -> None:
        """子类扩展点：启动自身（此时容器 / 视图已就绪）。"""

    def on_stop(self) -> None:
        """子类扩展点：释放自身持有的资源（退出路径，不要抛异常）。"""

    def retranslate(self) -> None:
        """子类扩展点：语言切换后重算 presenter 自己持有的文案 / 数据。

        视图控件由 Qt 的 ``LanguageChange`` 广播自动刷新；presenter 不是
        控件，收不到那条事件，因此由主 presenter 显式调用本方法。
        """

    def _stop_children(self) -> None:
        for child in reversed(tuple(self._children.values())):
            try:
                child.stop()
            except Exception:  # noqa: BLE001 - 退出路径不允许崩
                logger.exception(
                    "presenter: error while stopping %r", child.name)

    def __repr__(self) -> str:
        state = "started" if self._started else "stopped"
        return f"<{type(self).__name__} {self.name!r} {state}>"


#: 子 presenter 工厂：``factory(container, parent) -> Presenter``。
PresenterFactory = Callable[..., Presenter]

#: 已登记的子 presenter 工厂（**主 presenter 启动时按此顺序构造**）。
#:
#: 用列表而不是元组：presenter 各自在自己的模块里 import 期登记，
#: 主 presenter 只负责消费，不 import 任何具体 presenter（避免循环 import）。
PRESENTER_FACTORIES: list[PresenterFactory] = []


def register_presenter(factory: PresenterFactory) -> PresenterFactory:
    """登记子 presenter 工厂，返回入参（可作装饰器使用）。

    工厂在**主 presenter 启动时**才被调用，因此：

    * 里面可以放心 ``container.require(...)`` 取服务；
    * 不要在里面读配置 / 建控件之外做重活，``start()`` 之前视图还没 show。

    重复登记同一个工厂只入队一次（幂等）。
    """
    if factory not in PRESENTER_FACTORIES:
        PRESENTER_FACTORIES.append(factory)
    return factory


def registered_presenters() -> tuple[PresenterFactory, ...]:
    """已登记工厂的快照（启动顺序）。"""
    return tuple(PRESENTER_FACTORIES)
