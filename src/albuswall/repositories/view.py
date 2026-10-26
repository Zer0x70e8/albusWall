#
"""相簿（专辑）视图仓储（只读）。

接口契约（P1 收敛）：
    · 所有面向资产的公开方法只接受 uuid、只返回 uuid；``assets.id``
      仅作为内部索引 seek / tie-break 使用。
    · uuid 入参统一收 ``UUID | str``：``UUID`` 是 DTO 侧口径，``str`` 是
      历史调用点口径，绑定前一律过 ``as_uuid_str``（见
      ``repositories/utils/sql_helpers.py``）。
    · uuid 出参统一为 ``UUID``（与 DTO 的 UUID 统一契约对齐），列表形式
      为 ``list[UUID]``。
    · 所有「资产列表」ORDER BY 从 ``repositories/utils/asset_order.py``
      单点派生，保证 scope / 相簿 / 标签三处的「上/下一张」顺序一致。
    · 写操作（收藏 / 软删 / 恢复 / 硬删 / 相簿成员）见 ``AssetRepository``。
    · 行 → DTO 一律走 ``DTO.from_row``（``_fetch_one_dto`` /
      ``_fetch_all_dtos``），仓储不再手拼 DTO。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence
from uuid import UUID

from albuswall.common.constants.albums import (
    TRASH_ALBUM_UUIDS,
    VIRTUAL_ALBUM_UUIDS,
)
from albuswall.dto.album import Album, Asset
from albuswall.dto.utils import to_uuid

from .base import BaseRepository
from .utils.asset_order import order_by
from .utils.sql_helpers import as_uuid_str, chunked, placeholders
from .utils.visibility import active_asset_where

_UUIDLike = UUID | str


def _safe_uuid(value: _UUIDLike) -> Optional[UUID]:
    """把 uuid / 字符串安全解析成 ``UUID``；非法输入返回 ``None``（不抛）。

    系统相册判定 / 相册存在性判定都宁愿回答 ``False`` 也不抛——传入
    垃圾 uuid 是调用方的输入问题，不该让一个布尔判定炸掉整条写路径。
    """
    try:
        return to_uuid(value)
    except (ValueError, AttributeError, TypeError):
        return None


# noinspection SpellCheckingInspection
class ViewRepository(BaseRepository):
    """相簿（专辑）视图仓储（只读）。

    **只读**：写操作见 ``AssetRepository``。

    职责边界：
        · 虚拟相册（scope = active / deleted）的封面、列表、邻居；
        · 物理相簿的元信息、封面、成员列表、邻居；
        · 单个资产的 DTO / 磁盘路径 / 邻居定位。
    """

    # ---------------- 可见性条件 ----------------
    # 「可见资产」= 资产未软删 AND 所属 source 未软删，条件由
    # ``utils/visibility.py`` 单点生成，scope / 相簿 / 单资产读取共用。
    _ACTIVE_WHERE = active_asset_where()
    _ACTIVE_WHERE_A = active_asset_where("a")
    _SCOPE_WHERE = {
        "active": _ACTIVE_WHERE,
        "deleted": "is_deleted = 1",
    }
    _SCOPE_KEY_COL = {"active": "taken_at", "deleted": "deleted_at"}

    _PHYSICAL_ALBUM_FROM = (
        "assets a "
        "JOIN album_assets aa ON aa.asset_id = a.id "
        "JOIN albums AS al ON al.id = aa.album_id"
    )
    _PHYSICAL_ALBUM_WHERE = (
        f"al.uuid = ? AND al.is_deleted = 0 AND {_ACTIVE_WHERE_A}"
    )

    @classmethod
    def _scope_where(cls, scope: str) -> str:
        """scope → 可见性条件；非法 scope 抛 ``ValueError``（与 order_by 一致）。"""
        try:
            return cls._SCOPE_WHERE[scope]
        except KeyError:
            raise ValueError(f"unsupported scope: {scope!r}") from None

    # ==================================================================
    # 虚拟相册（scope）封面 / 列表
    # ==================================================================

    def get_cover_asset_by_scope(self, scope: str) -> Optional[Asset]:
        where = self._scope_where(scope)
        order = order_by(scope)
        return self._fetch_one_dto(
            Asset,
            f"SELECT * FROM assets WHERE {where} ORDER BY {order} LIMIT 1",
        )

    def list_assets_by_scope(self, scope: str) -> list[Asset]:
        where = self._scope_where(scope)
        order = order_by(scope)
        return self._fetch_all_dtos(
            Asset,
            f"SELECT * FROM assets WHERE {where} ORDER BY {order}",
        )

    def list_asset_uuids_by_scope(self, scope: str) -> list[UUID]:
        where = self._scope_where(scope)
        order = order_by(scope)
        rows = self._fetchall(
            f"SELECT uuid FROM assets WHERE {where} ORDER BY {order}"
        )
        return [to_uuid(row["uuid"]) for row in rows]

    # ==================================================================
    # 相册
    # ==================================================================

    def list_album_uuids(self) -> list[UUID]:
        """所有未被软删除的相簿 uuid（按 sort_order, id 排序）。"""
        rows = self._fetchall(
            """
            SELECT uuid
              FROM albums
             WHERE is_deleted = 0
             ORDER BY sort_order ASC, id ASC
            """
        )
        return [to_uuid(row["uuid"]) for row in rows]

    def get_album(self, album_uuid: _UUIDLike) -> Optional[Album]:
        """按 uuid 获取未被软删除的相簿 DTO。

        封面语义：``cover.is_deleted = 0`` 作为 LEFT JOIN 的 ON 条件，
        软删封面只会让 cover_uuid 变 NULL，不会过滤整行相簿。
        """
        cover_where = active_asset_where("cover")
        return self._fetch_one_dto(
            Album,
            f"""
            SELECT a.uuid,
                   a.title,
                   a.description,
                   cover.uuid AS cover_uuid,
                   a.album_type AS type,
                   a.created_at,
                   a.modified_at
              FROM albums AS a
              LEFT JOIN assets AS cover
                     ON cover.id = a.cover_asset_id
                    AND {cover_where}
             WHERE a.uuid = ?
               AND a.is_deleted = 0
            """,
            (as_uuid_str(album_uuid),),
        )

    def get_album_cover(self, album_uuid: _UUIDLike) -> Optional[Asset]:
        """按相簿 uuid 获取其封面资产（相簿与资产都必须可见）。"""
        asset_where = active_asset_where("asset")
        return self._fetch_one_dto(
            Asset,
            f"""
            SELECT asset.*
              FROM albums AS a
              JOIN assets AS asset
                     ON asset.id = a.cover_asset_id
             WHERE a.uuid = ?
               AND a.is_deleted = 0
               AND {asset_where}
            """,
            (as_uuid_str(album_uuid),),
        )

    def get_album_uuids_of_asset(self, asset_uuid: _UUIDLike) -> list[UUID]:
        """包含该资产的所有可见相簿 uuid（排序同 list_album_uuids）。"""
        rows = self._fetchall(
            f"""
            SELECT al.uuid
              FROM album_assets AS aa
              JOIN albums AS al ON al.id = aa.album_id
              JOIN assets AS a  ON a.id = aa.asset_id
             WHERE a.uuid = ?
               AND al.is_deleted = 0
               AND {self._ACTIVE_WHERE_A}
             ORDER BY al.sort_order ASC, al.id ASC
            """,
            (as_uuid_str(asset_uuid),),
        )
        return [to_uuid(row["uuid"]) for row in rows]

    # ---------------- 系统相册判定 / 相册可见性 ----------------
    # 语义唯一真源：``ui/builtin/library.py`` 的 is_system_album /
    # is_trash_album（经 ViewService.get_scope + 哨兵 uuid 判定）与
    # ``common/constants/albums.py``。本次下沉到仓储，供写服务直接守卫，
    # 服务层不必 import UI 层。

    def is_system_album(self, album_uuid: _UUIDLike) -> bool:
        """该 uuid 是否为**系统相册**（虚拟相册 All / Trash）。

        做什么：纯常量判定，与 ``ui/builtin/library.py::is_system_album``
        （``view_service.get_scope(uuid) is not None``）等义——虚拟相册
        有固定哨兵 uuid，且不落 ``albums`` 表。
        返回：bool。未知 / 非法 / 不存在的 uuid 一律 ``False``。
        抛：不抛。非法 uuid 字符串按"不是系统相册"处理（与 UI 现有实现
        吞掉解析异常的行为一致）。
        前置条件：无。**不查库**（虚拟相册没有对应的库行）。
        原子性：不适用（纯内存判定）。
        """
        album = _safe_uuid(album_uuid)
        return album is not None and album in VIRTUAL_ALBUM_UUIDS

    def is_trash_album(self, album_uuid: _UUIDLike) -> bool:
        """该 uuid 是否为**回收站相册**（scope = ``deleted`` 的虚拟相册）。

        做什么：同 :meth:`is_system_album`，只是比较回收站哨兵集合。
        返回：bool，未知 uuid → ``False``。
        抛：不抛。
        前置条件：无。不查库。
        原子性：不适用。

        说明：回收站**也是**系统相册，两个判定都返回 True。调用方要区分
        原因时必须**先判 trash 再判 system**，否则 ``reason`` 会被
        ``"system"`` 吞掉。
        """
        album = _safe_uuid(album_uuid)
        return album is not None and album in TRASH_ALBUM_UUIDS

    def album_exists(self, album_uuid: _UUIDLike) -> bool:
        """相册行是否存在（**含已软删**）。

        做什么：``SELECT 1 FROM albums WHERE uuid = ?``，不加
        ``is_deleted`` 过滤——本方法回答"有没有这一行"，可见性由
        :meth:`album_is_visible` 回答。
        返回：bool。
        抛：不抛（非法 uuid → False）。
        前置条件：无。
        原子性：单条只读查询，非事务。
        关系：替代 ``get_album(...) is not None`` 的存在性用法——后者
        带 ``is_deleted = 0`` 过滤，无法区分"不存在"与"已软删"。
        """
        album = _safe_uuid(album_uuid)
        if album is None:
            return False
        row = self._fetchone(
            "SELECT 1 FROM albums WHERE uuid = ?",
            (as_uuid_str(album),),
        )
        return row is not None

    def album_is_visible(self, album_uuid: _UUIDLike) -> bool:
        """相册是否存在**且未软删**（可作写入目标 / 可列出）。

        做什么：``SELECT 1 FROM albums WHERE uuid = ? AND is_deleted = 0``。
        返回：bool。
        抛：不抛（非法 uuid → False）。
        前置条件：无。
        原子性：单条只读查询，非事务。
        关系：等价于 ``get_album(...) is not None`` 的存在性部分（不含封面
        联表开销）；语义保持一致（``get_album`` 同样过滤 ``is_deleted``）。
        """
        album = _safe_uuid(album_uuid)
        if album is None:
            return False
        row = self._fetchone(
            "SELECT 1 FROM albums WHERE uuid = ? AND is_deleted = 0",
            (as_uuid_str(album),),
        )
        return row is not None

    def count_assets(self, album_uuid: _UUIDLike) -> int:
        row = self._fetchone(
            f"""
            SELECT COUNT(*) AS cnt
              FROM album_assets AS aa
              JOIN albums AS al ON al.id = aa.album_id
              JOIN assets AS a  ON a.id = aa.asset_id
             WHERE al.uuid = ?
               AND al.is_deleted = 0
               AND {self._ACTIVE_WHERE_A}
            """,
            (as_uuid_str(album_uuid),),
        )
        return int(row["cnt"]) if row else 0

    # ==================================================================
    # 相册内资产
    # ==================================================================

    def list_assets(
            self,
            album_uuid: _UUIDLike,
            offset: int = 0,
            limit: int = 100,
    ) -> list[Asset]:
        """按唯一确定顺序分页获取相簿内资产 DTO。

        排序读 ``assets.taken_at``（与 scope 排序同源）。

        TODO(P2): 大 OFFSET 后续可改为 cursor 分页。
        """
        order = order_by("active", prefix="a.")
        return self._fetch_all_dtos(
            Asset,
            f"""
            SELECT a.*
              FROM album_assets AS aa
              JOIN albums AS al ON al.id = aa.album_id
              JOIN assets AS a  ON a.id = aa.asset_id
             WHERE al.uuid = ?
               AND al.is_deleted = 0
               AND {self._ACTIVE_WHERE_A}
             ORDER BY {order}
             LIMIT ? OFFSET ?
            """,
            (as_uuid_str(album_uuid), limit, offset),
        )

    def list_asset_uuids_by_album(self, album_uuid: _UUIDLike) -> list[UUID]:
        """物理相册内所有可见 asset 的 uuid（同 JOIN / ORDER BY 契约）。"""
        order = order_by("active", prefix="a.")
        rows = self._fetchall(
            f"""
            SELECT a.uuid
              FROM album_assets AS aa
              JOIN albums AS al ON al.id = aa.album_id
              JOIN assets AS a  ON a.id = aa.asset_id
             WHERE al.uuid = ?
               AND al.is_deleted = 0
               AND {self._ACTIVE_WHERE_A}
             ORDER BY {order}
            """,
            (as_uuid_str(album_uuid),),
        )
        return [to_uuid(row["uuid"]) for row in rows]

    # ==================================================================
    # 单个资产
    # ==================================================================

    def get_asset(
            self, asset_uuid: _UUIDLike, include_deleted: bool = False
    ) -> Optional[Asset]:
        """按 uuid 取 Asset。

        ``include_deleted=False``（默认）按 **active 语义**：资产未软删
        且所属 source 未软删，与 ``list_asset_uuids_by_scope("active")`` /
        邻居定位完全同源，避免「列表里没有、按 uuid 却能打开」的错位。

        Args:
            asset_uuid: assets.uuid。
            include_deleted: True 时两层可见性都不过滤（Trash / 恢复流程）。
        """
        where = "uuid = ?"
        if not include_deleted:
            where += f" AND {self._ACTIVE_WHERE}"
        return self._fetch_one_dto(
            Asset,
            f"SELECT * FROM assets WHERE {where}",
            (as_uuid_str(asset_uuid),),
        )

    def classify_by_uuids(
            self, asset_uuids: Sequence[_UUIDLike]
    ) -> dict[str, str]:
        """一次批量查出每个 uuid 的可见性状态（G11）。

        做什么：``SELECT uuid, CASE WHEN <active 条件> THEN 'active' ELSE
        'deleted' END``，一次 ``IN (...)`` 查询（超过
        ``IN_CLAUSE_CHUNK`` 自动分片，每片一条语句）。
        active 条件与 ``get_asset(include_deleted=False)`` **同源**：
        ``assets.is_deleted = 0`` 且所属 ``ingest_source.is_deleted = 0``。

        返回：``{canonical uuid str: "active" | "deleted"}``。
        键按项目批量查询约定是 ``str(UUID)``（见
        ``repositories/utils/types.UuidKeyed``），**不含**不存在的 uuid
        ——调用方用 "键缺失" 表达 missing，不要喂 ``None``。
        抛：不抛（非法 uuid 被跳过，等价于"不存在"）。
        前置条件：无。
        原子性：只读快照（多分片之间不保证同一快照）。单条语句内一致。

        关系：新语义接口，替代"逐条 ``get_asset(include_deleted=True/False)``
        两次查询"的 N+1 分类法（服务层 ``AssetOpsService.classify_assets``
        依赖它）。
        """
        wanted: list[str] = []
        seen: set[str] = set()
        for value in asset_uuids:
            parsed = _safe_uuid(value)
            if parsed is None:
                continue
            key = str(parsed)
            if key in seen:
                continue
            seen.add(key)
            wanted.append(key)
        if not wanted:
            return {}

        result: dict[str, str] = {}
        for chunk in chunked(wanted):
            rows = self._fetchall(
                f"""
                SELECT uuid,
                       CASE WHEN {self._ACTIVE_WHERE} THEN 'active'
                            ELSE 'deleted' END AS state
                  FROM assets
                 WHERE uuid IN ({placeholders(len(chunk))})
                """,
                tuple(chunk),
            )
            for row in rows:
                result[row["uuid"]] = row["state"]
        return result

    # ==================================================================
    # 上/下一张定位（P2）——入参出参全是 uuid
    # ==================================================================
    # 契约：
    #   - 入参 current_asset_uuid：资产 uuid。
    #   - 返回 (prev_uuid, next_uuid, index, total)：
    #       · prev/next 是 ``UUID``；
    #       · index 为 1-based；目标行缺失返回 (None, None, 0, 0)。
    #   - 内部 tie-break 走 assets.id，保证与 list_asset_uuids_by_*
    #     完全同源。索引：idx_assets_active_taken_id / _deleted_deleted_at_id。

    def get_neighbours_by_scope(
            self, scope: str, current_asset_uuid: _UUIDLike
    ) -> tuple[Optional[UUID], Optional[UUID], int, int]:
        return self._neighbours(
            cur_uuid=as_uuid_str(current_asset_uuid),
            from_clause="assets",
            where_clause=self._scope_where(scope),
            where_params=(),
            id_col="id",
            uuid_col="uuid",
            key_col=self._SCOPE_KEY_COL[scope],
        )

    def get_neighbours_by_album(
            self, album_uuid: _UUIDLike, current_asset_uuid: _UUIDLike
    ) -> tuple[Optional[UUID], Optional[UUID], int, int]:
        return self._neighbours(
            cur_uuid=as_uuid_str(current_asset_uuid),
            from_clause=self._PHYSICAL_ALBUM_FROM,
            where_clause=self._PHYSICAL_ALBUM_WHERE,
            where_params=(as_uuid_str(album_uuid),),
            id_col="a.id",
            uuid_col="a.uuid",
            key_col="a.taken_at",
        )

    def locate_in_scope(
            self, scope: str, asset_uuid: _UUIDLike
    ) -> Optional[int]:
        return self._locate(
            cur_uuid=as_uuid_str(asset_uuid),
            from_clause="assets",
            where_clause=self._scope_where(scope),
            where_params=(),
            id_col="id",
            uuid_col="uuid",
            key_col=self._SCOPE_KEY_COL[scope],
        )

    def locate_in_album(
            self, album_uuid: _UUIDLike, asset_uuid: _UUIDLike
    ) -> Optional[int]:
        return self._locate(
            cur_uuid=as_uuid_str(asset_uuid),
            from_clause=self._PHYSICAL_ALBUM_FROM,
            where_clause=self._PHYSICAL_ALBUM_WHERE,
            where_params=(as_uuid_str(album_uuid),),
            id_col="a.id",
            uuid_col="a.uuid",
            key_col="a.taken_at",
        )

    # ------------------------------------------------------------------ #
    # 邻居 / 定位 —— 私有分派器
    # ------------------------------------------------------------------ #

    def _neighbours(
            self, *, cur_uuid, from_clause, where_clause, where_params,
            id_col, uuid_col, key_col,
    ) -> tuple[Optional[UUID], Optional[UUID], int, int]:
        """统一邻居聚合入口。当前项不在集合内 / 集合为空 → (None, None, 0, 0)。"""
        found, cur_id, cur_key = self._fetch_key(
            from_clause=from_clause, where_clause=where_clause,
            where_params=where_params, id_col=id_col, uuid_col=uuid_col,
            key_col=key_col, cur_uuid=cur_uuid,
        )
        if not found:
            return None, None, 0, 0

        prev_uuid = self._find_prev(
            from_clause=from_clause, where_clause=where_clause,
            where_params=where_params, id_col=id_col, uuid_col=uuid_col,
            key_col=key_col, cur_id=cur_id, cur_key=cur_key,
        )
        next_uuid = self._find_next(
            from_clause=from_clause, where_clause=where_clause,
            where_params=where_params, id_col=id_col, uuid_col=uuid_col,
            key_col=key_col, cur_id=cur_id, cur_key=cur_key,
        )
        index = self._count_before(
            from_clause=from_clause, where_clause=where_clause,
            where_params=where_params, id_col=id_col, key_col=key_col,
            cur_id=cur_id, cur_key=cur_key,
        ) + 1
        total = self._count_total(
            from_clause=from_clause, where_clause=where_clause,
            where_params=where_params,
        )
        return prev_uuid, next_uuid, index, total

    def _locate(
            self, *, cur_uuid, from_clause, where_clause, where_params,
            id_col, uuid_col, key_col,
    ) -> Optional[int]:
        found, cur_id, cur_key = self._fetch_key(
            from_clause=from_clause, where_clause=where_clause,
            where_params=where_params, id_col=id_col, uuid_col=uuid_col,
            key_col=key_col, cur_uuid=cur_uuid,
        )
        if not found:
            return None
        return self._count_before(
            from_clause=from_clause, where_clause=where_clause,
            where_params=where_params, id_col=id_col, key_col=key_col,
            cur_id=cur_id, cur_key=cur_key,
        ) + 1

    # ------------------------------------------------------------------ #
    # 邻居 / 定位 —— 私有工具（索引 seek，不物化全表）
    # ------------------------------------------------------------------ #
    def _fetch_key(self, *, from_clause, where_clause, where_params,
                   id_col, uuid_col, key_col, cur_uuid):
        """按 uuid 取当前项的 (id, 排序键)。

        返回的 cur_id 仅供内部 seek 使用，不向上层暴露。
        当前项不在集合内时返回 (False, None, None)。
        """
        row = self._fetchone(
            f"SELECT {id_col} AS rid, {key_col} AS k FROM {from_clause} "
            f"WHERE {where_clause} AND {uuid_col} = ?",
            (*where_params, cur_uuid),
        )
        if row is None:
            return False, None, None
        return True, row["rid"], row["k"]

    def _find_prev(self, *, from_clause, where_clause, where_params,
                   id_col, uuid_col, key_col, cur_id, cur_key):
        """严格排在 cur 前一项的 uuid；没有返回 None。

        集合排序：`(key IS NULL) ASC, key DESC, id DESC`。
        """
        if cur_key is not None:
            row = self._fetchone(
                f"""SELECT {uuid_col} AS nuuid FROM {from_clause}
                     WHERE {where_clause}
                       AND {key_col} IS NOT NULL
                       AND ({key_col} > ? OR ({key_col} = ? AND {id_col} > ?))
                     ORDER BY {key_col} ASC, {id_col} ASC
                     LIMIT 1""",
                (*where_params, cur_key, cur_key, cur_id),
            )
            return to_uuid(row["nuuid"]) if row else None

        row = self._fetchone(
            f"""SELECT {uuid_col} AS nuuid FROM {from_clause}
                 WHERE {where_clause}
                   AND {key_col} IS NULL
                   AND {id_col} > ?
                 ORDER BY {id_col} ASC LIMIT 1""",
            (*where_params, cur_id),
        )
        if row:
            return to_uuid(row["nuuid"])
        row = self._fetchone(
            f"""SELECT {uuid_col} AS nuuid FROM {from_clause}
                 WHERE {where_clause}
                   AND {key_col} IS NOT NULL
                 ORDER BY {key_col} ASC, {id_col} ASC LIMIT 1""",
            where_params,
        )
        return to_uuid(row["nuuid"]) if row else None

    def _find_next(self, *, from_clause, where_clause, where_params,
                   id_col, uuid_col, key_col, cur_id, cur_key):
        """严格排在 cur 后一项的 uuid；没有返回 None。"""
        if cur_key is not None:
            row = self._fetchone(
                f"""SELECT {uuid_col} AS nuuid FROM {from_clause}
                     WHERE {where_clause}
                       AND {key_col} IS NOT NULL
                       AND ({key_col} < ? OR ({key_col} = ? AND {id_col} < ?))
                     ORDER BY {key_col} DESC, {id_col} DESC
                     LIMIT 1""",
                (*where_params, cur_key, cur_key, cur_id),
            )
            if row:
                return to_uuid(row["nuuid"])
            row = self._fetchone(
                f"""SELECT {uuid_col} AS nuuid FROM {from_clause}
                     WHERE {where_clause}
                       AND {key_col} IS NULL
                     ORDER BY {id_col} DESC LIMIT 1""",
                where_params,
            )
            return to_uuid(row["nuuid"]) if row else None

        row = self._fetchone(
            f"""SELECT {uuid_col} AS nuuid FROM {from_clause}
                 WHERE {where_clause}
                   AND {key_col} IS NULL
                   AND {id_col} < ?
                 ORDER BY {id_col} DESC LIMIT 1""",
            (*where_params, cur_id),
        )
        return to_uuid(row["nuuid"]) if row else None

    def _count_before(self, *, from_clause, where_clause, where_params,
                      id_col, key_col, cur_id, cur_key) -> int:
        """严格排在 cur 之前的项数。"""
        if cur_key is not None:
            row = self._fetchone(
                f"""SELECT COUNT(*) AS cnt FROM {from_clause}
                     WHERE {where_clause}
                       AND {key_col} IS NOT NULL
                       AND ({key_col} > ? OR ({key_col} = ? AND {id_col} > ?))""",
                (*where_params, cur_key, cur_key, cur_id),
            )
            return int(row["cnt"]) if row else 0

        row = self._fetchone(
            f"""SELECT COUNT(*) AS cnt FROM {from_clause}
                 WHERE {where_clause} AND {key_col} IS NOT NULL""",
            where_params,
        )
        non_null = int(row["cnt"]) if row else 0
        row = self._fetchone(
            f"""SELECT COUNT(*) AS cnt FROM {from_clause}
                 WHERE {where_clause}
                   AND {key_col} IS NULL AND {id_col} > ?""",
            (*where_params, cur_id),
        )
        null_before = int(row["cnt"]) if row else 0
        return non_null + null_before

    def _count_total(self, *, from_clause, where_clause, where_params) -> int:
        row = self._fetchone(
            f"SELECT COUNT(*) AS cnt FROM {from_clause} WHERE {where_clause}",
            where_params,
        )
        return int(row["cnt"]) if row else 0

    # ==================================================================
    # 资产磁盘路径
    # ==================================================================

    def get_asset_path(
            self, asset_uuid: _UUIDLike, include_deleted: bool = False
    ) -> Optional[str]:
        """按 uuid 取磁盘完整路径。

        路径规则：
          · 有 source_path → ``Path(source_path) / file_path``；
          · 否则 file_path 视为绝对路径直接返回；
          · 资产不可见（默认按 active 语义：未软删且 source 未软删）
            且未 ``include_deleted`` → None。

        与 ``get_asset`` 同一套可见性条件，保证「DTO 能取到」与
        「路径能取到」不会分叉。
        """
        where = "a.uuid = ?"
        if not include_deleted:
            where += f" AND {self._ACTIVE_WHERE_A}"
        row = self._fetchone(
            f"""
            SELECT a.file_path   AS file_path,
                   s.source_path AS source_path
              FROM assets AS a
              LEFT JOIN ingest_source AS s
                     ON s.id = a.source_id
             WHERE {where}
            """,
            (as_uuid_str(asset_uuid),),
        )
        return self._resolve_full_path(row)

    @staticmethod
    def _resolve_full_path(row) -> Optional[str]:
        if row is None or not row["file_path"]:
            return None
        file_path = Path(row["file_path"])
        source_path = row["source_path"]
        if file_path.is_absolute() or not source_path:
            return str(file_path)
        return str(Path(source_path) / file_path)
