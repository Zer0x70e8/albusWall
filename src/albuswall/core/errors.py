#
""""""

__all__ = ["MissingDependencyError"]


class MissingDependencyError(KeyError):
    """请求的依赖没有注册。

    继承 ``KeyError`` 以便 ``except KeyError`` 也能捕获，同时用精确类型
    区分「容器缺依赖」和「普通字典缺键」。
    """

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name

    def __str__(self) -> str:
        # 覆盖 KeyError.__str__ 的 repr 引号行为
        return f"dependency {self.name!r} is not registered"
