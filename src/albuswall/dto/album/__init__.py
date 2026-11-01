#
"""Album 域 DTO 包（Command / Query 分包），与 ``dto.tag`` 同构。

* ``dto.album.query``   —— 读模型（``Album`` / ``Asset``）；
* ``dto.album.command`` —— 写模型（``AlbumCreate`` / ``AlbumUpdate`` / ``AlbumReorder``）。

本 ``__init__`` 只做**显式**再导出（无通配、无新增语义）；新代码建议直接从所属
子模块 import，在 import 行上就能看出读写侧。

历史：本域原先是单文件 ``dto/album.py``；加入相簿写模型时改成包，旧路径
``from albuswall.dto.album import Album, Asset`` 由本文件保持不变。
"""

from .command import AlbumCreate, AlbumReorder, AlbumUpdate
from .query import Album, Asset

__all__ = [
    "Album",
    "AlbumCreate",
    "AlbumReorder",
    "AlbumUpdate",
    "Asset",
]
