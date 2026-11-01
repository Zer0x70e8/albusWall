#
"""资产写操作仓储。

写操作专属：所有会改动 ``assets`` / ``album_assets`` / ``asset_tags``
状态的 SQL 都集中在此。
**不要和 ViewRepository 混用** —— 读查询一律走 ``ViewRepository``。

接口契约（P1 收敛）：
    · 所有公开方法以 ``asset uuid`` 为键，**不暴露 assets.id**。
    · uuid 入参收 ``UUID | str``（DTO 侧是 ``UUID``），绑定前统一过
      ``as_uuid_str``；``assets.id`` 仅在 SQL 内部作为外键
      （``album_assets.asset_id`` / ``asset_tags.asset_id``）使用。
    · 批量语句统一走 ``chunked`` + ``placeholders``，单事务提交。

磁盘文件契约：
    * ``purge`` / ``empty_trash`` 只删数据库行，**不碰任何磁盘文件**；
    * 缩略图文件应由调用方先行通过 ``ThumbnailService.purge_bulk``
      （或 ``ThumbnailRepository.clear_and_snapshot_bulk_by_uuid``）拿到
      路径快照，再由调用方自行 unlink；
    * 原图文件同理，由调用方决定去留。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence
from uuid import UUID

from albuswall.dto.utils import to_uuid
from albuswall.utils.time import now_str

from .base import BaseRepository
from .utils.sql_helpers import as_uuid_str, chunked, placeholders
from .utils.visibility import active_asset_where

_UUIDLike = UUID | str

# 「可见资产」条件（含 source 可见性）；成员增删的写入前置共用。
_ACTIVE_ASSET_WHERE_A = active_asset_where("a")

# 「相册-资产成员关系可写」条件：相册未软删 + 资产可见（含 source 可见）。
#
# 这是成员写入（``add_to_album`` / ``move_between_albums`` 的
# ``INSERT OR IGNORE ... SELECT``）与成员判定（``memberships``）的**唯一
# 条件来源**。复制一份的后果是"判定为不在相册 → 写入被 IGNORE 掉"这类
# 静默漂移，因此禁止就地重写。
_ALBUM_VISIBLE_WHERE = "al.is_deleted = 0"
_MEMBERSHIP_VISIBLE_WHERE = f"{_ALBUM_VISIBLE_WHERE} AND {_ACTIVE_ASSET_WHERE_A}"

# 成员写入语句（``add_to_album`` 与 ``move_between_albums`` 共用同一份 SQL）。
# 绑定参数顺序固定：``(asset_uuid, album_uuid)``。
#   · 表：``album_assets``（写）/ ``albums``（定位 album_id）/ ``assets``（定位 asset_id）
#   · 条件：``al.is_deleted = 0`` 且资产可见（见 ``_MEMBERSHIP_VISIBLE_WHERE``）
#   · ``asset_taken_at`` 冗余列从 ``assets.taken_at`` 同步
_ADD_MEMBER_SQL = f"""
    INSERT OR IGNORE INTO album_assets (album_id, asset_id, asset_taken_at)
    SELECT al.id, a.id, a.taken_at
      FROM albums AS al
      JOIN assets AS a ON a.uuid = ?
     WHERE al.uuid = ?
       AND {_MEMBERSHIP_VISIBLE_WHERE}
"""

# 成员判定语句：``(album_uuid, *asset_uuids)``。
# 命中即"该资产已经是该相册的成员，且满足成员可写条件"。
_MEMBERSHIP_QUERY_SQL = f"""
    SELECT a.uuid
      FROM albums AS al
      JOIN album_assets AS aa ON aa.album_id = al.id
      JOIN assets AS a ON a.id = aa.asset_id
     WHERE al.uuid = ?
       AND a.uuid IN ({{ph}})
       AND {_MEMBERSHIP_VISIBLE_WHERE}
"""


# ---------------------------------------------------------------------------
# move_between_albums 的跳过原因（repo 层词汇，**不依赖 dto 层**）
# ---------------------------------------------------------------------------
class MoveSkipReason:
    """``AssetRepository.move_between_albums`` 的单条跳过原因。

    取值与 ``AssetOpsService`` 的 ``SkipReason`` 是两套词汇：repo 层只描述
    "数据库视角发生了什么"，翻成领域/UI 语义由服务层负责（D4：repo 不
    import ``dto.asset``）。``AssetOpsService`` 里的映射表与这里一一对应。
    """

    ASSET_MISSING: str = "asset_missing"
    """资产 uuid 不存在。"""

    ASSET_INVISIBLE: str = "asset_invisible"
    """资产不可见（``assets.is_deleted = 1`` 或所属 source 已软删）。"""

    SOURCE_INVISIBLE: str = "source_invisible"
    """源相册不存在或已软删（源侧整体不可用，含源相册行缺失）。"""

    DST_ALBUM_MISSING: str = "dst_album_missing"
    """目标相册行不存在。"""

    DST_ALBUM_DELETED: str = "dst_album_deleted"
    """目标相册存在但已软删。"""

    SRC_EQUALS_DST: str = "src_equals_dst"
    """源相册与目标相册相同（纯 no-op，未写任何行）。"""

    NOT_IN_SOURCE: str = "not_in_source"
    """资产可见，但本来就不在源相册里。"""


@dataclass(frozen=True, slots=True)
class MoveSkip:
    """repo 层的单条跳过记录（``asset_uuid`` + ``reason``）。

    故意不复用 ``dto/asset/events.SkippedAsset``：repo 层不依赖 dto 层（D4），
    由 ``AssetOpsService`` 翻译成对外 DTO。
    """

    asset_uuid: UUID
    reason: str


@dataclass(frozen=True, slots=True)
class MoveOutcome:
    """``move_between_albums`` 的 repo 层结果。

    * ``moved``：**从源相册移除**（并按可写条件补进目标相册）的资产。
      含"本来就在目标相册"的那些——它们只被从源相册摘掉，因此
      ``already_in_target`` ⊆ ``moved``（服务层 docstring 同口径）。
    * ``already_in_target``：``moved`` 中在移动前就已属于目标相册的子集，
      仅供上层区分"这次真的加了新成员"与"只是从源侧摘掉"。
    * ``skipped``：未处理的资产 + 原因（见 ``MoveSkipReason``）。
    * ``noop_reason``：整批 no-op 的原因（目前只有 ``src_equals_dst``），
      非 no-op 时为 ``None``。
    """

    moved: tuple[UUID, ...] = ()
    already_in_target: tuple[UUID, ...] = ()
    skipped: tuple[MoveSkip, ...] = ()
    noop_reason: Optional[str] = None


def _dedupe_uuids(values: Sequence[_UUIDLike]) -> list[str]:
    """去重（保持顺序）并转成 SQL 可绑定的 ``str``。"""
    return [as_uuid_str(v) for v in dict.fromkeys(values)]


# noinspection SpellCheckingInspection
class AssetRepository(BaseRepository):
    """资产（assets）写操作仓储。

    **写操作专属，不要和 ViewRepository 混用。**

    职责边界：
        · 收藏切换（``set_favorite``）；
        · 软删 / 恢复（``soft_delete*`` / ``restore*``）；
        · 硬删与清空回收站（``purge*`` / ``empty_trash``）；
        · 相簿成员增删（``add_to_album`` / ``remove_from_album``）；
        · 标签成员增删（``add_tag*`` / ``remove_tag*``）。
        读侧（列表、封面、邻居定位等）请使用 ``ViewRepository``。

    软删语义：
        · 软删写 ``is_deleted = 1`` + ``deleted_at = 当前 UTC``；
        · 恢复写 ``is_deleted = 0`` + ``deleted_at = NULL``；
        · 两者均幂等：对已经在目标状态的资产再次执行返回 0 行。

    磁盘文件契约：
        · ``purge`` / ``empty_trash`` 只删数据库行，**不碰磁盘**；
        · 缩略图清理路径示例（调用方编排）::

              uuids = view_repo.list_asset_uuids_by_scope("deleted")
              snapshots = thumbnail_repo.clear_and_snapshot_bulk_by_uuid(uuids)
              # 调用方遍历 snapshots 删磁盘文件
              asset_repo.purge_many(uuids)                  # 删行
    """

    # ==================================================================
    # 收藏
    # ==================================================================

    def set_favorite(self, asset_uuid: _UUIDLike, favorite: bool) -> int:
        """设置 / 取消收藏。返回受影响行数（0 表示资产不存在）。"""
        cursor = self._execute(
            "UPDATE assets SET is_favorite = ? WHERE uuid = ?",
            (1 if favorite else 0, as_uuid_str(asset_uuid)),
        )
        return cursor.rowcount

    # ==================================================================
    # 软删 / 恢复
    # ==================================================================

    def soft_delete(self, asset_uuid: _UUIDLike) -> int:
        """软删除单个资产（幂等：已软删返回 0 行）。

        ``deleted_at`` 由 Python 侧按 ``utils.time`` 的统一格式生成后绑参，
        不在 SQL 内写 ``strftime``：时间的唯一格式来源留在 Python。
        """
        cursor = self._execute(
            """
            UPDATE assets
               SET is_deleted = 1,
                   deleted_at = ?
             WHERE uuid = ?
               AND is_deleted = 0
            """,
            (now_str(), as_uuid_str(asset_uuid)),
        )
        return cursor.rowcount

    def soft_delete_many(self, asset_uuids: Sequence[_UUIDLike]) -> int:
        """批量软删，单事务提交。返回实际变更行数。"""
        return self._update_many(
            """
            UPDATE assets
               SET is_deleted = 1,
                   deleted_at = ?
             WHERE uuid IN ({ph})
               AND is_deleted = 0
            """,
            _dedupe_uuids(asset_uuids),
            prefix=(now_str(),),
        )

    def soft_delete_by_uuids(
            self, asset_uuids: Sequence[_UUIDLike]
    ) -> set[UUID]:
        """批量软删，返回**实际被软删**的 uuid 集合（G2）。

        做什么：按 ``IN (...)`` 分批，在**同一个事务**里先读"当前未软删的
        行"，再对这些行写 ``is_deleted = 1, deleted_at = now``。返回值只
        包含真的从 0 改成 1 的那些 uuid。
        返回：``set[UUID]``。不存在的、已经软删的、以及事务内并发被改走
        的 uuid 都**不在**集合里（调用方据此区分 changed / already_deleted）。
        抛：底层 sqlite 异常直接穿透（事务已回滚，无部分生效）。
        前置条件：无。
        原子性：**单事务**；涉及表仅 ``assets``（``is_deleted`` /
        ``deleted_at``）。``deleted_at`` 由 Python 侧 ``now_str()`` 生成，
        与 ``soft_delete`` / ``soft_delete_many`` 同一时间口径。
        ``trg_assets_modified_at`` 触发器会顺带刷新 ``modified_at``。

        关系：语义同 ``soft_delete_many``，差别只在返回值——后者返回
        rowcount，无法回答"哪几条真的变了"；本方法是 ``trash.changed``
        的唯一可信来源。
        """
        uuids = _dedupe_uuids(asset_uuids)
        if not uuids:
            return set()

        changed: set[UUID] = set()
        with self._transaction() as conn:
            for chunk in chunked(uuids):
                rows = conn.execute(
                    f"""
                    SELECT uuid FROM assets
                     WHERE is_deleted = 0
                       AND uuid IN ({placeholders(len(chunk))})
                    """,
                    tuple(chunk),
                ).fetchall()
                confirmed = [row["uuid"] for row in rows]
                if not confirmed:
                    continue
                conn.execute(
                    f"""
                    UPDATE assets
                       SET is_deleted = 1,
                           deleted_at = ?
                     WHERE uuid IN ({placeholders(len(confirmed))})
                       AND is_deleted = 0
                    """,
                    (now_str(), *confirmed),
                )
                changed.update(to_uuid(value) for value in confirmed)
        return changed

    def restore(self, asset_uuid: _UUIDLike) -> int:
        """恢复单个软删资产（幂等：未删除返回 0 行）。"""
        cursor = self._execute(
            """
            UPDATE assets
               SET is_deleted = 0,
                   deleted_at = NULL
             WHERE uuid = ?
               AND is_deleted = 1
            """,
            (as_uuid_str(asset_uuid),),
        )
        return cursor.rowcount

    def restore_many(self, asset_uuids: Sequence[_UUIDLike]) -> int:
        """批量恢复，单事务提交。返回实际变更行数。"""
        return self._update_many(
            """
            UPDATE assets
               SET is_deleted = 0,
                   deleted_at = NULL
             WHERE uuid IN ({ph})
               AND is_deleted = 1
            """,
            _dedupe_uuids(asset_uuids),
        )

    # TODO 预留，可能没用
    # # ==================================================================
    # # 硬删 / 清空回收站
    # # ==================================================================
    #
    # def purge(self, asset_uuid: _UUIDLike) -> int:
    #     """永久删除单个资产行。
    #
    #     - ``album_assets`` / ``asset_tags`` 走 ON DELETE CASCADE；
    #     - ``albums.cover_asset_id`` 走 ON DELETE SET NULL；
    #     - **不触碰磁盘文件**（缩略图 / 原图由调用方编排）。
    #     """
    #     cursor = self._execute(
    #         "DELETE FROM assets WHERE uuid = ?", (as_uuid_str(asset_uuid),)
    #     )
    #     return cursor.rowcount
    #
    # def purge_many(self, asset_uuids: Sequence[_UUIDLike]) -> int:
    #     """批量硬删，单事务提交。磁盘契约同 ``purge``。"""
    #     return self._update_many(
    #         "DELETE FROM assets WHERE uuid IN ({ph})",
    #         _dedupe_uuids(asset_uuids),
    #     )
    #
    # def empty_trash(self) -> int:
    #     """清空回收站：硬删所有 ``is_deleted = 1`` 的资产。
    #
    #     **不触碰磁盘文件**。推荐调用方先
    #     ``ViewRepository.list_asset_uuids_by_scope("deleted")`` 取一遍 uuid
    #     交给缩略图服务收集快照、自行删盘，再调本方法删行。
    #     """
    #     cursor = self._execute("DELETE FROM assets WHERE is_deleted = 1")
    #     return cursor.rowcount

    # ==================================================================
    # 相簿成员
    # ==================================================================

    def add_to_album(
            self, album_uuid: _UUIDLike, asset_uuid: _UUIDLike
    ) -> int:
        """把资产加入指定相簿。

        - 相簿或资产不可见（is_deleted = 1）时不会写入；
        - 已存在的关系通过 INSERT OR IGNORE 跳过（天然幂等）；
        - ``asset_taken_at`` 冗余列从 ``assets.taken_at`` 同步。
        """
        # 语句与条件来自模块级 ``_ADD_MEMBER_SQL``（与 ``memberships`` /
        # ``move_between_albums`` 共用同一份，见文件头常量注释）。
        cursor = self._execute(
            _ADD_MEMBER_SQL,
            (as_uuid_str(asset_uuid), as_uuid_str(album_uuid)),
        )
        return cursor.rowcount

    def remove_from_album(
            self, album_uuid: _UUIDLike, asset_uuid: _UUIDLike
    ) -> int:
        """把资产从指定相簿移除（相簿不可见时不做任何事）。"""
        cursor = self._execute(
            """
            DELETE FROM album_assets
             WHERE album_id = (
                       SELECT id FROM albums
                        WHERE uuid = ?
                          AND is_deleted = 0
                   )
               AND asset_id = (
                       SELECT id FROM assets WHERE uuid = ?
                   )
            """,
            (as_uuid_str(album_uuid), as_uuid_str(asset_uuid)),
        )
        return cursor.rowcount

    def memberships(
            self,
            album_uuid: _UUIDLike,
            asset_uuids: Sequence[_UUIDLike],
    ) -> dict[UUID, bool]:
        """批量判定"这些资产是否**已经是该相册的成员**"（A2 / 原 G5）。

        做什么：一次 ``IN (...)`` 批量查询（超过 ``IN_CLAUSE_CHUNK`` 自动
        分片），条件与 ``add_to_album`` 的 ``INSERT ... SELECT``
        **完全同源**（模块级 ``_MEMBERSHIP_VISIBLE_WHERE``）：
        ``albums.is_deleted = 0`` 且资产可见（``assets.is_deleted = 0`` 且
        所属 ``ingest_source.is_deleted = 0``）。
        返回：``{UUID: bool}``，键是**归一化后的 UUID**（不是 str），
        入参里的每个 uuid 都会出现在结果里。
        语义边界：uuid 不存在 → ``False``（不抛、不区分"不存在"与"不可见"；
        需要区分的调用方先用 ``ViewRepository.classify_by_uuids``）；
        相册不存在 / 已软删 → 全部 ``False``。
        抛：不抛（非法 uuid 字符串按"不是成员"处理）。
        前置条件：无。
        原子性：只读快照；多分片之间不保证同一快照。
        关系：替代 ``ViewRepository.get_album_uuids_of_asset`` 的批量用法
        （后者是逐资产 N+1，且不表达"可写成员关系"）。不影响
        ``add_to_album`` / ``remove_from_album`` 的既有行为。
        """
        requested: list[UUID] = []
        seen: set[UUID] = set()
        for value in asset_uuids:
            try:
                parsed = to_uuid(value)
            except (ValueError, AttributeError, TypeError):
                continue
            if parsed in seen:
                continue
            seen.add(parsed)
            requested.append(parsed)
        if not requested:
            return {}

        result: dict[UUID, bool] = {uuid: False for uuid in requested}
        album = as_uuid_str(album_uuid)
        for chunk in chunked([str(u) for u in requested]):
            rows = self._fetchall(
                _MEMBERSHIP_QUERY_SQL.replace(
                    "{ph}", placeholders(len(chunk)),
                ),
                (album, *chunk),
            )
            for row in rows:
                result[to_uuid(row["uuid"])] = True
        return result

    # ------------------------------------------------------------------
    # 批量成员写（``AssetOpsService`` 的写用例走这两个）
    # ------------------------------------------------------------------
    #
    # 为什么单条之外还要批量版：``add_to_album`` / ``remove_from_album`` 是
    # "一条资产"的接口，返回 ``rowcount``；而服务层的用例主语是**一批资产**，
    # 需要"哪些真的变成功了"的 uuid 集合来记账（撤销栈只认实际发生的事）。
    # 用 ``rowcount`` 反推不出是哪一个，所以批量版在事务内做
    # "写前读 → 写 → 写后读"，用差集给出精确集合。

    def add_many_to_album(
            self,
            album_uuid: _UUIDLike,
            asset_uuids: Sequence[_UUIDLike],
    ) -> set[UUID]:
        """批量加入相册，返回**真正新增**的 asset uuid 集合。

        条件与 :meth:`add_to_album` 完全同源（相簿可见 + 资产可见），已存在
        的成员被 ``INSERT OR IGNORE`` 跳过且不出现在返回值里。
        """
        wanted = _dedupe_uuids(asset_uuids)
        if not wanted:
            return set()
        requested = [to_uuid(u) for u in wanted]

        before = self.memberships(album_uuid, requested)
        album = as_uuid_str(album_uuid)
        with self._transaction() as conn:
            for chunk in chunked(wanted):
                conn.execute(
                    f"""
                    INSERT OR IGNORE INTO album_assets
                           (album_id, asset_id, asset_taken_at)
                    SELECT al.id, a.id, a.taken_at
                      FROM albums AS al
                      JOIN assets AS a ON a.uuid IN ({placeholders(len(chunk))})
                     WHERE al.uuid = ?
                       AND {_MEMBERSHIP_VISIBLE_WHERE}
                    """,
                    (*chunk, album),
                )
        after = self.memberships(album_uuid, requested)
        return {u for u in requested if after.get(u) and not before.get(u)}

    def remove_many_from_album(
            self,
            album_uuid: _UUIDLike,
            asset_uuids: Sequence[_UUIDLike],
    ) -> set[UUID]:
        """批量移出相册，返回**真正移除**的 asset uuid 集合。

        与 :meth:`remove_from_album` 同源：相簿不可见时一行都不动。
        """
        wanted = _dedupe_uuids(asset_uuids)
        if not wanted:
            return set()
        requested = [to_uuid(u) for u in wanted]

        before = self.memberships(album_uuid, requested)
        album = as_uuid_str(album_uuid)
        with self._transaction() as conn:
            for chunk in chunked(wanted):
                conn.execute(
                    f"""
                    DELETE FROM album_assets
                     WHERE album_id = (
                               SELECT id FROM albums
                                WHERE uuid = ? AND is_deleted = 0
                           )
                       AND asset_id IN (
                               SELECT id FROM assets
                                WHERE uuid IN ({placeholders(len(chunk))})
                           )
                    """,
                    (album, *chunk),
                )
        after = self.memberships(album_uuid, requested)
        return {u for u in requested if before.get(u) and not after.get(u)}

    def move_between_albums(
            self,
            src_album_uuid: _UUIDLike,
            dst_album_uuid: _UUIDLike,
            asset_uuids: Sequence[_UUIDLike],
    ) -> MoveOutcome:
        """把一批资产从源相册移动到目标相册（A3 / 原 G9，**真原子**）。

        做什么：在**一个事务**里完成"预读 → 从源相册删成员 → 按可写条件
        补进目标相册"，返回 repo 层结果 ``MoveOutcome``。

        事务范围（单事务 / 失败回滚范围）
        ---------------------------------
        ``with self._transaction()``：块内所有语句共用同一连接与同一事务；
        任何一条抛异常（含 ``sqlite3.IntegrityError``）→ **整批回滚**，
        数据回到调用前状态，异常原样穿透（本方法不吞、不翻译）。
        涉及表：``albums``（只读：id / is_deleted）、``assets``（只读：
        id / 可见性）、``album_assets``（读 + INSERT OR IGNORE + DELETE）。

        判定全部基于**事务内预读**，不看 ``rowcount``：
        ``moved`` / ``already_in_target`` / ``skipped`` 由预读结果决定。

        与其它方法的关系
        ----------------
        * 新语义接口：替代"循环调用 ``remove_from_album`` + ``add_to_album``"
          （那会拆成两条语句、两个事务，产生"删了没加"的中间态）；
        * ``src == dst`` 早退：**不写任何行**，``noop_reason`` =
          ``src_equals_dst``，``moved`` 为空；
        * 不动 ``add_to_album`` / ``remove_from_album`` 的既有行为。

        不碰 ``assets.modified_at``：只写 ``album_assets``。``album_assets``
        没有触发器，若补写 ``modified_at`` 会被缩略图补齐队列当成"资产变了"
        而重算缩略图——那是副作用，不是本方法该有的行为。
        ``album_assets.added_at`` 也不改（本轮约定，见执行报告"默认处理"）。

        磁盘文件契约：同文件头——不碰任何磁盘文件。

        Args:
            src_album_uuid: 源相册 uuid。
            dst_album_uuid: 目标相册 uuid。
            asset_uuids: 待移动资产 uuid；自动去重保序。

        Returns:
            ``MoveOutcome``。``already_in_target`` 是 ``moved`` 的子集
            （既在源又在目标的资产只从源侧摘掉）。

        抛：底层 sqlite 异常穿透（``IntegrityError`` 等，事务已回滚）。

        前置条件：无（相册不存在 / 不可见都不抛，转成
        ``skipped(dst_album_missing | dst_album_deleted | source_invisible)``；
        目标侧原因优先于源侧，与服务层 skip 语义一致）。
        """
        uuids = _dedupe_uuids(asset_uuids)
        if not uuids:
            return MoveOutcome()

        src = as_uuid_str(src_album_uuid)
        dst = as_uuid_str(dst_album_uuid)
        ordered = [to_uuid(value) for value in uuids]

        with self._transaction() as conn:
            # ---- 1. 预读相册：id + 是否软删（albums） ----
            album_rows = conn.execute(
                """
                SELECT id, uuid, is_deleted
                  FROM albums
                 WHERE uuid IN (?, ?)
                """,
                (src, dst),
            ).fetchall()
            albums = {row["uuid"]: (row["id"], bool(row["is_deleted"]))
                      for row in album_rows}
            src_row = albums.get(src)
            dst_row = albums.get(dst)

            # ---- 2. 相册级不可用：目标优先（与 Service 的 skip 语义一致） ----
            if dst_row is None:
                return self._skip_all_move(
                    ordered, MoveSkipReason.DST_ALBUM_MISSING,
                )
            if dst_row[1]:
                return self._skip_all_move(
                    ordered, MoveSkipReason.DST_ALBUM_DELETED,
                )
            if src_row is None or src_row[1]:
                return self._skip_all_move(
                    ordered, MoveSkipReason.SOURCE_INVISIBLE,
                )

            src_id, dst_id = src_row[0], dst_row[0]

            # ---- 3. 预读资产：id + 可见性（assets + ingest_source） ----
            by_uuid: dict[UUID, tuple[int, bool]] = {}
            for chunk in chunked(uuids):
                rows = conn.execute(
                    f"""
                    SELECT a.id, a.uuid,
                           CASE WHEN {_ACTIVE_ASSET_WHERE_A}
                                THEN 1 ELSE 0 END AS is_visible
                      FROM assets AS a
                     WHERE a.uuid IN ({placeholders(len(chunk))})
                    """,
                    tuple(chunk),
                ).fetchall()
                for row in rows:
                    by_uuid[to_uuid(row["uuid"])] = (
                        row["id"], bool(row["is_visible"]),
                    )

            # ---- 4. 预读成员关系（album_assets，两个相册一次查） ----
            asset_ids = [by_uuid[u][0] for u in ordered if u in by_uuid]
            in_src: set[UUID] = set()
            in_dst: set[UUID] = set()
            id_to_uuid = {
                by_uuid[u][0]: u for u in ordered if u in by_uuid
            }
            for chunk in chunked(asset_ids):
                rows = conn.execute(
                    f"""
                    SELECT album_id, asset_id
                      FROM album_assets
                     WHERE album_id IN (?, ?)
                       AND asset_id IN ({placeholders(len(chunk))})
                    """,
                    (src_id, dst_id, *chunk),
                ).fetchall()
                for row in rows:
                    uuid = id_to_uuid.get(row["asset_id"])
                    if uuid is None:
                        continue
                    if row["album_id"] == src_id:
                        in_src.add(uuid)
                    if row["album_id"] == dst_id:
                        in_dst.add(uuid)

            # ---- 5. 逐条分类（全部基于预读） ----
            skipped: list[MoveSkip] = []
            already_in_target: list[UUID] = []
            to_move: list[UUID] = []
            for uuid in ordered:
                info = by_uuid.get(uuid)
                if info is None:
                    skipped.append(MoveSkip(uuid, MoveSkipReason.ASSET_MISSING))
                elif not info[1]:
                    skipped.append(
                        MoveSkip(uuid, MoveSkipReason.ASSET_INVISIBLE),
                    )
                elif uuid not in in_src:
                    skipped.append(
                        MoveSkip(uuid, MoveSkipReason.NOT_IN_SOURCE),
                    )
                else:
                    to_move.append(uuid)
                    if uuid in in_dst:
                        already_in_target.append(uuid)

            # ---- 6. src == dst 早退：不写任何行 ----
            if src_id == dst_id:
                return MoveOutcome(
                    moved=(),
                    already_in_target=tuple(already_in_target),
                    skipped=tuple(skipped),
                    noop_reason=MoveSkipReason.SRC_EQUALS_DST,
                )

            if not to_move:
                return MoveOutcome(
                    moved=(),
                    already_in_target=(),
                    skipped=tuple(skipped),
                )

            # ---- 7. 写入：先补目标（INSERT OR IGNORE，条件同 add_to_album），
            #         再按预读的 (album_id, asset_id) 精确摘掉源侧 ----
            for uuid in to_move:
                conn.execute(
                    _ADD_MEMBER_SQL, (str(uuid), dst),
                )
            remove_ids = [by_uuid[uuid][0] for uuid in to_move]
            for chunk in chunked(remove_ids):
                conn.execute(
                    f"""
                    DELETE FROM album_assets
                     WHERE album_id = ?
                       AND asset_id IN ({placeholders(len(chunk))})
                    """,
                    (src_id, *chunk),
                )

        return MoveOutcome(
            moved=tuple(to_move),
            already_in_target=tuple(already_in_target),
            skipped=tuple(skipped),
        )

    @staticmethod
    def _skip_all_move(
            ordered: Sequence[UUID],
            reason: str,
    ) -> MoveOutcome:
        """相册级不可用时，把整批输入统一标成该原因（内部小工具）。"""
        return MoveOutcome(
            skipped=tuple(MoveSkip(uuid, reason) for uuid in ordered),
        )

    # ==================================================================
    # 标签成员
    # ==================================================================

    def add_tag_to_asset(
            self, tag_uuid: _UUIDLike, asset_uuid: _UUIDLike
    ) -> int:
        """给资产打标签。

        - 标签或资产不可见（is_deleted = 1）时不会写入；
        - 已存在的绑定通过 INSERT OR IGNORE 跳过（天然幂等）；
        - ``tag_uuid`` 必须指向 ``tags.uuid``（规范标签），**不得写别名**。
        """
        cursor = self._execute(
            f"""
            INSERT OR IGNORE INTO asset_tags (asset_id, tag_uuid)
            SELECT a.id, t.uuid
              FROM assets AS a
              JOIN tags   AS t ON t.uuid = ?
             WHERE a.uuid = ?
               AND {_ACTIVE_ASSET_WHERE_A}
               AND t.is_deleted = 0
            """,
            (as_uuid_str(tag_uuid), as_uuid_str(asset_uuid)),
        )
        return cursor.rowcount

    def remove_from_tag(
            self, tag_uuid: _UUIDLike, asset_uuid: _UUIDLike
    ) -> int:
        """把资产从指定标签移除（标签不可见时不做任何事）。"""
        cursor = self._execute(
            """
            DELETE FROM asset_tags
             WHERE asset_id = (
                       SELECT id FROM assets WHERE uuid = ?
                   )
               AND tag_uuid = (
                       SELECT uuid FROM tags
                        WHERE uuid = ?
                          AND is_deleted = 0
                   )
            """,
            (as_uuid_str(asset_uuid), as_uuid_str(tag_uuid)),
        )
        return cursor.rowcount

    # ==================================================================
    # 标签成员（批量）
    # ==================================================================

    def add_tags_to_asset(
            self, asset_uuid: _UUIDLike, tag_uuids: Sequence[_UUIDLike]
    ) -> int:
        """批量给资产打标签。

        - 资产或标签不可见（``is_deleted = 1``）时不会写入对应绑定；
        - 已存在的绑定通过 ``INSERT OR IGNORE`` 跳过（天然幂等）；
        - 入参先按出现顺序去重，避免同批重复 uuid 造成无谓扫描；
        - 单事务提交，返回**实际新增**的绑定行数。
        """
        uuids = _dedupe_uuids(tag_uuids)
        if not uuids:
            return 0

        asset_value = as_uuid_str(asset_uuid)
        affected = 0
        with self._transaction() as conn:
            for chunk in chunked(uuids):
                cursor = conn.execute(
                    f"""
                    INSERT OR IGNORE INTO asset_tags (asset_id, tag_uuid)
                    SELECT a.id, t.uuid
                      FROM assets AS a
                      JOIN tags   AS t
                        ON t.uuid IN ({placeholders(len(chunk))})
                       AND t.is_deleted = 0
                     WHERE a.uuid = ?
                       AND {_ACTIVE_ASSET_WHERE_A}
                    """,
                    (*chunk, asset_value),
                )
                affected += cursor.rowcount
        return affected

    def remove_tags_from_asset(
            self, asset_uuid: _UUIDLike, tag_uuids: Sequence[_UUIDLike]
    ) -> int:
        """批量移除资产上的标签绑定。

        - 与 ``remove_from_tag`` 保持一致：仅移除 ``tags.is_deleted = 0``
          的标签绑定；软删标签的绑定保留，标签恢复后即可重现；
        - 资产不存在时自然 0 行；
        - 入参去重后单事务提交，返回**实际删除**的绑定行数。
        """
        uuids = _dedupe_uuids(tag_uuids)
        if not uuids:
            return 0

        asset_value = as_uuid_str(asset_uuid)
        affected = 0
        with self._transaction() as conn:
            for chunk in chunked(uuids):
                cursor = conn.execute(
                    f"""
                    DELETE FROM asset_tags
                     WHERE asset_id = (
                               SELECT id FROM assets WHERE uuid = ?
                           )
                       AND tag_uuid IN (
                               SELECT uuid FROM tags
                                WHERE uuid IN ({placeholders(len(chunk))})
                                  AND is_deleted = 0
                           )
                    """,
                    (asset_value, *chunk),
                )
                affected += cursor.rowcount
        return affected

    # ==================================================================
    # 内部工具
    # ==================================================================

    def _update_many(
            self,
            sql: str,
            uuids: Sequence[str],
            *,
            prefix: Sequence = (),
    ) -> int:
        """按 ``IN (...)`` 分批执行同一条语句，单事务提交。

        ``sql`` 中使用 ``{ph}`` 表示当前批次的占位符列表；用
        ``str.replace`` 而不是 ``str.format`` 展开，避免 SQL 里未来出现
        ``{`` / ``}``（例如 JSON 字面量）时被误当成格式占位。

        ``prefix`` 是每批固定绑定的前置参数（如 ``deleted_at``），
        ``uuids`` 已由调用方去重并转成 ``str``。
        """
        if not uuids:
            return 0
        affected = 0
        with self._transaction() as conn:
            for chunk in chunked(uuids):
                statement = sql.replace("{ph}", placeholders(len(chunk)))
                cursor = conn.execute(statement, (*prefix, *chunk))
                affected += cursor.rowcount
        return affected
