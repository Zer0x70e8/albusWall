#
"""相册哨兵 uuid 常量（系统相册 / 回收站相册的**唯一真源**）。

虚拟相册（All / Trash）不对应 ``albums`` 表的任何一行，它们由固定 uuid
表示，语义（scope = ``active`` / ``deleted``）定义在 ``services/view.py``。
本模块只放**标识**，不放行为：

* 判定函数：``ViewRepository.is_system_album`` / ``is_trash_album``
  （A1 下沉，服务层可直接调用）；
* 同名再导出：``services/view.py`` 继续 import 本模块，旧路径
  ``albuswall.services.view.VIRTUAL_ALBUM_ALL_UUID`` 不破坏。

历史：这两个常量原先定义在 ``services/view.py``，导致写服务（不能 import
UI，也不想 import 整个 ViewService）拿不到"这 uuid 是不是系统相册"的
领域判定。本次下沉到 ``common/constants``，仓储 / 服务 / UI 共用同一份。

依赖方向：``common ← (repositories / services / ui)``；本模块**不得**反向
import ``dto`` / ``repositories`` / ``services``。
"""

from uuid import UUID

__all__ = [
    "TRASH_ALBUM_UUIDS",
    "VIRTUAL_ALBUM_ALL_UUID",
    "VIRTUAL_ALBUM_TRASH_UUID",
    "VIRTUAL_ALBUM_UUIDS",
]

VIRTUAL_ALBUM_ALL_UUID: UUID = UUID("00000000-0000-0000-0000-000000000001")
"""「全部」虚拟相册 uuid（scope = ``active``）。

资产写操作**不得**以它为成员写入目标：它不是 ``albums`` 表的一行，
成员关系由 ``assets`` 的可见性派生。
"""

VIRTUAL_ALBUM_TRASH_UUID: UUID = UUID("00000000-0000-0000-0000-000000000002")
"""回收站虚拟相册 uuid（scope = ``deleted``）。同样是派生视图，不落库。"""

VIRTUAL_ALBUM_UUIDS: frozenset[UUID] = frozenset({
    VIRTUAL_ALBUM_ALL_UUID,
    VIRTUAL_ALBUM_TRASH_UUID,
})
"""全部系统相册（虚拟相册）uuid。

契约：不在本集合里的 uuid 一律按**物理相册**处理——判定函数对未知 uuid
返回 ``False``，不抛异常（与 UI 现有 ``is_system_album`` 语义一致）。
"""

TRASH_ALBUM_UUIDS: frozenset[UUID] = frozenset({VIRTUAL_ALBUM_TRASH_UUID})
"""回收站相册 uuid 集合（当前只有 Trash 一个，为将来多 trash 视图留位）。"""
