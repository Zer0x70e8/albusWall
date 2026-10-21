#
"""共享内核（shared kernel）。

只放跨域、低层、无业务编排能力的东西：

* ``enums/``         —— 值域枚举（按域分文件，``__init__`` 汇总）
* ``constants/``     —— schema / 跨域常量（按域分文件，不做汇总 re-export）
* ``exceptions.py``  —— 跨层业务异常
* ``futures.py``     —— 跨域复用的并发原语

约定正文见 ``common/CONVENTIONS.md``。
"""
