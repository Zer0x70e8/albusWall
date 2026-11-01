#
"""只读视图域：相簿 / 资产的查询门面（本模块只做再导出）。

为什么单独成包（而不是并进 ``service/asset``）
----------------------------------------------
它 owner 的是**读取形状**，不是表：一次调用跨 ``assets`` / ``albums`` /
``album_assets`` 三张表，返回给 UI 的是一屏能直接渲染的数据（DTO + 排序 +
邻接位置）。``dto`` 侧已经是 CQRS 分明的（``dto/album.py`` 是 Query，
``dto/asset/events.py`` 是 Command 结果），包结构跟它对齐就不会别扭。

与其它包的关系::

    ViewService ──(assets_viewed)──→ ThumbnailService   按需生成缩略图
    ViewService ──(只读)──────────→ ViewRepository

本包不 import 任何其它服务包；写操作全部落在
:mod:`albuswall.service.asset.command`。
"""

from .service import SCOPE_ACTIVE, SCOPE_DELETED, ViewService

__all__ = ["SCOPE_ACTIVE", "SCOPE_DELETED", "ViewService"]
