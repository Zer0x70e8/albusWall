#
"""任务函数集合：可被 spawn 子进程 import 的模块级顶层函数。

这里的函数只依赖标准库，可以被 ``ProcessPoolExecutor(spawn)`` 安全
反序列化（详见 :mod:`albuswall.demo.demo`）。
"""
