#
""""""

from typing import TYPE_CHECKING, Generic, TypeVar, Callable

__all__ = ["Lazy"]

if TYPE_CHECKING:
    T = TypeVar("T")


    # noinspection SpellCheckingInspection
    class Lazy(Generic[T]):
        def __init__(self, factory: Callable[[], T]) -> None: ...

        def __get__(self, obj, objtype=None) -> T: ...
else:
    class Lazy:
        """延迟初始化的描述符。

        首次访问时若实例尚未设置该属性，则调用 factory() 生成默认值并缓存。
        factory 可以是任意可调用对象，比如 object / list / dict / 自定义函数。
        """

        def __init__(self, factory):
            if not callable(factory):
                raise TypeError(f"factory must be a callable object, got{factory!r}")
            self.factory = factory
            self.name = None

        def __set_name__(self, owner, name):
            # Python 3.6+ 自动传入属性名，无需手写 name 参数
            self.name = name

        def __get__(self, obj, objtype=None):
            if obj is None:
                # 通过类访问：Foo.attr 返回描述符本身
                return self
            # 能进到这里，说明 obj.__dict__ 里还没有这个属性（非数据描述符优先级低于实例字典）
            value = self.factory()
            setattr(obj, self.name, value)  # 缓存到实例字典，之后访问就不再走 __get__
            return value
