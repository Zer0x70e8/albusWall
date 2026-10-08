#
"""信号转接器（SignalAdapter）。

统一承接项目里几种异质的信号：

  1. **Qt 信号** —— ``signal.connect(slot)`` / ``signal.emit(*args)``，
     槽按位置参数接收。
  2. **项目自有 Signal** —— ``send(sender, **kwargs)``，回调签名
     ``cb(sender, **kwargs)``（见 signals.py）。
  3. **跨线程桥接** —— ``queued=True`` 时后台线程触发 → 主线程执行槽。

设计目标
--------
· 单一 API 覆盖三种场景，调用方不用感知底层差异；
· 连接幂等、可断开、可集中 teardown；
· 参数变换 / 过滤 / 排队全部声明式，不写中转函数；
· 不侵入既有信号实现（``signals.Signal`` 与 Qt 信号本体不动）。

**注意**：本类刻意**不**继承 ``QObject`` —— 我们自定义了 ``connect`` /
``disconnect``，与 ``QObject`` 上的同名方法签名不兼容，继承会触发
静态检查告警。需要父子关系时，把 ``parent`` 传给构造函数即可，内部
的跨线程中继会自动挂到该 parent 下。

槽签名速查
----------
::

    ┌──────────────────┬────────────────┬───────────────────────────┐
    │ source 类型      │ keep_sender    │ 槽签名                    │
    ├──────────────────┼────────────────┼───────────────────────────┤
    │ Qt 信号          │  —             │ def slot(*args)           │
    │ 自有 Signal      │  False（默认） │ def slot(**kwargs)        │
    │ 自有 Signal      │  True          │ def slot(sender, **kwargs)│
    └──────────────────┴────────────────┴───────────────────────────┘

  即：Qt 信号的参数按位置传入；自有 Signal 默认丢弃 sender，
  只有显式 ``keep_sender=True`` 时把 sender 放在 ``args[0]``。

约定
----
· ``transform`` 接收 ``(args: tuple, kwargs: dict)``，返回同形状二元组。
· ``predicate`` 以解包后的 ``*args, **kwargs`` 调用，返回 False 表示丢弃。
· 自有 ``Signal`` 的 ``sender`` 默认被丢掉（``keep_sender=True`` 可保留）。

生命周期
--------
``SignalAdapter`` 持有所有 ``Connection``；在 owner 销毁前调用
:meth:`disconnect_all` 释放。不调用会残留连接，直到 source 被 GC。

典型用法::

    class Foo(QObject):
        def __init__(self, service, trash, ...):
            super().__init__()
            self._adapter = SignalAdapter(self)

            # 同线程直连
            self._adapter.connect(
                service.scan_finished, self._on_scan_finished,
            )

            # 后台 → 主线程桥接：queued=True 走 QueuedConnection
            self._adapter.connect(
                trash.sources_changed, self._on_sources_changed,
                queued=True,
                predicate=lambda **kw: kw.get("kind") != "purged",
            )

            # 转发到另一个信号
            self._adapter.forward(
                source.raw_signal, self._some_queued_signal,
            )

        def teardown(self):
            self._adapter.disconnect_all()
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Literal, Optional

from PySide6.QtCore import QObject, Qt, Signal as QtSignal

# 用户一处 import 就能拿到全部信号类型
from albuswall.utils.signals import Signal, ProcessSignal  # noqa: F401

_logger = logging.getLogger(__name__)

__all__ = [
    "SignalAdapter",
    "Connection",
    "Signal",
    "ProcessSignal",
]

SignalKind = Literal["qt", "custom", "unknown"]


# --------------------------------------------------------------------------- #
# 内部：跨线程投递载荷
# --------------------------------------------------------------------------- #
@dataclass(slots=True, frozen=True)
class _Payload:
    args: tuple
    kwargs: dict


# --------------------------------------------------------------------------- #
# 内部：排队中继
# --------------------------------------------------------------------------- #
# noinspection broad-exception
class _Relay(QObject):
    """把 ``_Payload`` 从任意线程排队投递到 relay 所在线程执行。

    · ``post``           —— 从发射者线程调用；
    · ``_on_dispatched`` —— 在 relay 的线程（一般是主线程）里执行 sink。
    """

    dispatched = QtSignal(object)

    def __init__(
            self,
            sink: Callable[[_Payload], None],
            parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._sink = sink
        self.dispatched.connect(
            self._on_dispatched, Qt.ConnectionType.QueuedConnection,
        )

    def _on_dispatched(self, payload: _Payload) -> None:
        try:
            self._sink(payload)
        except Exception:  # noqa: BLE001
            _logger.exception("SignalAdapter queued sink raised")

    def post(self, payload: _Payload) -> None:
        self.dispatched.emit(payload)


# --------------------------------------------------------------------------- #
# 信号风格探测
# --------------------------------------------------------------------------- #
def _signal_kind(sig: Any) -> SignalKind:
    """返回 ``'qt' | 'custom' | 'unknown'``。

    判据：项目自有的 ``Signal`` / ``ProcessSignal`` 都提供 ``send`` 方法
    （``emit`` 只是 alias）；Qt 信号只有 ``emit``，没有 ``send``。
    """
    if not (hasattr(sig, "connect") and hasattr(sig, "disconnect")):
        return "unknown"
    if callable(getattr(sig, "send", None)):
        return "custom"
    if callable(getattr(sig, "emit", None)):
        return "qt"
    return "unknown"


def _describe_supported() -> str:
    return (
        "source / target 必须是以下之一：\n"
        "  · Qt 信号（具有 connect / disconnect / emit）\n"
        "  · 自有 Signal / ProcessSignal（具有 connect / disconnect / send）"
    )


# --------------------------------------------------------------------------- #
# 连接句柄
# --------------------------------------------------------------------------- #
class Connection:
    """由 :class:`SignalAdapter` 返回的连接句柄。

    推荐用法：持有句柄，通过 :meth:`disconnect` 断开。
    相比 ``Signal.disconnect(callback)``，无需记住原 callback。
    """

    __slots__ = ("_source", "_handler", "_relay", "_alive", "_source_repr")

    def __init__(
            self,
            source: Any,
            handler: Any,
            relay: Optional[_Relay] = None,
    ) -> None:
        self._source = source
        self._handler = handler
        self._relay = relay
        self._alive = True
        # 保留 source 的 repr 快照，断开后仍能用于调试输出
        self._source_repr = repr(source)

    @property
    def alive(self) -> bool:
        return self._alive

    def disconnect(self) -> None:
        """断开本连接。幂等。"""
        if not self._alive:
            return
        self._alive = False
        try:
            self._source.disconnect(self._handler)
        except (RuntimeError, TypeError) as exc:
            _logger.debug("adapter disconnect ignored: %s", exc)
        self._source = None
        self._handler = None
        self._relay = None

    def __repr__(self) -> str:
        return (
            f"Connection(source={self._source_repr}, "
            f"alive={self._alive})"
        )


# --------------------------------------------------------------------------- #
# 主类
# --------------------------------------------------------------------------- #
# noinspection broad-exception
class SignalAdapter:
    """信号转接器。见模块 docstring。"""

    def __init__(self, parent: Optional[QObject] = None) -> None:
        # parent 只用于给内部的 _Relay 挂父子关系；本类自身不是 QObject。
        self._parent = parent
        self._connections: list[Connection] = []

    # ------------------------------------------------------------------ #
    # 公开 API
    # ------------------------------------------------------------------ #
    def connect(
            self,
            source: Any,
            slot: Callable[..., Any],
            *,
            transform: Optional[Callable[[tuple, dict], tuple[tuple, dict]]] = None,
            predicate: Optional[Callable[..., bool]] = None,
            queued: bool = False,
            keep_sender: bool = False,
    ) -> Connection:
        """把 ``source`` 的触发路由到 ``slot``。

        Args:
            source: 信号对象（Qt 信号或自有 ``Signal`` / ``ProcessSignal``）。
            slot:   接收端可调用对象。槽签名见模块 docstring 的"速查表"。
            transform: 参数变换 —— ``(args, kwargs) -> (args, kwargs)``。
            predicate: ``(*args, **kwargs) -> bool``；返回 False 丢弃本次触发。
            queued:   True 时切到接收端线程执行（后台 → 主线程桥接）。
            keep_sender: 仅对自有 Signal 有效；True 时把 sender 放进 args[0]。

        Returns:
            Connection —— 可传给 :meth:`disconnect` 单独断开。

        Raises:
            TypeError: source 不是受支持的信号对象。
        """
        kind = _signal_kind(source)
        if kind == "unknown":
            raise TypeError(
                f"SignalAdapter.connect 不支持该 source: {source!r}\n"
                f"  当前类型: {type(source).__name__}\n"
                + _describe_supported()
            )

        invoke = self._build_invoker(slot, transform, predicate)
        handler, relay = self._make_handler(
            kind, invoke, queued=queued, keep_sender=keep_sender,
        )

        # 直接让它抛：connect 失败就是编程错误，不该被吞掉。
        source.connect(handler)

        conn = Connection(source, handler, relay)
        self._connections.append(conn)
        return conn

    def forward(
            self,
            source: Any,
            target: Any,
            *,
            transform: Optional[Callable[[tuple, dict], tuple[tuple, dict]]] = None,
            predicate: Optional[Callable[..., bool]] = None,
            queued: bool = False,
            keep_sender: bool = False,
    ) -> Connection:
        """把 ``source`` 的触发透传到另一个信号 ``target``。

        自动适配目标信号风格：
        · Qt 目标    → ``target.emit(*args, **kwargs)``；
        · 自有 Signal → ``target.send(sender, **kwargs)``。
        """
        target_kind = _signal_kind(target)
        if target_kind == "unknown":
            raise TypeError(
                f"SignalAdapter.forward 不支持该 target: {target!r}\n"
                f"  当前类型: {type(target).__name__}\n"
                + _describe_supported()
            )

        def _re_emit(*args, **kwargs):
            if target_kind == "custom":
                sender = args[0] if args else None
                target.send(sender, **kwargs)
            else:
                target.emit(*args, **kwargs)

        return self.connect(
            source, _re_emit,
            transform=transform, predicate=predicate,
            queued=queued, keep_sender=keep_sender,
        )

    def disconnect(self, conn: Connection) -> None:
        """断开单条连接。幂等。"""
        try:
            self._connections.remove(conn)
        except ValueError:
            pass
        conn.disconnect()

    def disconnect_all(self) -> None:
        """一次性断开所有连接（teardown 时调用）。幂等。"""
        conns, self._connections = self._connections, []
        for c in conns:
            c.disconnect()

    # ------------------------------------------------------------------ #
    # 内部：invoker —— predicate → transform → slot 的统一入口
    # ------------------------------------------------------------------ #
    @staticmethod
    def _build_invoker(
            slot: Callable[..., Any],
            transform: Optional[Callable[[tuple, dict], tuple[tuple, dict]]],
            predicate: Optional[Callable[..., bool]],
    ) -> Callable[[tuple, dict], None]:

        def invoke(args: tuple, kwargs: dict) -> None:
            if predicate is not None:
                # 用户提供的 predicate 可能抛任何异常；吞掉并丢弃本次触发
                try:
                    if not predicate(*args, **kwargs):
                        return
                except Exception:  # noqa: BLE001
                    _logger.exception("SignalAdapter predicate raised")
                    return

            if transform is not None:
                try:
                    args, kwargs = transform(args, kwargs)
                except Exception:  # noqa: BLE001
                    _logger.exception("SignalAdapter transform raised")
                    return

            try:
                slot(*args, **kwargs)
            except Exception:  # noqa: BLE001
                _logger.exception("SignalAdapter slot raised")

        return invoke

    # ------------------------------------------------------------------ #
    # 内部：handler 工厂 —— (kind, queued) 两个正交维度的唯一实现
    # ------------------------------------------------------------------ #
    def _make_handler(
            self,
            kind: SignalKind,
            invoke: Callable[[tuple, dict], None],
            *,
            queued: bool,
            keep_sender: bool,
    ) -> tuple[Callable[..., Any], Optional[_Relay]]:
        """按 (kind, queued) 生成 ``(handler, relay)``。

        - Qt 信号：位置参数 → args；kwargs 恒为空。
        - 自有信号：sender 是否进入 args 由 ``keep_sender`` 决定。
        - ``queued=True`` 时，实际调用通过 ``_Relay`` 排队到接收端线程。
        """
        relay = (
            _Relay(sink=lambda p: invoke(p.args, p.kwargs), parent=self._parent)
            if queued else None
        )

        def _dispatch(args: tuple, kwargs: dict) -> None:
            if relay is not None:
                relay.post(_Payload(args, kwargs))
            else:
                invoke(args, kwargs)

        if kind == "qt":
            def handler(*args, **kwargs):
                _dispatch(args, kwargs)
        else:  # custom
            def handler(sender, **kwargs):
                _dispatch((sender,) if keep_sender else (), kwargs)

        return handler, relay

    # ------------------------------------------------------------------ #
    # 调试
    # ------------------------------------------------------------------ #
    def __len__(self) -> int:
        return len(self._connections)

    def __str__(self) -> str:
        lines = [f"{type(self).__name__}({len(self._connections)} conns)"]
        for c in self._connections:
            lines.append(f"\t{c!r}")
        return "\n".join(lines)
