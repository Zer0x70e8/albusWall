#
""""""

import inspect
from typing import Callable, Any, Optional


def factory_repr(func: Callable, returns: Optional[Any] = None) -> str:
    """生成 'func_name -> Type' 形式的简短描述。

    returns 优先；没有时回退到函数注解；都没有则为 '?'。
    """
    name = getattr(func, "__name__", repr(func))

    if returns is not None:
        # noinspection string-conversion-without-dunder-method
        ret_name = getattr(returns, "__name__", str(returns))
        return f"{name} -> {ret_name}"

    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError):
        return f"{name} -> ?"

    ret = sig.return_annotation
    if ret is inspect.Signature.empty:
        ret_name = "?"
    elif isinstance(ret, type):
        ret_name = ret.__name__
    else:
        ret_name = str(ret).replace("typing.", "")

    return f"{name} -> {ret_name}"
