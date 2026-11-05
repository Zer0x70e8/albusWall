#
"""``albuswall.ui.delegate`` —— 全部绘制委托（模型 ↔ 像素的唯一转换点）。

委托只认 role，不认模型类，所以同一份委托可以服务多份模型：

============================  ==============================================
委托                          Role 契约 / 适用视图
============================  ==============================================
:class:`SquareThumbDelegate`   ``DecorationRole``，正方形格子基类
:class:`AlbumCoverDelegate`    ``ItemRole``，相册封面网格
:class:`TrashAlbumDelegate`    ``ItemRole``，回收站单行（左图标 / 弹簧 / 右名称）
:class:`AssetThumbDelegate`    ``ItemRole``，资产网格（收藏 / 倒计时角标）
:class:`MetadataDelegate`      ``MetadataRole``，信息面板键值行
:class:`SourceCardDelegate`    ``SourceCardRole``，导入源卡片（``ColumnListView``）
:class:`InfoCellDelegate`      ``ItemRole``（或 getter），缩略图网格（内容网格：
                               ``InfoMode.AUTO`` 逐格显示回收站天数 / 收藏状态）
:class:`TagChipDelegate`       标签 chip（tags 视图，随 ``TagChipView`` 提供）
============================  ==============================================
"""

from __future__ import annotations

from ..widgets.tag_chip_delegate import TagChipDelegate
from .album_cover_delegate import AlbumCoverDelegate
from .asset_thumb_delegate import AssetThumbDelegate
from .info_cell_delegate import InfoCellDelegate, InfoMode
from .metadata_delegate import MetadataDelegate
from .source_card_delegate import CARD_DEFAULTS, SourceCardDelegate
from .square_thumb_delegate import SquareThumbDelegate
from .trash_album_delegate import TrashAlbumDelegate

__all__ = [
    "CARD_DEFAULTS",
    "AlbumCoverDelegate",
    "AssetThumbDelegate",
    "InfoCellDelegate",
    "InfoMode",
    "MetadataDelegate",
    "SourceCardDelegate",
    "SquareThumbDelegate",
    "TagChipDelegate",
    "TrashAlbumDelegate",
]
