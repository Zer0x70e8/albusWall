#
""""""

import uuid
from sqlite3 import IntegrityError
from typing import List, Optional, Sequence

from albuswall.dto.album import AssetDTO
from albuswall.dto.tag import Tag, TagCreate, TagUpdate

from .base import BaseRepository
from ..dto.sentinel import UNSET
from ..utils.text import normalize_tag_name

# 与 AssetRepository / ThumbnailRepository 同量级，兼顾旧版 SQLite 变量上限
_IN_CLAUSE_CHUNK = 900


class TagRepository(BaseRepository):
    """标签视图仓储（只读）。

    接口契约（P1 收敛）：
        · 所有面向资产的公开方法只接受 uuid、只返回 uuid；
          ``assets.id`` / ``tags.id`` 仅作为内部索引 seek / tie-break 使用。
        · 资产绑定的 ``tag_uuid`` 指向 ``tags.uuid``（规范标签），不涉及别名；
          别名的解析 / 跳转由应用层完成后，再写 ``asset_tags``。
        · 「叶子」是动态状态（当前无子节点），不是标签类型；
          所有规范标签都可以绑定资产，本仓储不区分叶子 / 非叶子。
    """

    # ---------------- 排序契约（与 ViewRepository 同源） ----------------
    # {p} 会被替换成表别名前缀（例如 "a." 或 ""）。
    # 复制自 ViewRepository，保证 scope / album / tag 三处的「资产排序」
    # 完全一致，避免任意一处漂移。若后续要做单一来源，建议上提到
    # 共享 mixin / 基类，而不是让 TagRepository 继承 ViewRepository。
    _ASSET_ORDER_SQL: dict[str, str] = {
        "active": "{p}taken_at IS NULL, {p}taken_at DESC, {p}id DESC",
        "deleted": "{p}deleted_at IS NULL, {p}deleted_at DESC, {p}id DESC",
    }
    _ALLOWED_PREFIXES = frozenset(("", "a."))

    @classmethod
    def _order_by(cls, scope: str, prefix: str = "") -> str:
        if prefix not in cls._ALLOWED_PREFIXES:
            raise ValueError(f"unsupported prefix: {prefix!r}")
        return cls._ASSET_ORDER_SQL[scope].format(p=prefix)

    @staticmethod
    def _row_to_tag(row) -> Tag:
        return Tag(
            uuid=row["uuid"],
            parent_uuid=row["parent_uuid"],
            name=row["name"],
            norm_name=row["norm_name"],
            display_name=row["display_name"],
            description=row["description"],
            sort_order=row["sort_order"],
            is_deleted=bool(row["is_deleted"]),
            deleted_at=row["deleted_at"],
            created_at=row["created_at"],
            modified_at=row["modified_at"],
        )

    # ==================================================================
    # 标签枚举 / 单个标签
    # ==================================================================

    def list_tag_uuids(self) -> List[str]:
        """所有未被软删除的标签 uuid（按 sort_order, id 排序）。"""
        sql = """
                SELECT uuid
                  FROM tags
                 WHERE is_deleted = 0
                 ORDER BY sort_order ASC, id ASC
            """
        self.logger.trace("before _fetchall, sql=%s", sql)
        rows = self._fetchall(sql)
        self.logger.trace(
            "after _fetchall, type=%s, len=%s, repr=%r",
            type(rows).__name__,
            len(rows) if rows is not None else None,
            rows,
        )
        if not rows:
            return []
        return [row["uuid"] for row in rows]

    def get_tag(self, tag_uuid: str) -> Optional[Tag]:
        """按 uuid 获取未被软删除的标签 DTO。

        父链以 uuid 暴露（``parent_uuid``），不泄露内部 ``tags.id``；
        根节点 ``parent_uuid is None``。

        LEFT JOIN 只看 ``p.id = t.parent_id``，**不**附带 ``p.is_deleted = 0``：
        父标签被软删时，子标签的结构性父链仍然如实返回，
        是否可见由调用方自行用 ``get_tag(parent_uuid)`` 判断。
        若在 JOIN 上过滤软删，会把「孤儿」伪装成根节点，属于仓储层替应用做决策。
        """
        row = self._fetchone(
            """
            SELECT t.uuid,
                   p.uuid AS parent_uuid,
                   t.name,
                   t.norm_name,
                   t.display_name,
                   t.description,
                   t.sort_order,
                   t.is_deleted,
                   t.deleted_at,
                   t.created_at,
                   t.modified_at
              FROM tags AS t
              LEFT JOIN tags AS p ON p.id = t.parent_id
             WHERE t.uuid = ?
               AND t.is_deleted = 0
            """,
            (tag_uuid,),
        )
        if row is None:
            return None
        return self._row_to_tag(row)

    def list_tags(self) -> List[Tag]:
        """全量未删标签 DTO（UI 建树 / 展示用），一次 JOIN 出 parent_uuid。

        仅返回 ``is_deleted = 0`` 的标签。父链上若父标签已软删，
        ``parent_uuid`` 仍如实返回该软删标签的 uuid（不把孤儿伪装成根节点），
        是否可见由 service 层决定。

        排序与 ``list_tag_uuids`` 一致（sort_order, id），便于对齐展示顺序。
        """
        rows = self._fetchall(
            """
            SELECT t.uuid,
                   p.uuid AS parent_uuid,
                   t.name,
                   t.norm_name,
                   t.display_name,
                   t.description,
                   t.sort_order,
                   t.is_deleted,
                   t.deleted_at,
                   t.created_at,
                   t.modified_at
              FROM tags AS t
              LEFT JOIN tags AS p ON p.id = t.parent_id
             WHERE t.is_deleted = 0
             ORDER BY t.sort_order ASC, t.id ASC
            """
        )
        return [self._row_to_tag(row) for row in (rows or [])]

    # ==================================================================
    # 资产 <-> 标签
    # ==================================================================

    def get_tag_uuids_of_asset(self, asset_uuid: str) -> List[str]:
        """包含该资产的所有可见标签 uuid（排序同 list_tag_uuids）。"""
        rows = self._fetchall(
            """
            SELECT t.uuid
              FROM asset_tags AS at
              JOIN tags   AS t ON t.uuid = at.tag_uuid
              JOIN assets AS a ON a.id   = at.asset_id
             WHERE a.uuid = ?
               AND t.is_deleted = 0
               AND a.is_deleted = 0
             ORDER BY t.sort_order ASC, t.id ASC
            """,
            (asset_uuid,),
        )
        return [r["uuid"] for r in (rows or [])]

    def get_tags_of_asset(self, asset_uuid: str) -> List[Tag]:
        """单资产的所有可见标签完整 DTO（详情页用）。

        仅返回 ``tags.is_deleted = 0`` 且资产本身可见的标签；
        父链的软删语义同 ``get_tag`` / ``list_tags``：父标签软删时
        ``parent_uuid`` 仍如实返回，不在此处做可见性收口。
        """
        rows = self._fetchall(
            """
            SELECT t.uuid,
                   p.uuid AS parent_uuid,
                   t.name,
                   t.norm_name,
                   t.display_name,
                   t.description,
                   t.sort_order,
                   t.is_deleted,
                   t.deleted_at,
                   t.created_at,
                   t.modified_at
              FROM asset_tags AS at
              JOIN tags   AS t ON t.uuid = at.tag_uuid
              JOIN assets AS a ON a.id   = at.asset_id
              LEFT JOIN tags AS p ON p.id = t.parent_id
             WHERE a.uuid = ?
               AND a.is_deleted = 0
               AND t.is_deleted = 0
             ORDER BY t.sort_order ASC, t.id ASC
            """,
            (asset_uuid,),
        )
        return [self._row_to_tag(row) for row in (rows or [])]

    def count_assets(self, tag_uuid: str) -> int:
        row = self._fetchone(
            """
            SELECT COUNT(*) AS cnt
              FROM asset_tags AS at
              JOIN tags   AS t ON t.uuid = at.tag_uuid
              JOIN assets AS a ON a.id   = at.asset_id
             WHERE t.uuid = ?
               AND t.is_deleted = 0
               AND a.is_deleted = 0
            """,
            (tag_uuid,),
        )
        return int(row["cnt"]) if row else 0

    def count_assets_bulk(
            self, tag_uuids: Sequence[str]
    ) -> dict[str, int]:
        """批量统计标签下的可见资产数量。

        语义与 ``count_assets`` 一致：只统计 ``tags.is_deleted = 0`` 且
        ``assets.is_deleted = 0`` 的绑定。

        返回字典包含**所有**请求的 ``tag_uuid``：不存在 / 软删 / 无资产
        的键值为 0，便于调用方直接按 uuid 取值而不必再判断键是否存在。
        """
        result: dict[str, int] = {u: 0 for u in tag_uuids}
        if not result:
            return result

        uuids = list(result.keys())
        for start in range(0, len(uuids), _IN_CLAUSE_CHUNK):
            chunk = uuids[start:start + _IN_CLAUSE_CHUNK]
            placeholders = ",".join("?" * len(chunk))
            rows = self._fetchall(
                f"""
                SELECT t.uuid AS uuid,
                       COUNT(a.id) AS cnt
                  FROM tags AS t
                  LEFT JOIN asset_tags AS at ON at.tag_uuid = t.uuid
                  LEFT JOIN assets     AS a  ON a.id = at.asset_id
                                            AND a.is_deleted = 0
                 WHERE t.uuid IN ({placeholders})
                   AND t.is_deleted = 0
                 GROUP BY t.uuid
                """,
                chunk,
            )
            for r in (rows or []):
                result[r["uuid"]] = int(r["cnt"])
        return result

    # ==================================================================
    # 标签内资产
    # ==================================================================

    def list_assets(
            self,
            tag_uuid: str,
            offset: int = 0,
            limit: int = 100,
    ) -> List[AssetDTO]:
        """按唯一确定顺序分页获取标签内资产 DTO。

        排序读 ``assets.taken_at``（与 scope / album 排序同源）。

        TODO(P2): 大 OFFSET 后续可改为 cursor 分页。
        """
        order = self._order_by("active", prefix="a.")
        rows = self._fetchall(
            f"""
            SELECT a.*
              FROM asset_tags AS at
              JOIN tags   AS t ON t.uuid = at.tag_uuid
              JOIN assets AS a ON a.id   = at.asset_id
             WHERE t.uuid = ?
               AND t.is_deleted = 0
               AND a.is_deleted = 0
             ORDER BY {order}
             LIMIT ? OFFSET ?
            """,
            (tag_uuid, limit, offset),
        )
        return [AssetDTO.from_row(row) for row in (rows or [])]

    def list_asset_uuids_by_tag(self, tag_uuid: str) -> List[str]:
        """标签内所有可见 asset 的 uuid（同 JOIN / ORDER BY 契约）。"""
        order = self._order_by("active", prefix="a.")
        rows = self._fetchall(
            f"""
            SELECT a.uuid
              FROM asset_tags AS at
              JOIN tags   AS t ON t.uuid = at.tag_uuid
              JOIN assets AS a ON a.id   = at.asset_id
             WHERE t.uuid = ?
               AND t.is_deleted = 0
               AND a.is_deleted = 0
             ORDER BY {order}
            """,
            (tag_uuid,),
        )
        return [r["uuid"] for r in (rows or [])]

    # ==================================================================
    # 写：创建 / 更新
    # ==================================================================

    def create(self, tag: TagCreate) -> Optional[str]:
        """创建标签，返回新 uuid；父不存在或同层重名返回 None。"""
        new_uuid = str(uuid.uuid4())
        try:
            if tag.parent_uuid is None:
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
                     tag.parent_uuid),
                )
                if cursor.rowcount == 0:
                    return None  # 父不存在
            return new_uuid
        except IntegrityError as e:
            # idx_tags_parent_norm_uq 命中 → 同层重名
            if "unique" in str(e).lower():
                return None
            raise

    def update(self, tag_uuid: str, patch: TagUpdate) -> bool:
        """PATCH 更新标签；空 patch 或目标不存在返回 False。

        改名时同步重算 ``norm_name``，否则唯一索引会放过脏数据。
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

        params.append(tag_uuid)
        try:
            cursor = self._execute(
                f"UPDATE tags SET {', '.join(set_parts)} WHERE uuid = ? AND is_deleted = 0",
                params,
            )
        except IntegrityError as e:
            if "unique" in str(e).lower():
                return False
            raise
        return cursor.rowcount > 0

    def find_by_name(
            self, parent_uuid: Optional[str], name: str
    ) -> Optional[str]:
        """按同层 + 名字查标签 uuid；不存在返回 None。

        对外只暴露 ``find_by_name``：调用方给原始 ``name``，
        归一化在仓储内部完成，不要求调用方关心 ``norm_name`` 是内部表示。
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
                (parent_uuid, norm),
            )
        return row["uuid"] if row else None

    # ==================================================================
    # 写：软删 / 恢复 / 移动 / 存在性
    # ==================================================================

    def soft_delete(self, tag_uuid: str) -> bool:
        """软删除单个标签（幂等：已软删返回 False）。

        **不做级联**，与 ``AssetRepository.soft_delete`` 保持一致：
        子标签的 ``parent_id`` 仍指向本行，结构上变成「挂在软删父下」，
        查询时子标签自身仍可见、``parent_uuid`` 指向软删标签。
        是否拒绝、级联软删、还是放任孤儿，由 service 层基于
        ``list_tags()`` / ``get_tag()`` 自行决策。

        ``asset_tags`` 绑定保留不动：软删期间查询被 ``t.is_deleted = 0``
        过滤；恢复后绑定自动重现。
        """
        cursor = self._execute(
            """
            UPDATE tags
               SET is_deleted = 1,
                   deleted_at = strftime('%Y-%m-%dT%H:%M:%f','now')
             WHERE uuid = ?
               AND is_deleted = 0
            """,
            (tag_uuid,),
        )
        return cursor.rowcount > 0

    def restore(self, tag_uuid: str) -> bool:
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
                (tag_uuid,),
            )
        except IntegrityError as e:
            if "unique" in str(e).lower():
                return False
            raise
        return cursor.rowcount > 0

    def move(self, tag_uuid: str, new_parent_uuid: Optional[str]) -> bool:
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
        if new_parent_uuid == tag_uuid:
            return False  # 自环

        try:
            if new_parent_uuid is None:
                cursor = self._execute(
                    """
                    UPDATE tags
                       SET parent_id = NULL
                     WHERE uuid = ?
                       AND is_deleted = 0
                    """,
                    (tag_uuid,),
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
                    (new_parent_uuid, tag_uuid, new_parent_uuid),
                )
            return cursor.rowcount > 0
        except IntegrityError as e:
            if "unique" in str(e).lower():
                return False
            raise

    def exists(self, tag_uuid: str, *, include_deleted: bool = False) -> bool:
        """标签是否存在。

        ``include_deleted=False``（默认）只认可见标签；
        ``include_deleted=True`` 时软删标签也算存在（用于幂等判断 / 冲突提示）。
        """
        if include_deleted:
            row = self._fetchone(
                "SELECT 1 AS x FROM tags WHERE uuid = ?",
                (tag_uuid,),
            )
        else:
            row = self._fetchone(
                "SELECT 1 AS x FROM tags WHERE uuid = ? AND is_deleted = 0",
                (tag_uuid,),
            )
        return row is not None


"""
2. soft_delete 有活跃子节点时怎么办？

schema 里 parent_id 是 ON DELETE RESTRICT，但软删不触发外键。所以软删父标签后，子节点仍挂着软删的父——查询时子节点还可见，parent_uuid 指向一个软删标签。

repo 层不做级联（和 AssetRepository.soft_delete 一致），service 层决策：要么拒绝"删有子节点的标签"，要么先把子节点也软删，要么允许孤儿。现在 repo 提供 list_tags() 全量数据，service 自己能判断。
3. move 的环检测放哪

move(A, A) 自环——repo 层拒绝（一个 if tag_uuid == new_parent_uuid）。
move(A, A的子节点) 跨层环——service 层拒绝（repo 要做递归查询才能判断，太重）。docstring 里写清楚。
4. find_by_name vs find_by_norm_name

上一轮接口盘点用的是 find_by_norm_name(parent_uuid, norm_name)，但实现里 find_by_name(parent_uuid, name) 内部归一化。对外只暴露 find_by_name——调用方不该关心 norm_name 是内部表示。
"""
