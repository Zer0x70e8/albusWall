#
"""``albuswall.ui.model`` —— UI 展示模型层（Qt item model）。

定位
----
这里是「数据 → 视图」的**唯一转换层**：模型把外部喂进来的条目（dataclass、
DTO、``sqlite3.Row`` 包出来的对象……只要是带同名属性的对象都行）摊平成
Qt 的 item data，委托再从 role 里取数据绘制。因此：

* **不 import ``albuswall.dto``**：模型只认「属性名」，不认具体领域类型，
  换成 DTO、pydantic 模型或裸 dataclass 都不需要改这一层；
* **不创建 widget**：模型只发 Qt 通知，绑定的 view 自己重绘；
* **不查库 / 不调度服务**：装载数据是调用方的事。

包内分工
--------
``roles``              全部 role 契约（模型与委托之间的私有协议）
``base``               通用条目列表模型基类（声明式 role → 字段映射）
``album_model``        相册格子（封面 + 标题 + 描述）
``asset_model``        资产格子（缩略图 + 收藏 / 倒计时角标）
``metadata_model``     信息面板的键值行（带字段规格与格式化）
``navigation_model``   详情页底部缩略图导航条（支持懒加载回调）
``source_card_model``  导入源卡片（标题 / 描述 / 路径 / 标签）
``tag_model``          chip 列表与层级标签树

典型用法::

    from albuswall.ui.model import AlbumModel, AssetGridModel, SourceCardModel

    album_model = AlbumModel(parent=window.album)
    album_model.set_entries(albums)
    window.album.set_album_model(album_model)   # page 会顺带装好默认委托
"""

from __future__ import annotations

from .album_model import AlbumEntry, AlbumModel
from .asset_model import AssetEntry, AssetGridModel
from .base import EntryListModel
from .metadata_model import FieldSpec, MetadataModel
from .navigation_model import NavigationEntry, NavigationModel
from .roles import (
    METADATA_ROLE_BAND,
    NAVIGATION_ROLE_BAND,
    SOURCE_CARD_ROLE_BAND,
    TAG_ROLE_BAND,
    THUMBNAIL_ROLE_BAND,
    AlbumRole,
    AssetRole,
    ItemRole,
    MetadataRole,
    NavigationRole,
    SourceCardRole,
    TagRole,
)
from .source_card_model import SourceCard, SourceCardModel
from .tag_model import TagChipModel, TagNode, TagTreeModel

__all__ = [
    # 基类
    "EntryListModel",
    # role 契约
    "METADATA_ROLE_BAND",
    "NAVIGATION_ROLE_BAND",
    "SOURCE_CARD_ROLE_BAND",
    "TAG_ROLE_BAND",
    "THUMBNAIL_ROLE_BAND",
    "AlbumRole",
    "AssetRole",
    "ItemRole",
    "MetadataRole",
    "NavigationRole",
    "SourceCardRole",
    "TagRole",
    # 模型
    "AlbumEntry",
    "AlbumModel",
    "AssetEntry",
    "AssetGridModel",
    "FieldSpec",
    "MetadataModel",
    "NavigationEntry",
    "NavigationModel",
    "SourceCard",
    "SourceCardModel",
    "TagChipModel",
    "TagNode",
    "TagTreeModel",
]
