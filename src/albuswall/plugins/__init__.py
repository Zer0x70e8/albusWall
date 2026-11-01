#
"""第一方插件目录。

``main._boot`` 用 ``discover_all(builtin_package="albuswall.plugins")`` 扫描本包
的**所有子模块**并 import；子模块顶层的 ``@plugin`` 装饰器负责完成声明。因此：

- 新增内置插件 = 在本目录下新建一个 ``.py``（或子包），在顶层写 ``@plugin``；
- 以 ``_`` 开头的模块不会被扫描（可以放内部工具）；
- 本 ``__init__`` 不做任何 import / 注册，保持「import 期零副作用」。
"""
