#
""""""

class _Unset:
    __slots__ = ()

    def __repr__(self):
        return "<UNSET>"

    def __bool__(self):
        return False


UNSET = _Unset()


# noinspection string-conversion-without-dunder-method,SpellCheckingInspection
class ObservableProperty:
    """和内置 property 用法一致，额外支持变更后回调。"""

    def __init__(self, fget=None, fset=None, fdel=None, doc=None):
        self.fget = fget
        self.fset = fset
        self.fdel = fdel
        if doc is None:
            doc = getattr(fget, "__doc__", None)
        self.__doc__ = doc
        self._callbacks = []
        self._name = getattr(fget, "__name__", None)

    def __set_name__(self, owner, name):
        self._name = name

    def __get__(self, obj, objtype=None):
        if obj is None:
            return self
        if self.fget is None:
            raise AttributeError(f"属性 {self._name!r} 不可读")
        return self.fget(obj)

    def __set__(self, obj, value):
        if self.fset is None:
            raise AttributeError(f"属性 {self._name!r} 不可写")

        old = UNSET
        if self.fget is not None:
            # noinspection broad-exception
            try:
                old = self.fget(obj)
            except Exception:
                old = UNSET

        self.fset(obj, value)
        self._fire(obj, value, old)

    def __delete__(self, obj):
        if self.fdel is None:
            raise AttributeError(f"属性 {self._name!r} 不可删")
        self.fdel(obj)

    def getter(self, fget):
        return self._spawn(fget, self.fset, self.fdel)

    def setter(self, fset):
        return self._spawn(self.fget, fset, self.fdel)

    def deleter(self, fdel):
        return self._spawn(self.fget, self.fset, fdel)

    def call(self, func):
        """注册变更回调，签名 func(instance, new_value, old_value)。"""
        self._callbacks.append(func)
        return self

    def _spawn(self, fget, fset, fdel):
        new = type(self)(fget, fset, fdel, self.__doc__)
        new._callbacks = self._callbacks
        new._name = self._name
        return new

    def _fire(self, obj, new_value, old_value):
        for cb in self._callbacks:
            cb(obj, new_value, old_value)

observable_property = ObservableProperty
