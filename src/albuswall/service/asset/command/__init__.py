#
"""资产写命令子包：``assets`` / ``album_assets`` / ``asset_tags`` 的写入口。

与其它包的边界::

    service/view/        读（跨 assets/albums/album_assets，只读门面）
    service/asset/command/  写（收藏 / 软删 / 相册成员 / 标签绑定）
    service/asset/trash/    回收站（软删的续集：恢复 / 保留期清理 / 清空）
    service/tag/         标签自身的生命周期（树 / 路径解析 / 别名）

为什么 ``ops`` 与 ``tagging`` 在**同一个包**里
---------------------------------------------
两者共享 ``AssetRepository``，而且 ``AssetOpsService`` 构造上就依赖
``AssetTagService``（组合批量打标签用）。拆成两个包只会多一层 import，还让
"资产写操作"这个概念裂成两半。将来真要拆，外部 import 路径也不受影响。

没有信号（本轮）
----------------
源域的写命令会发 ``source_created`` / ``source_changed`` / ``source_deleted``，
因为状态层与触发器桥接层要订阅它们。资产域现在**没有订阅方**：UI 走主动
查询，缩略图由 ``ViewService.assets_viewed`` 按需驱动。因此在写方法里加
``emit`` 只会带来"发了没人听"的噪音。

真需要时按源域的做法来：在对应服务上声明 ``SignalDescriptor``，在装配层
（``service/__init__.py``）接线，别在各方法里散落 emit。
"""

from .ops import AssetOpsService
from .tagging import (
    AddTagsResult,
    AssetTagError,
    AssetTagService,
    SetTagsResult,
)

__all__ = [
    "AddTagsResult",
    "AssetOpsService",
    "AssetTagError",
    "AssetTagService",
    "SetTagsResult",
]
