#
"""Hello —— 最小可用的第一方插件。

它不是演示用的玩具：本文件放在 ``src/albuswall/plugins/`` 下，会被
``main._boot`` 的 ``discover_all(builtin_package="albuswall.plugins")`` 自动
扫描并 import，所以 ``python -m albuswall`` 真启动时它就参与完整生命周期。
删掉这个文件即可让内置插件目录回到空态。

它演示插件系统的五件事：

1. **声明**：模块顶层 ``@plugin("hello", version="1.0.0")`` —— 装饰时只往
   ``_pending`` 追加，工厂要到 ``setup`` 阶段才执行；
2. **注册服务**：工厂里 ``container.set("hello_service", …)``，任何拿到容器
   的代码都能 ``require("hello_service")`` 取到它；
3. **挂生命周期 hook**：boot / init / loop / exit 各一条（注意直接
   ``register("loop", …)`` 的钩子签名是 ``hook(app)``，零参包装只发生在
   ``ctx.on("loop")`` 里）；
4. **生命周期返回值**：返回 ``(start, stop)`` 二元组，由 ``PluginManager`` 在
   ``start_all`` / ``stop_all`` 阶段调用（顺序：setup → boot → init → start →
   loop → stop → exit）；
5. **读配置**：可选 ``[hello] greeting = …``（``@configclass`` 声明的
   ``HelloConf`` 类视图），缺省时用内置问候语。

两条边界（详见 ``docs/plugin-author-guide.md``）：

* 这里用的是 ``@plugin`` 工厂形态，工厂收到的是 **container**（不是
  ``PluginContext``），所以 hook 走 ``registry.register`` 直挂 —— 这类直挂
  **不受** setup 失败回滚管辖。需要「hook 可撤销 / 用 ``ctx.provide``」时，
  改用 ``BasePlugin`` 子类，在 ``setup(self, ctx)`` 里用 ``ctx.on(...)``。
* ``exit`` hook 里只用 ``container.get`` 读服务对象，不依赖插件是否还
  ``started``：``finally_`` 的顺序是**先 ``stop_all()`` 再跑 exit 钩子**。
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from albuswall.configue import configclass
from albuswall.core.container import Container
from albuswall.core.plugins import plugin
from albuswall.core.registry import register

#: 插件自己的 logger（继承 ``albuswall`` logger 的级别与 handler）。
_logger = logging.getLogger(__name__)

#: 没有配置时的默认问候语。
DEFAULT_GREETING = "Hello, AlbusWall!"

#: 配置段名 / key 名（``[hello] greeting``）。
CONFIG_SECTION = "hello"
CONFIG_KEY = "greeting"


# noinspection bad-assignment
@configclass(CONFIG_SECTION, "main")
class HelloConf:
    """``[hello]`` 段：插件自己的可选配置。

    插件模块是 ``discover_all`` 在 ``setup_config`` + ``load()`` **之后**才
    import 的，装饰晚于 ``load()`` 的 Step 4；``configclass`` 对「``Configue``
    已构造」的类会在装饰当下立即注入类型与默认值（``inject_raw(
    overwrite=False)``，文件里写了的值照样赢），所以这里可以放心写默认值。

    声明本身的价值：``(section, key)`` 进了声明表，``load(strict_undeclared
    =True)`` 不再把它当幽灵 key，拼错的 ``[hello] greetng`` 也读不到值。
    """

    greeting: str = DEFAULT_GREETING


class HelloService:
    """``hello_service``：一个能被别的插件/内置代码调用的最小服务。

    Attributes:
        greeting: 问候语（可能来自 ``[hello] greeting``）。
        started: 插件是否已 ``start``（``stop`` 后回到 ``False``）。
        calls: ``greet()`` 被调用过几次（测试与排障用）。
    """

    def __init__(self, greeting: str = DEFAULT_GREETING) -> None:
        self.greeting = greeting
        self.started = False
        self.calls = 0

    def greet(self, who: str = "") -> str:
        """返回一条问候；``who`` 非空时附在问候语后面。"""
        self.calls += 1
        return f"{self.greeting} {who}".strip()

    def __repr__(self) -> str:
        return (
            f"<HelloService greeting={self.greeting!r} "
            f"started={self.started} calls={self.calls}>"
        )


def _greeting_from_config() -> str:
    """读 ``[hello] greeting``；空白 / 缺值时退回默认问候语。

    走 :class:`HelloConf` 类视图（``@configclass`` 声明 + 默认值），而不是
    ``config.tree``：key 在声明期就注册，默认值由装饰器注入 Resolver（见
    ``configclass`` 的「装饰时机」说明）。

    ``getattr`` 只是防御性兜底，不是错误处理：``ConfigMissingError`` 继承
    ``AttributeError``，所以「没有 ``Configue`` 单例」/「节点被删掉」都会安静
    地落到默认值（单测里插件可能被单独加载，此时没有配置单例）。
    """
    value = getattr(HelloConf(), CONFIG_KEY, DEFAULT_GREETING)
    text = str(value or "").strip()
    return text or DEFAULT_GREETING


@plugin("hello", version="1.0.0")
def hello(container: Container) -> tuple[Callable[[], None], Callable[[], None]]:
    """工厂：注册服务 + 挂 hook，返回 ``(start, stop)``。

    工厂签名永远是 ``Callable[[Container], Any]`` —— 不需要 ctx 时这是最省的
    形态；需要 ctx（可回滚的 ``provide`` / ``on``）就用 ``BasePlugin``。
    """
    service = HelloService(_greeting_from_config())
    container.set("hello_service", service)

    # ---- hooks（setup 阶段就绪，boot/init 随后执行） ----
    def on_boot() -> None:
        _logger.info("[hello] boot: greeting=%r", service.greeting)

    def on_init() -> None:
        _logger.info("[hello] init: service ready (%r)", service)

    def on_loop(app: object) -> None:
        # 直挂的 loop hook 必须是 hook(app)：零参包装只在 ctx.on("loop") 里做。
        # （@plugin 工厂形态没有 ctx，所以这里自己接住 app。）
        _logger.info("[hello] loop: %s", service.greet("from the main loop"))

    def on_exit() -> None:
        # exit 钩子必须幂等：用 get 而不是 require。
        # 注意此时插件已经 stop 过（stop_all 先于 exit 钩子）。
        live = container.get("hello_service")
        if live is service:
            _logger.info("[hello] exit: good-bye (%r)", service)
        else:
            _logger.warning("[hello] exit: hello_service is gone")

    register("boot", on_boot)
    register("init", on_init)
    register("loop", on_loop)
    register("exit", on_exit)

    # ---- start / stop ----
    def start() -> None:
        service.started = True
        _logger.info("[hello] start: %s", service.greeting)

    def stop() -> None:
        service.started = False
        _logger.info("[hello] stop: %s (%d greeting(s) served)",
                     service.greeting, service.calls)

    return start, stop
