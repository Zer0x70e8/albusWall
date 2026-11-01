#
"""相簿域服务：``albums`` 的生命周期（本模块只做再导出）。

它为什么存在
------------
``albums`` 是唯一"有表、有索引、有外键，但两版实现都没有写侧"的对象 ——
读一直走 ``ViewRepository``，写从缺。本包补上写侧，`album_assets` 的成员
增删仍归资产写用例（:mod:`albuswall.service.asset.command`）。

边界::

    service/album/         albums：建 / 改 / 封面 / 排序 / 软删 / 恢复
    service/asset/command/ album_assets 成员：加 / 删 / 移动（以"一批资产"为主语）
    service/view/          读：相簿 DTO、封面、成员列表、邻接位置

三者的可见性口径完全一致（``albums.is_deleted = 0`` + 资产可见性），因为都
基于 ``repositories/utils/visibility.py`` 里那套 where 片段。
"""

from .service import AlbumService

__all__ = ["AlbumService"]
