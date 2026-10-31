#
"""三层视图：``Configue.tree`` → ``_ConfigTree`` → ``_FileView`` → ``_SectionView``。

设计约定：

- 视图**无状态**（不缓存、不持有值），每次访问新建一个；构造 O(1)。
- 视图是「值总线」，**不做类型校验**：``tree.main.core.debug = "yes"`` 会直接
  写进 Resolver（``debug: bool`` 也不会拦）。严格 ``isinstance`` 只属于
  ``@configclass`` 类字段（``_ConfigSlot.__set__``）。
- **树视图可以写未声明的 key**：``tree.main.new_key = 1`` 不报错，因为写入
  ``__setattr__`` 只要求「文件已加载」，不查 ``@configclass`` 声明。这是和
  ``Config()`` 视图的关键区别——``Config`` 的字段必须在 ``@configclass`` 里
  声明过（否则类上根本没有这个属性）。
  例外：``Configue.strict_tree_writes = True``（默认关）会额外要求
  ``(section, key)`` 被 ``@configclass`` 声明或加载钩子登记过，否则抛
  ``AttributeError``。这是给开发/测试期的审计开关，见 ``Configue`` 的
  ``strict_tree_writes`` 属性文档。
- 写入是**文件作用域**的：``tree.main.core.x = v`` 把 ``x`` 的 origin 改写成
  main 的路径，于是 ``tree.main.save()`` 一定写回 main。这是「文件视图」的
  应有语义——否则值会留在原先读到它的 machine.ini 里，save() 变成空操作。
  同理，未声明 key 的 ``origin`` 也指向 main，所以同样能写回（``_FIELD_OWNER``
  的兜底只为「没有 origin 的已声明字段」服务）。
- **树视图共享 Resolver，写入只改「归属文件」，不改值本身**：所有视图读的是
  同一份 ``Resolver._raw``。``tree.main.core.x = v`` 之后再读
  ``tree.machine.core.x`` 拿到的**还是 v**（不是 machine 文件里的旧值）。
  视图**隔离的是写回落点与 ``${...}`` 的 origin 归属，不隔离值**；想要「每个
  文件一个独立值」请用不同的 key，或直接读原始文件。
- **``unload()`` 的回退只在内存里**：写过盘之后就 ``save()`` 了，卸载只能让
  内存回退到别的文件的值，磁盘不会被回滚（详见 ``Configue.unload``）。
- ``file`` 必须已 ``load()``：未加载时 ``_FileView.__getattr__`` 抛
  ``AttributeError``，不给「静默读到空值」的机会。但**枚举**（``keys()`` /
  ``__len__`` / ``__iter__`` / ``__contains__``）是宽容的：未加载的 ``_FileView``
  返回空序列而不是抛异常——枚举用于「先看看有没有值再决定要不要 load()」。
- 只支持 2 层 path（``section.key``）；N 层留到后续。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from .registry import _FILES

if TYPE_CHECKING:  # noqa: F401  只在类型检查期需要，避免运行期循环 import
    from .configue import Configue

__all__ = ["_ConfigTree", "_FileView", "_SectionView"]


class _ConfigTree:
    """配置树根：``c.tree.<file>.<section>.<key>``。

    不缓存（``Configue.tree`` 每次新建一个），所以不存在「视图过期」问题：
    文件 ``unload()`` 之后旧的 ``_FileView`` 会因为 ``loaded=False`` 立即失效。
    """

    __slots__ = ("_configue",)

    def __init__(self, configue: "Configue") -> None:
        self._configue = configue

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        if name not in _FILES:
            raise AttributeError(
                f"no config file named {name!r}; "
                f"registered files: {sorted(_FILES)}"
            )
        return _FileView(self._configue, name)

    def __getitem__(self, name: str):
        return self.__getattr__(name)

    # ---------- dict-like ----------
    def keys(self) -> list[str]:
        """所有已 ``reg`` 的文件名（不区分 loaded），按 ``order`` 降序。

        与 ``classes_in`` 一致：order 大的在前，给调试 / 设置页一个稳定顺序。
        """
        return [
            f.name
            for f in sorted(_FILES.values(), key=lambda f: (-f.order, f.name))
        ]

    def values(self) -> list["_FileView"]:
        return [_FileView(self._configue, name) for name in self.keys()]

    def items(self) -> list[tuple[str, "_FileView"]]:
        return list(zip(self.keys(), self.values()))

    def __contains__(self, name: object) -> bool:
        return (
            isinstance(name, str)
            and not name.startswith("_")
            and name in _FILES
        )

    def __len__(self) -> int:
        return len(_FILES)

    def __iter__(self):
        return iter(self.keys())

    def __dir__(self):
        return self.keys()

    def __repr__(self):
        loaded = sum(1 for f in _FILES.values() if f.loaded)
        return f"_ConfigTree(files={len(_FILES)}, loaded={loaded})"


class _FileView:
    """单个配置文件的视图：``c.tree.<file>``。"""

    __slots__ = ("_configue", "_name")

    def __init__(self, configue: "Configue", name: str) -> None:
        self._configue = configue
        self._name = name

    def __getattr__(self, section: str):
        if section.startswith("_"):
            raise AttributeError(section)
        finfo = _FILES.get(self._name)
        if finfo is None or not finfo.loaded:
            raise AttributeError(
                f"file {self._name!r} is registered but not loaded; "
                f"call configue.load() first"
            )
        return _SectionView(self._configue, self._name, (section,))

    def __getitem__(self, section: str):
        return self.__getattr__(section)

    def save(self) -> list[Path]:
        """把该文件的 dirty 节点写回磁盘（``Configue.flush(file=...)`` 的别名）。"""
        return self._configue.flush(file=self._name)

    # ---------- dict-like（枚举宽容：未 loaded → 空）----------
    def keys(self) -> list[str]:
        """该文件**当前提供生效值**的 section 名；未 loaded → ``[]``。"""
        finfo = _FILES.get(self._name)
        if finfo is None or not finfo.loaded or finfo.resolved_path is None:
            return []
        return self._configue.resolver.sections_of_path(finfo.resolved_path)

    def values(self) -> list["_SectionView"]:
        return [
            _SectionView(self._configue, self._name, (section,))
            for section in self.keys()
        ]

    def items(self) -> list[tuple[str, "_SectionView"]]:
        return list(zip(self.keys(), self.values()))

    def __contains__(self, section: object) -> bool:
        return isinstance(section, str) and section in self.keys()

    def __len__(self) -> int:
        return len(self.keys())

    def __iter__(self):
        return iter(self.keys())

    def __dir__(self):
        return self.keys()

    def __repr__(self):
        finfo = _FILES.get(self._name)
        if finfo is None:
            return f"_FileView(name={self._name!r}, <unregistered>)"
        if not finfo.loaded:
            return f"_FileView(name={self._name!r}, <not loaded>)"
        return f"_FileView(name={self._name!r}, sections={self.keys()})"


class _SectionView:
    """单个 section 的视图：``c.tree.<file>.<section>``。

    写入是「值总线」语义：不校验类型、**默认不要求 key 被 ``@configclass``
    声明**（详见模块 docstring）。``strict_tree_writes`` 打开后才会校验声明。
    """

    __slots__ = ("_configue", "_file", "_path")

    def __init__(
            self,
            configue: "Configue",
            file: str,
            path: tuple[str, ...],
    ) -> None:
        # 必须走 object.__setattr__：下面把 __setattr__ 劫持成「写配置」了，
        # 普通赋值会无限递归（``_configue`` 会被当成 key 写进 Resolver）。
        object.__setattr__(self, "_configue", configue)
        object.__setattr__(self, "_file", file)
        object.__setattr__(self, "_path", path)

    def __getattr__(self, key: str):
        if key.startswith("_"):
            raise AttributeError(key)
        section = self._path[0]
        try:
            return self._configue.resolver.get_typed(section, key)
        except KeyError:
            raise AttributeError(key) from None

    def __setattr__(self, key: str, value: Any):
        if key.startswith("_"):
            object.__setattr__(self, key, value)
            return
        finfo = _FILES.get(self._file)
        if finfo is None or not finfo.loaded or finfo.resolved_path is None:
            raise RuntimeError(
                f"file {self._file!r} is registered but not loaded; "
                f"call configue.load() first"
            )
        section = self._path[0]
        configue = self._configue
        # 声明校验（默认关）：拼错 key 直接炸，而不是静默写进文件。
        # 注意检查放在「文件已加载」之后——未加载是更根本的问题，报它更有用。
        # 判定规则与 ``load(strict_undeclared=True)`` 共用 ``_is_declared``：
        # 一处定义「什么算被认识」，读侧审计与写侧拦截才不可能漂移。
        if configue.strict_tree_writes and not configue._is_declared(  # noqa: SLF001
                section, key,
        ):
            raise AttributeError(
                f"[{section}]{key} is not declared by any @configclass "
                f"or registered by a load hook; write refused "
                f"(strict_tree_writes=True)"
            )
        configue.resolver.set(section, key, value, persist=True)
        # 树视图是**文件视图**：从这里写进去的值归属当前文件，而不是留在
        # 「原先读到它的那个文件」里。否则 ``tree.main.x = v`` + ``tree.main.save()``
        # 会静默把值写到 machine.ini，与视图名字矛盾。
        # 注意：这也让「未声明的 key」有 origin，flush 时不需要 _FIELD_OWNER 兜底。
        configue.resolver.set_origin(section, key, finfo.resolved_path)
        configue._schedule_persist(self._file)

    def __getitem__(self, key: str):
        return self.__getattr__(key)

    def __setitem__(self, key: str, value: Any):
        self.__setattr__(key, value)

    # ---------- dict-like ----------
    def keys(self) -> list[str]:
        """该 section 的 key（``@configclass`` 声明并注入默认值的 + 文件里写的）。

        **只列「取得到值」的 key**：声明过但没有默认值、文件里也没写的字段
        不在列——``keys()`` / ``values()`` / ``items()`` 必须保持一一对应，
        否则 ``for k in view.keys(): view[k]`` 会在缺值字段上抛
        ``ConfigMissingError``。需要探测「某个 key 有没有值」用
        ``key in view``（它判的正是 Resolver 里有没有这个节点）。
        """
        return self._configue.resolver.keys_of(self._path[0])

    def values(self) -> list[Any]:
        section = self._path[0]
        return [
            self._configue.resolver.get_typed(section, key)
            for key in self.keys()
        ]

    def items(self) -> list[tuple[str, Any]]:
        return list(zip(self.keys(), self.values()))

    def __contains__(self, key: object) -> bool:
        return (
            isinstance(key, str)
            and (self._path[0], key) in self._configue.resolver
        )

    def __len__(self) -> int:
        return len(self.keys())

    def __iter__(self):
        return iter(self.keys())

    def __dir__(self):
        return self.keys()

    def __repr__(self):
        return (
            f"_SectionView(file={self._file!r}, "
            f"section={self._path[0]!r}, keys={self.keys()})"
        )
