#
"""CommandService —— 命令服务门面：协议解析 + 委托 Dispatcher。"""

from __future__ import annotations

import json
from typing import Any, Iterable

from albuswall.command import CommandRegistry
from albuswall.log import getLogger

from .dispatcher import Dispatcher
from .exceptions import BadRequest

logger = getLogger(__name__)


# ==================== 命令协议解析（与传输无关） ====================

def split_params(params: Any) -> tuple[list, dict]:
    """str / None / list / tuple / dict -> (args, kwargs)。"""
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except json.JSONDecodeError as e:
            raise BadRequest(f"invalid JSON params: {e}") from e

    if params is None:
        return [], {}
    if isinstance(params, (list, tuple)):
        return list(params), {}
    if not isinstance(params, dict):
        raise BadRequest(
            f"params must be dict/list/tuple/None/str, "
            f"got {type(params).__name__}"
        )

    if "args" in params or "kwargs" in params:
        raw_args = params.get("args") or []
        raw_kwargs = params.get("kwargs") or {}
        if not isinstance(raw_args, (list, tuple)):
            raise BadRequest("params.args must be list/tuple")
        if not isinstance(raw_kwargs, dict):
            raise BadRequest("params.kwargs must be dict")
        extra = {k: v for k, v in params.items()
                 if k not in ("args", "kwargs")}
        kwargs = dict(raw_kwargs)
        kwargs.update(extra)
        return list(raw_args), kwargs

    return [], dict(params)


def _extract_subject(req: dict) -> frozenset[str] | None:
    if "subject" not in req:
        return None
    raw = req["subject"]
    if raw is None:
        return frozenset()
    if not isinstance(raw, (list, tuple, set, frozenset)):
        raise BadRequest("'subject' must be a sequence of strings")
    try:
        return frozenset(raw)
    except TypeError as e:
        raise BadRequest(f"invalid 'subject': {e}") from e


# ==================== 门面 ====================

class CommandService:
    """命令服务的唯一业务入口。

    - ``execute(path, args, kwargs)``         已解析参数，供内部/测试直调
    - ``execute_request({method, params})``   协议 dict，CLI / IPC / GUI 共用

    只抛 ``DispatchError`` 子类（含 ``BadRequest``）。调用方 except 一棵树即可。
    """

    def __init__(
        self,
        registry: CommandRegistry,
        *,
        subject_provider=None,
    ) -> None:
        self.registry = registry
        self.dispatcher = Dispatcher(registry, subject_provider=subject_provider)

    def execute(
        self,
        path: str,
        args: Iterable[Any] = (),
        kwargs: dict[str, Any] | None = None,
        *,
        subject: frozenset[str] | None = None,
    ) -> Any:
        return self.dispatcher.dispatch(
            path, list(args), dict(kwargs or {}), subject=subject,
        )

    def execute_request(self, req: dict) -> Any:
        if not isinstance(req, dict):
            raise BadRequest("request must be a dict")
        method = req.get("method")
        if not isinstance(method, str) or not method:
            raise BadRequest("missing or invalid 'method'")
        args, kwargs = split_params(req.get("params"))
        subject = _extract_subject(req)
        return self.dispatcher.dispatch(method, args, kwargs, subject=subject)

