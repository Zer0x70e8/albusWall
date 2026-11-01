#
"""UI 模型的 role 契约（模型 ↔ 视图 / 委托之间的私有协议）。

模型在 ``data()`` 里按 role 产出数据，委托在 ``paint()`` 里按 role 取数据；
两端各写一份魔法数字最容易失联，所以这里集中声明全部 role。

分段规则
--------
``UserRole`` 之后按 **域** 划独立区间，每段 100 个槽位，互不重叠：

====================  ==========================================
区间                  用途
====================  ==========================================
+0                    缩略图格子（:class:`ItemRole`，相册 / 资产共用）
+100                  信息面板键值行（:class:`MetadataRole`）
+200                  导入源卡片（:class:`SourceCardRole`）
+300                  标签 chip / 标签树（:class:`TagRole`）
+400                  详情页导航条（:class:`NavigationRole`）
====================  ==========================================

因为相册和资产是同一类「一张图 + 一行字」的格子，两者共用
:class:`ItemRole` 的槽位（``AlbumRole`` / ``AssetRole`` 只是语义别名），
同一个委托因此能同时吃两种模型；其余域各占一段，扩展时不互相打扰。

兼容别名
--------
``KEY_ROLE`` / ``VALUE_ROLE`` / ``RAW_VALUE_ROLE`` / ``TAG_ROLE`` /
``TREE_TAG_ROLE`` / ``NAV_ID_ROLE`` 是给旧调用方留的模块级别名，
新代码请直接用枚举（``MetadataRole.Key`` 等）。
"""

from __future__ import annotations

from enum import IntEnum

from PySide6.QtCore import Qt

__all__ = [
    "METADATA_ROLE_BAND",
    "NAVIGATION_ROLE_BAND",
    "SOURCE_CARD_ROLE_BAND",
    "TAG_ROLE_BAND",
    "THUMBNAIL_ROLE_BAND",
    "AlbumRole",
    "AssetRole",
    "ItemRole",
    "KEY_ROLE",
    "MetadataRole",
    "NAV_ID_ROLE",
    "NavigationRole",
    "RAW_VALUE_ROLE",
    "SourceCardRole",
    "TAG_ROLE",
    "TagRole",
    "TREE_TAG_ROLE",
    "VALUE_ROLE",
]

#: ``Qt.ItemDataRole.UserRole`` 的整数起点，所有自定义 role 从这里往后排。
_USER_ROLE = int(Qt.ItemDataRole.UserRole)

#: 缩略图格子 role 区间起点（相册 / 资产共用）。
THUMBNAIL_ROLE_BAND = _USER_ROLE
#: 信息面板 role 区间起点。
METADATA_ROLE_BAND = _USER_ROLE + 100
#: 导入源卡片 role 区间起点。
SOURCE_CARD_ROLE_BAND = _USER_ROLE + 200
#: 标签 role 区间起点。
TAG_ROLE_BAND = _USER_ROLE + 300
#: 详情页导航条 role 区间起点。
NAVIGATION_ROLE_BAND = _USER_ROLE + 400


class ItemRole(IntEnum):
    """缩略图格子的数据 role（相册与资产共用一套槽位）。

    取值不一定全部被某个模型提供：相册不提供 ``Favorite``，资产不提供
    ``AssetCount``，``data()`` 对未提供（或值为 ``None``）的 role 返回
    ``None``，委托据此决定画不画。
    """

    #: 标识（``UUID`` 或字符串，口径由调用方决定）。
    Uuid = THUMBNAIL_ROLE_BAND + 1
    #: 标题；与 ``DisplayRole`` 同值。
    Title = THUMBNAIL_ROLE_BAND + 2
    #: 缩略图 ``QPixmap``；与 ``DecorationRole`` 同值。
    Pixmap = THUMBNAIL_ROLE_BAND + 3
    #: 描述 / 副标题。
    Description = THUMBNAIL_ROLE_BAND + 4
    #: 是否收藏（``True`` / ``False`` / ``None`` 表示未知）。
    Favorite = THUMBNAIL_ROLE_BAND + 5
    #: 回收站剩余天数（``int`` / ``None`` 表示不在回收站）。
    DaysLeft = THUMBNAIL_ROLE_BAND + 6
    #: 是否已软删（``bool`` / ``None`` 表示未知）。
    Deleted = THUMBNAIL_ROLE_BAND + 7
    #: 资产数量（相册封面角标用）。
    AssetCount = THUMBNAIL_ROLE_BAND + 8


#: 相册格子的 role —— 与 :class:`ItemRole` 同槽位，只是语义别名。
AlbumRole = ItemRole
#: 资产格子的 role —— 与 :class:`ItemRole` 同槽位，只是语义别名。
AssetRole = ItemRole


class MetadataRole(IntEnum):
    """信息面板键值行的 role。"""

    #: 字段显示名（已本地化），如「拍摄时间」。
    Key = METADATA_ROLE_BAND + 1
    #: 字段显示值（已格式化），如「2024-01-01 12:00」。
    Value = METADATA_ROLE_BAND + 2
    #: 字段原始值，供排序 / 复制 / 二次处理。
    RawValue = METADATA_ROLE_BAND + 3


class SourceCardRole(IntEnum):
    """导入源卡片的数据 role。"""

    #: 源标识（``UUID`` / ``int`` 均可）。
    Uuid = SOURCE_CARD_ROLE_BAND + 1
    #: 卡片标题；与 ``DisplayRole`` 同值。
    Title = SOURCE_CARD_ROLE_BAND + 2
    #: 卡片描述。
    Description = SOURCE_CARD_ROLE_BAND + 3
    #: 源路径（半等宽单行显示）。
    Path = SOURCE_CARD_ROLE_BAND + 4
    #: 标签名序列（``list[str]``）。
    Tags = SOURCE_CARD_ROLE_BAND + 5


class TagRole(IntEnum):
    """标签 chip / 标签树节点的 role。"""

    #: 标签对象本身（至少带 ``uuid``，可选 ``name`` / ``display_name``）。
    Tag = TAG_ROLE_BAND + 1


class NavigationRole(IntEnum):
    """详情页导航条缩略图的数据 role。"""

    #: 导航项标识（只用于回传信号，模型不解释它）。
    Id = NAVIGATION_ROLE_BAND + 1
    #: 缩略图 ``QPixmap``；与 ``DecorationRole`` 同值。
    Pixmap = NAVIGATION_ROLE_BAND + 2


# --------------------------------------------------------------------------- #
# 兼容别名（旧调用方直接 import 这些名字）
# --------------------------------------------------------------------------- #
#: 信息面板字段名 role 的旧名字。
KEY_ROLE = int(MetadataRole.Key)
#: 信息面板字段值 role 的旧名字。
VALUE_ROLE = int(MetadataRole.Value)
#: 信息面板字段原始值 role 的旧名字。
RAW_VALUE_ROLE = int(MetadataRole.RawValue)
#: 标签 chip / 标签树承载标签对象的 role 的旧名字。
TAG_ROLE = int(TagRole.Tag)
#: 标签树承载标签对象的 role 的旧名字（与 chip 同槽位）。
TREE_TAG_ROLE = int(TagRole.Tag)
#: 导航条承载标识的 role 的旧名字。
NAV_ID_ROLE = int(NavigationRole.Id)
