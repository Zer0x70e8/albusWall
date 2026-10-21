#
"""标签仓储。

接口契约（P1 收敛）：
    · 所有面向资产的公开方法只接受 uuid、只返回 uuid；
      ``assets.id`` / ``tags.id`` 仅作为内部索引 seek / tie-break 使用。
    · uuid 入参统一收 ``UUID | str``，绑定前过 ``as_uuid_str``；
      uuid 出参统一为 ``UUID``（与 DTO 的 UUID 统一契约对齐）。
    · 资产绑定的 ``tag_uuid`` 指向 ``tags.uuid``（规范标签），不涉及别名；
      别名的解析 / 跳转由应用层完成后，再写 ``asset_tags``。
    · 「叶子」是动态状态（当前无子节点），不是标签类型；
      所有规范标签都可以绑定资产，本仓储不区分叶子 / 非叶子。
    · 行 → DTO 一律走 ``Tag.from_row``；``norm_name`` 归一化统一走
      ``dto.utils.normalize_tag_name``，仓储内不再保留副本。
"""

from __future__ import annotations

import uuid
from sqlite3 import IntegrityError
from typing import Optional, Sequence
from uuid import UUID

from albuswall.dto.album import AssetDTO
from albuswall.dto.sentinel import UNSET
from albuswall.dto.tag import Tag, TagCreate, TagUpdate
from albuswall.dto.utils import normalize_tag_name, to_uuid
from albuswall.utils.time import now_str

from .base import BaseRepository
from .utils.asset_order import order_by
from .utils.sql_helpers import as_uuid_str, chunked, placeholders
from .utils.types import UuidKeyed
from .utils.visibility import active_asset_where

_UUIDLike = UUID | str

# 「可见资产」条件（含 source 可见性）；标签统计 / 标签内列表共用。
_ACTIVE_ASSET_WHERE_A = active_asset_where("a")

_TAG_COLUMNS = """
           t.uuid,
           p.uuid AS parent_uuid,
           t.name,
           t.norm_name,
           t.display_name,
           t.description,
           t.sort_order,
           t.is_deleted,
           t.deleted_at,
           t.created_at,
           t.modified_at"""


def _tag_sql(*, where: str, joins: str = "", order: str = "") -> str:
    """拼装标签读查询，列清单只有 ``_TAG_COLUMNS`` 一份。

    可变部分（JOIN / WHERE / ORDER BY）按名字传入，避免调用点做
    ``_TAG_SELECT + " JOIN ..."`` 这种半截字符串拼接导致读起来断片。
    """
    sql = f"""
    SELECT {_TAG_COLUMNS}
      FROM tags AS t
      LEFT JOIN tags AS p ON p.id = t.parent_id
    """
    if joins:
        sql += joins
    sql += f" WHERE {where}"
    if order:
        sql += f" ORDER BY {order}"
    return sql


class TagRepository(BaseRepository):
    """标签仓储（读写）。

    读路径的父链语义：``LEFT JOIN`` 只看 ``p.id = t.parent_id``，
    **不**附带 ``p.is_deleted = 0``：父标签被软删时，子标签的结构性父链
    仍然如实返回（``parent_uuid`` 指向软删标签），是否可见由 service 判定。
    若在 JOIN 上过滤软删，会把「孤儿」伪装成根节点，属于仓储层替应用做决策。

    写路径的重名冲突（``idx_tags_parent_norm_uq``）统一捕获
    ``IntegrityError`` 并降级为 ``False`` / ``None``，由 service 翻译成
    显式异常。
    """

    # ==================================================================
    # 标签枚举 / 单个标签
    # ==================================================================

    def list_tag_uuids(self) -> list[UUID]:
        """所有未被软删除的标签 uuid（按 sort_order, id 排序）。"""
        rows = self._fetchall(
            """
            SELECT uuid
              FROM tags
             WHERE is_deleted = 0
             ORDER BY sort_order ASC, id ASC
            """
        )
        return [to_uuid(row["uuid"]) for row in rows]

    def get_tag(self, tag_uuid: _UUIDLike) -> Optional[Tag]:
        """按 uuid 获取未被软删除的标签 DTO。

        父链以 uuid 暴露（``parent_uuid``），不泄露内部 ``tags.id``；
        根节点 ``parent_uuid is None``。
        """
        return self._fetch_one_dto(
            Tag,
            _tag_sql(where="t.uuid = ? AND t.is_deleted = 0"),
            (as_uuid_str(tag_uuid),),
        )

    def list_tags(self) -> list[Tag]:
        """全量未删标签 DTO（UI 建树 / 展示用），一次 JOIN 出 parent_uuid。

        仅返回 ``is_deleted = 0`` 的标签。父链上若父标签已软删，
        ``parent_uuid`` 仍如实返回该软删标签的 uuid（不把孤儿伪装成根节点），
        是否可见由 service 层决定。

        排序与 ``list_tag_uuids`` 一致（sort_order, id），便于对齐展示顺序。
        """
        return self._fetch_all_dtos(
            Tag,
            _tag_sql(
                where="t.is_deleted = 0",
                order="t.sort_order ASC, t.id ASC",
            ),
        )

    # ==================================================================
    # 资产 <-> 标签
    # ==================================================================

    def get_tag_uuids_of_asset(self, asset_uuid: _UUIDLike) -> list[UUID]:
        """包含该资产的所有可见标签 uuid（排序同 list_tag_uuids）。"""
        rows = self._fetchall(
            f"""
            SELECT t.uuid
              FROM asset_tags AS at
              JOIN tags   AS t ON t.uuid = at.tag_uuid
              JOIN assets AS a ON a.id   = at.asset_id
             WHERE a.uuid = ?
               AND t.is_deleted = 0
               AND {_ACTIVE_ASSET_WHERE_A}
             ORDER BY t.sort_order ASC, t.id ASC
            """,
            (as_uuid_str(asset_uuid),),
        )
        return [to_uuid(row["uuid"]) for row in rows]

    def get_tags_of_asset(self, asset_uuid: _UUIDLike) -> list[Tag]:
        """单资产的所有可见标签完整 DTO（详情页用）。

        仅返回 ``tags.is_deleted = 0`` 且资产本身可见的标签；
        父链的软删语义同 ``get_tag`` / ``list_tags``：父标签软删时
        ``parent_uuid`` 仍如实返回，不在此处做可见性收口。
        """
        return self._fetch_all_dtos(
            Tag,
            _tag_sql(
                joins="""
              JOIN asset_tags AS at ON at.tag_uuid = t.uuid
              JOIN assets     AS a  ON a.id = at.asset_id""",
                where=(
                    f"a.uuid = ? AND t.is_deleted = 0"
                    f" AND {_ACTIVE_ASSET_WHERE_A}"
                ),
                order="t.sort_order ASC, t.id ASC",
            ),
            (as_uuid_str(asset_uuid),),
        )

    def count_assets(self, tag_uuid: _UUIDLike) -> int:
        row = self._fetchone(
            f"""
            SELECT COUNT(*) AS cnt
              FROM asset_tags AS at
              JOIN tags   AS t ON t.uuid = at.tag_uuid
              JOIN assets AS a ON a.id   = at.asset_id
             WHERE t.uuid = ?
               AND t.is_deleted = 0
               AND {_ACTIVE_ASSET_WHERE_A}
            """,
            (as_uuid_str(tag_uuid),),
        )
        return int(row["cnt"]) if row else 0

    def count_assets_bulk(
            self, tag_uuids: Sequence[_UUIDLike]
    ) -> UuidKeyed[int]:
        """批量统计标签下的可见资产数量。

        语义与 ``count_assets`` 一致：只统计 ``tags.is_deleted = 0`` 且
        ``assets.is_deleted = 0`` 的绑定。

        返回字典**包含所有请求的 uuid**：不存在 / 软删 / 无资产的键值为 0，
        便于调用方直接按 uuid 取值而不必再判断键是否存在。键是 canonical
        ``str(UUID)``（见 ``repositories/utils/types.py`` · ``UuidKeyed``），
        与缩略图批量查询的键口径一致；调用方用 ``str(tag.uuid)`` 取值。
        """
        requested = list(dict.fromkeys(as_uuid_str(u) for u in tag_uuids))
        result: UuidKeyed[int] = {u: 0 for u in requested}
        if not requested:
            return result

        for chunk in chunked(requested):
            rows = self._fetchall(
                f"""
                SELECT t.uuid AS uuid,
                       COUNT(a.id) AS cnt
                  FROM tags AS t
                  LEFT JOIN asset_tags AS at ON at.tag_uuid = t.uuid
                  LEFT JOIN assets     AS a  ON a.id = at.asset_id
                                            AND {_ACTIVE_ASSET_WHERE_A}
                 WHERE t.uuid IN ({placeholders(len(chunk))})
                   AND t.is_deleted = 0
                 GROUP BY t.uuid
                """,
                chunk,
            )
            for row in rows:
                result[row["uuid"]] = int(row["cnt"])
        return result

    # ==================================================================
    # 标签内资产
    # ==================================================================

    def list_assets(
            self,
            tag_uuid: _UUIDLike,
            offset: int = 0,
            limit: int = 100,
    ) -> list[AssetDTO]:
        """按唯一确定顺序分页获取标签内资产 DTO。

        排序读 ``assets.taken_at``（与 scope / album 排序同源）。

        TODO(P2): 大 OFFSET 后续可改为 cursor 分页。
        """
        order = order_by("active", prefix="a.")
        return self._fetch_all_dtos(
            AssetDTO,
            f"""
            SELECT a.*
              FROM asset_tags AS at
              JOIN tags   AS t ON t.uuid = at.tag_uuid
              JOIN assets AS a ON a.id   = at.asset_id
             WHERE t.uuid = ?
               AND t.is_deleted = 0
               AND {_ACTIVE_ASSET_WHERE_A}
             ORDER BY {order}
             LIMIT ? OFFSET ?
            """,
            (as_uuid_str(tag_uuid), limit, offset),
        )

    def list_asset_uuids_by_tag(self, tag_uuid: _UUIDLike) -> list[UUID]:
        """标签内所有可见 asset 的 uuid（同 JOIN / ORDER BY 契约）。"""
        order = order_by("active", prefix="a.")
        rows = self._fetchall(
            f"""
            SELECT a.uuid
              FROM asset_tags AS at
              JOIN tags   AS t ON t.uuid = at.tag_uuid
              JOIN assets AS a ON a.id   = at.asset_id
             WHERE t.uuid = ?
               AND t.is_deleted = 0
               AND {_ACTIVE_ASSET_WHERE_A}
             ORDER BY {order}
            """,
            (as_uuid_str(tag_uuid),),
        )
        return [to_uuid(row["uuid"]) for row in rows]

    # ==================================================================
    # 写：创建 / 更新
    # ==================================================================

    def create(self, tag: TagCreate) -> Optional[UUID]:
        """创建标签，返回新 uuid；父不存在或同层重名返回 None。

        ``TagCreate.parent_uuid`` 是 ``UUID``；写路径在这里负责
        ``UUID→str`` 绑定。
        """
        new_uuid = str(uuid.uuid4())
        parent_uuid = (
            None if tag.parent_uuid is None else as_uuid_str(tag.parent_uuid)
        )
        try:
            if parent_uuid is None:
                self._execute(
                    """
                    INSERT INTO tags (uuid, parent_id, name, norm_name,
                                      display_name, description, sort_order)
                    VALUES (?, NULL, ?, ?, ?, ?, ?)
                    """,
                    (new_uuid, tag.name, tag.norm_name,
                     tag.display_name, tag.description, tag.sort_order),
                )
            else:
                cursor = self._execute(
                    """
                    INSERT INTO tags (uuid, parent_id, name, norm_name,
                                      display_name, description, sort_order)
                    SELECT ?, p.id, ?, ?, ?, ?, ?
                      FROM tags AS p
                     WHERE p.uuid = ?
                       AND p.is_deleted = 0
                    """,
                    (new_uuid, tag.name, tag.norm_name,
                     tag.display_name, tag.description, tag.sort_order,
                     parent_uuid),
                )
                if cursor.rowcount == 0:
                    return None  # 父不存在
            return to_uuid(new_uuid)
        except IntegrityError as e:
            # idx_tags_parent_norm_uq 命中 → 同层重名
            if "unique" in str(e).lower():
                return None
            raise

    def update(self, tag_uuid: _UUIDLike, patch: TagUpdate) -> bool:
        """PATCH 更新标签；空 patch 或目标不存在返回 False。

        改名时同步重算 ``norm_name``（派生值由 ``TagUpdate`` 提供），
        否则唯一索引会放过脏数据。
        """
        if patch.is_empty():
            return False

        set_parts: list[str] = []
        params: list = []

        if patch.name is not UNSET:
            set_parts.append("name = ?")
            params.append(patch.name)
            # 关键：改名必须同步重算 norm_name
            set_parts.append("norm_name = ?")
            params.append(patch.derived_norm_name)
        if patch.display_name is not UNSET:
            set_parts.append("display_name = ?")
            params.append(patch.display_name)
        if patch.description is not UNSET:
            set_parts.append("description = ?")
            params.append(patch.description)
        if patch.sort_order is not UNSET:
            set_parts.append("sort_order = ?")
            params.append(patch.sort_order)

        params.append(as_uuid_str(tag_uuid))
        try:
            cursor = self._execute(
                f"UPDATE tags SET {', '.join(set_parts)} "
                f"WHERE uuid = ? AND is_deleted = 0",
                tuple(params),
            )
        except IntegrityError as e:
            if "unique" in str(e).lower():
                return False
            raise
        return cursor.rowcount > 0

    def find_by_name(
            self, parent_uuid: Optional[_UUIDLike], name: str
    ) -> Optional[UUID]:
        """按同层 + 名字查标签 uuid；不存在返回 None。

        对外只暴露 ``find_by_name``：调用方给原始 ``name``，
        归一化在仓储内部完成（``dto.utils.normalize_tag_name``），
        不要求调用方关心 ``norm_name`` 是内部表示。
        """
        norm = normalize_tag_name(name)
        if parent_uuid is None:
            row = self._fetchone(
                """
                SELECT uuid FROM tags
                 WHERE parent_id IS NULL
                   AND norm_name = ?
                   AND is_deleted = 0
                """,
                (norm,),
            )
        else:
            row = self._fetchone(
                """
                SELECT t.uuid FROM tags AS t
                  JOIN tags AS p ON p.id = t.parent_id
                 WHERE p.uuid = ?
                   AND t.norm_name = ?
                   AND t.is_deleted = 0
                """,
                (as_uuid_str(parent_uuid), norm),
            )
        return to_uuid(row["uuid"]) if row else None

    # ==================================================================
    # 写：软删 / 恢复 / 移动 / 存在性
    # ==================================================================

    def soft_delete(self, tag_uuid: _UUIDLike) -> bool:
        """软删除单个标签（幂等：已软删返回 False）。

        **不做级联**，与 ``AssetRepository.soft_delete`` 保持一致：
        子标签的 ``parent_id`` 仍指向本行，结构上变成「挂在软删父下」，
        查询时子标签自身仍可见、``parent_uuid`` 指向软删标签。
        是否拒绝、级联软删、还是放任孤儿，由 service 层基于
        ``list_tags()`` / ``get_tag()`` 自行决策。

        ``asset_tags`` 绑定保留不动：软删期间查询被 ``t.is_deleted = 0``
        过滤；恢复后绑定自动重现。``deleted_at`` 由 ``utils.time.now_str()``
        生成后绑参，时间格式只有一处来源。
        """
        cursor = self._execute(
            """
            UPDATE tags
               SET is_deleted = 1,
                   deleted_at = ?
             WHERE uuid = ?
               AND is_deleted = 0
            """,
            (now_str(), as_uuid_str(tag_uuid)),
        )
        return cursor.rowcount > 0

    def restore(self, tag_uuid: _UUIDLike) -> bool:
        """恢复软删标签（幂等：未删除返回 False）。

        **不校验父链**：若父标签仍处于软删状态，恢复后的标签会
        ``parent_uuid`` 指向软删父。是否要求父先恢复由 service 层决定。
        同层重名冲突（软删期间别人建了同名标签）会触发 IntegrityError，
        此处按唯一冲突返回 False，由 service 层提示冲突原因。
        """
        try:
            cursor = self._execute(
                """
                UPDATE tags
                   SET is_deleted = 0,
                       deleted_at = NULL
                 WHERE uuid = ?
                   AND is_deleted = 1
                """,
                (as_uuid_str(tag_uuid),),
            )
        except IntegrityError as e:
            if "unique" in str(e).lower():
                return False
            raise
        return cursor.rowcount > 0

    def move(
            self,
            tag_uuid: _UUIDLike,
            new_parent_uuid: Optional[_UUIDLike],
    ) -> bool:
        """把标签移动到新父下（``new_parent_uuid is None`` 表示移到根）。

        层级检查分工：
            · **自环** ``move(A, A)`` 在 repo 层拒绝（一个 if）；
            · **跨层环** ``move(A, A 的后代)`` 需要递归查询，太重，
              由 service 层在调用前自行校验；
            · 目标父不存在 / 软删，返回 False；
            · 目标层已有同名标签（``idx_tags_parent_norm_uq``），
              捕获 IntegrityError 返回 False。

        移动到根时，若原本已是根，视为幂等成功（UPDATE 命中 1 行）。
        """
        tag = to_uuid(tag_uuid)
        parent = (
            None if new_parent_uuid is None else to_uuid(new_parent_uuid)
        )
        if parent == tag:
            return False  # 自环

        tag_value = str(tag)
        parent_value = None if parent is None else str(parent)
        try:
            if parent_value is None:
                cursor = self._execute(
                    """
                    UPDATE tags
                       SET parent_id = NULL
                     WHERE uuid = ?
                       AND is_deleted = 0
                    """,
                    (tag_value,),
                )
            else:
                cursor = self._execute(
                    """
                    UPDATE tags
                       SET parent_id = (
                               SELECT id FROM tags
                                WHERE uuid = ?
                                  AND is_deleted = 0
                           )
                     WHERE uuid = ?
                       AND is_deleted = 0
                       AND EXISTS (
                               SELECT 1 FROM tags
                                WHERE uuid = ?
                                  AND is_deleted = 0
                           )
                    """,
                    (parent_value, tag_value, parent_value),
                )
            return cursor.rowcount > 0
        except IntegrityError as e:
            if "unique" in str(e).lower():
                return False
            raise

    def exists(
            self, tag_uuid: _UUIDLike, *, include_deleted: bool = False
    ) -> bool:
        """标签是否存在。

        ``include_deleted=False``（默认）只认可见标签；
        ``include_deleted=True`` 时软删标签也算存在（用于幂等判断 / 冲突提示）。
        """
        value = as_uuid_str(tag_uuid)
        if include_deleted:
            row = self._fetchone(
                "SELECT 1 AS x FROM tags WHERE uuid = ?", (value,)
            )
        else:
            row = self._fetchone(
                "SELECT 1 AS x FROM tags WHERE uuid = ? AND is_deleted = 0",
                (value,),
            )
        return row is not None
