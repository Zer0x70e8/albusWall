#
"""缩略图路径的读写仓储。

数据模型
--------
assets 表里与缩略图相关的列：
    thumb_path        —— 缩略图主目录，**绝对路径**
    thumb_small_path  —— small  缩略图，**相对 thumb_path**
    thumb_medium_path —— medium 缩略图，**相对 thumb_path**
    thumb_large_path  —— large  缩略图，**相对 thumb_path**

完整路径 = thumb_path.rstrip('/') + '/' + thumb_<spec>_path
（拼接统一走 ThumbnailPaths.resolve / utils.path.join_path）

契约要点：
  - base_dir 是绝对路径；spec 路径永远是相对 base 的短路径。
  - 仓储层不做文件系统 IO；clear_and_snapshot() 只清库并返回被清的路径，
    文件删除由调用方负责。
  - get_task_input* 默认带 is_deleted=0，仅供生成侧使用；
    读接口（含回收站场景）请用 get_paths / get_paths_bulk，
    它们不过滤 is_deleted。

常量来源
--------
ALL_SPECS / SPEC_TO_COLUMN / BASE_COLUMN / ALL_THUMB_COLUMNS
均来自 albuswall.dto.thumbnail，本模块不再就地定义，避免多处漂移。
"""

from typing import Any, Iterable, Mapping, Optional, Sequence

from albuswall.log import getLogger
from albuswall.dto.sentinel import UnsetType, UNSET
from albuswall.dto.thumbnail import (
    ALL_SPECS,
    ALL_THUMB_COLUMNS,
    BASE_COLUMN,
    SPEC_TO_COLUMN,
    MissingThumbnailRow,
    ThumbnailHashRow,
    ThumbnailPaths,
    ThumbnailStats,
    ThumbnailTaskInput,
    ThumbSpec,
)
from albuswall.utils.path import join_path

from .base import BaseRepository

logger = getLogger(__name__)  # albuswall.database.thumbnail

# 接近但小于 SQLite 默认 SQLITE_MAX_VARIABLE_NUMBER，兼顾旧版
_IN_CLAUSE_CHUNK = 900


class ThumbnailRepository(BaseRepository):
    """缩略图路径落库 / 查询。

    - 所有写入均为幂等 UPDATE。
    - spec 键必须在 SPEC_TO_COLUMN 中；未知 spec 记录 warning 并忽略。
    - base_dir 单独维护，不走 spec 映射。

    标识约定：
        - asset_id (INTEGER PRIMARY KEY) 是内部标识，用于 JOIN、排序、批量操作。
          id 反映插入顺序，等价于导入顺序。
        - uuid 是对外标识，用于跨模块/跨设备/持久化任务中的引用。
        业务层有 asset 行时用 id 版本；只有 uuid 时用 uuid 版本。

    命名约定：
        - get_paths*       —— 读取，**不过滤 is_deleted**（回收站场景用）
        - get_task_input*  —— 生成侧上下文，默认过滤 is_deleted
        - clear_paths*     —— 清空列（可只清部分 spec）
        - clear_and_snapshot* —— 清空全部缩略图列并返回清空前的快照（永删用）
    """

    # ------------------------------------------------------------------ #
    # 写入
    # ------------------------------------------------------------------ #
    def update_paths(
            self,
            asset_id: int,
            paths: Mapping[ThumbSpec, Optional[str]],
            *,
            base_dir: str | None | UnsetType = UNSET,
    ) -> int:
        """更新单个资产的缩略图路径。

        Args:
            asset_id: assets.id
            paths: {spec: relative_path_or_None}
                   仅更新传入的 spec；值为 None 表示清空该列。
            base_dir: 可选。传入时同步更新 thumb_path 主目录。
                      传 None 表示清空；不传则保持不变。

        Returns:
            受影响行数（0 表示 asset_id 不存在或无字段可更新）。
        """
        return self._update_where("id", asset_id, paths, base_dir=base_dir)

    def update_paths_by_uuid(
            self,
            uuid: str,
            paths: Mapping[ThumbSpec, Optional[str]],
            *,
            base_dir: str | None | UnsetType = UNSET,
    ) -> int:
        """同 update_paths，但用 uuid 定位。"""
        return self._update_where("uuid", uuid, paths, base_dir=base_dir)

    def bulk_update_paths(
            self,
            rows: Iterable[
                tuple[int, Mapping[ThumbSpec, Optional[str]]] |
                tuple[int, Mapping[ThumbSpec, Optional[str]], object]
                ],
    ) -> int:
        """批量更新缩略图路径，单事务提交。

        Args:
            rows: 可迭代的
                  · (asset_id, {spec: rel_path})          —— 只更 spec
                  · (asset_id, {spec: rel_path}, base_dir) —— 同时更 base
                  base_dir 传 None 表示清空；不传则保持原值。

        Returns:
            成功更新的资产数（累加 rowcount）。
        """
        updated = 0
        with self._transaction() as conn:
            for item in rows:
                if len(item) == 3:
                    asset_id, paths, base_dir = item
                elif len(item) == 2:
                    asset_id, paths = item
                    base_dir = UNSET
                else:
                    raise ValueError(
                        f"bulk_update_paths row must be 2- or 3-tuple, "
                        f"got {len(item)}"
                    )

                sets, params = self._build_update_sets(
                    paths, base_dir=base_dir, context=f"asset_id={asset_id}"
                )
                if not sets:
                    continue
                params.append(asset_id)
                sql = f"UPDATE assets SET {', '.join(sets)} WHERE id = ?"
                cursor = conn.execute(sql, params)
                updated += cursor.rowcount
        return updated

    def clear_paths(
            self,
            asset_id: int,
            specs: Sequence[ThumbSpec] = ALL_SPECS,
            *,
            clear_base: bool = False,
    ) -> int:
        """清空指定 spec 的路径列。

        Args:
            asset_id: assets.id
            specs: 要清空的 spec 序列。
            clear_base: 是否同时清空 thumb_path 主目录。

        Returns:
            受影响行数。
        """
        paths = {s: None for s in specs}
        base_dir: str | None | UnsetType = None if clear_base else UNSET
        return self.update_paths(asset_id, paths, base_dir=base_dir)

    def clear_paths_under_base_prefix(self, base_prefix: str) -> int:
        """缓存主目录被删/迁移后调用：清空所有以 base_prefix 开头的缩略图记录。

        契约：
          - base_prefix 是 **assets.thumb_path 的绝对路径前缀**
            （如 "/home/skyline/.cache/albuswall/thumbs/v1/"），
            不是相对前缀 "v1/"。因 thumb_path 存的是绝对路径，
            LIKE 匹配必须基于绝对路径前缀。
          - 尾部是否带 "/" 由调用方决定：
            · "…/v1/"   → 只命中该版本目录下的资产（推荐）
            · "…/v1"    → 会命中 "…/v10/"、"…/v1x/" 等
          - 本方法只清库；磁盘文件删除由调用方按 base_prefix 自行 rmtree。
          - **不过滤 is_deleted**：整个目录都废了，回收站资产记录也一起清。

        Args:
            base_prefix: thumb_path 的绝对路径前缀（非空）。

        Raises:
            ValueError: base_prefix 为空。
        """
        if not base_prefix:
            raise ValueError("base_prefix must be a non-empty string")

        set_clause = ", ".join(f"{c} = NULL" for c in ALL_THUMB_COLUMNS)
        sql = (
            f"UPDATE assets SET {set_clause} "
            r"WHERE thumb_path LIKE ? ESCAPE '\'"
        )
        like = f"{self._escape_like(base_prefix)}%"
        cursor = self._execute(sql, (like,))
        logger.info(
            "cleared thumbnail paths under %r: %d rows",
            base_prefix, cursor.rowcount,
        )
        return cursor.rowcount

    def clear_and_snapshot(self, asset_id: int) -> Optional[ThumbnailPaths]:
        """永久删除时调用：清空该资产所有缩略图列，返回清空前的路径快照。

        仓储不触碰文件系统；调用方拿到返回值后按 paths 里的
        base + spec 组合自行 unlink 文件。返回 None 表示 asset_id 不存在。

        与 clear_paths 的区别：
          - clear_paths 是「重建前置空」，粒度可只挑若干 spec；
          - clear_and_snapshot 是「资产销毁」，四个列一起清，
            返回快照供删文件。

        **调用时机**：仅限永久删除（清空回收站）。软删（is_deleted=1）
        不要调这里——软删必须保留缩略图供 Trash 预览，否则回收站里
        看不到图。
        """
        snapshot = self.get_paths(asset_id)
        if snapshot is None:
            return None

        set_clause = ", ".join(f"{c} = NULL" for c in ALL_THUMB_COLUMNS)
        self._execute(
            f"UPDATE assets SET {set_clause} WHERE id = ?", (asset_id,)
        )
        logger.info(
            "cleared thumbnail paths asset_id=%d base=%r",
            asset_id, snapshot.base,
        )
        return snapshot

    def clear_and_snapshot_bulk(
            self, asset_ids: Sequence[int]
    ) -> dict[int, ThumbnailPaths]:
        """批量清空并返回快照；返回 {asset_id: 清空前的路径快照}。

        用于 Trash 批量清空。仓储只清库；文件删除由调用方遍历返回值执行。
        不存在的 id 不出现在结果里。
        """
        snapshots = self.get_paths_bulk(asset_ids)
        if not snapshots:
            return {}

        set_clause = ", ".join(f"{c} = NULL" for c in ALL_THUMB_COLUMNS)
        sql = f"UPDATE assets SET {set_clause} WHERE id = ?"
        with self._transaction() as conn:
            for aid in snapshots:
                conn.execute(sql, (aid,))
        logger.info("cleared thumbnail paths: %d assets", len(snapshots))
        return snapshots

    # ------------------------------------------------------------------ #
    # 读取（不过滤 is_deleted，含回收站场景）
    # ------------------------------------------------------------------ #
    def get_paths(self, asset_id: int) -> Optional[ThumbnailPaths]:
        """返回 ThumbnailPaths；资产不存在返回 None。不过滤 is_deleted。"""
        row = self._fetchone(
            f"""
            SELECT id, {self._path_columns()}
              FROM assets
             WHERE id = ?
            """,
            (asset_id,),
        )
        return self._row_to_paths(row)

    def get_paths_by_uuid(self, uuid: str) -> Optional[ThumbnailPaths]:
        """同 get_paths，按 uuid 查。不过滤 is_deleted。"""
        row = self._fetchone(
            f"""
            SELECT id, {self._path_columns()}
              FROM assets
             WHERE uuid = ?
            """,
            (uuid,),
        )
        return self._row_to_paths(row)

    def get_paths_bulk(
            self, asset_ids: Sequence[int]
    ) -> dict[int, ThumbnailPaths]:
        """批量读缩略图路径。

        不区分 is_deleted：读取侧（含回收站）都走这里。
        返回 {asset_id: ThumbnailPaths}，缺失的 id 不在结果里。
        """
        if not asset_ids:
            return {}

        result: dict[int, ThumbnailPaths] = {}
        ids = list(asset_ids)
        cols = self._path_columns()
        for start in range(0, len(ids), _IN_CLAUSE_CHUNK):
            chunk = ids[start:start + _IN_CLAUSE_CHUNK]
            placeholders = ",".join("?" * len(chunk))
            rows = self._fetchall(
                f"""
                SELECT id, {cols}
                  FROM assets
                 WHERE id IN ({placeholders})
                """,
                chunk,
            ) or []
            for r in rows:
                result[r["id"]] = ThumbnailPaths.from_row(r)
        return result

    # ------------------------------------------------------------------ #
    # 预热 / 巡检
    # ------------------------------------------------------------------ #
    def list_missing(
            self,
            specs: Sequence[ThumbSpec] = ALL_SPECS,
            limit: int = 500,
            source_id: Optional[int] = None,
            *,
            include_deleted: bool = False,
    ) -> list[MissingThumbnailRow]:
        """列出缺少任意一个 spec 缩略图的资产（预热 / 补偿用）。

        Args:
            specs: 判定「缺失」的 spec 集合。
            limit: 最多返回行数。
            source_id: 只统计该 source 的资产；None 表示不限。
            include_deleted: 默认 False，只统计活跃资产。
                             传 True 时连软删资产也纳入（用于回收站重建场景）。
        """
        cols = [SPEC_TO_COLUMN[s] for s in specs if s in SPEC_TO_COLUMN]
        if not cols:
            return []

        where_null = " OR ".join(f"{c} IS NULL" for c in cols)

        clauses: list[str] = []
        params: list = []

        if not include_deleted:
            clauses.append("is_deleted = 0")

        clauses.append(f"(thumb_path IS NULL OR ({where_null}))")

        if source_id is not None:
            clauses.append("source_id = ?")
            params.append(source_id)

        params.append(limit)
        where_sql = " AND ".join(clauses)

        sql = f"""
            SELECT id, uuid, source_id, file_path, file_hash,
                   {self._path_columns()}
              FROM assets
             WHERE {where_sql}
             ORDER BY modified_at ASC
             LIMIT ?
        """
        rows = self._fetchall(sql, params) or []
        return [MissingThumbnailRow.from_row(r) for r in rows]

    def list_all_with_hashes(
            self,
            limit: int = 1000,
            offset: int = 0,
    ) -> list[ThumbnailHashRow]:
        """列出活跃资产的 hash + 缩略图路径，用于与磁盘反向比对。"""
        rows = self._fetchall(
            f"""
            SELECT id, uuid, file_hash,
                   {self._path_columns()}
              FROM assets
             WHERE is_deleted = 0
             ORDER BY id
             LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ) or []
        return [ThumbnailHashRow.from_row(r) for r in rows]

    def count_by_status(self) -> ThumbnailStats:
        """统计有/无缩略图的数量，用于监控面板。只统计活跃资产。"""
        row = self._fetchone(
            """
            SELECT
              COUNT(*)                                                       AS total,
              SUM(CASE WHEN thumb_path        IS NOT NULL THEN 1 ELSE 0 END) AS has_base,
              SUM(CASE WHEN thumb_large_path  IS NOT NULL THEN 1 ELSE 0 END) AS has_large,
              SUM(CASE WHEN thumb_medium_path IS NOT NULL THEN 1 ELSE 0 END) AS has_medium,
              SUM(CASE WHEN thumb_small_path  IS NOT NULL THEN 1 ELSE 0 END) AS has_small
              FROM assets
             WHERE is_deleted = 0
            """
        )
        if row is None:
            return ThumbnailStats()
        return ThumbnailStats(
            total=row["total"] or 0,
            has_base=row["has_base"] or 0,
            has_large=row["has_large"] or 0,
            has_medium=row["has_medium"] or 0,
            has_small=row["has_small"] or 0,
        )

    # ------------------------------------------------------------------ #
    # 任务输入（默认过滤 is_deleted，仅供生成侧）
    # ------------------------------------------------------------------ #
    def get_task_input(
            self,
            asset_id: int,
            *,
            include_deleted: bool = False,
    ) -> Optional[ThumbnailTaskInput]:
        """取渲染缩略图所需的上下文（join ingest_source 拿 source_path）。

        仅供**生成侧**调用：默认带 is_deleted = 0，已删除资产返回 None。
        读取场景（含 Trash）请改用 get_paths / get_paths_bulk，
        它们不过滤 is_deleted，能正确读到回收站里资产的缩略图。
        """
        return self._task_input_where(
            "a.id", asset_id, include_deleted=include_deleted,
        )

    def get_task_input_by_uuid(
            self,
            uuid: str,
            *,
            include_deleted: bool = False,
    ) -> Optional[ThumbnailTaskInput]:
        """同 get_task_input，但按 assets.uuid 定位。"""
        return self._task_input_where(
            "a.uuid", uuid, include_deleted=include_deleted,
        )

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    @staticmethod
    def _path_columns() -> str:
        """构造 SELECT 里的缩略图列清单（逗号分隔，无前缀）。"""
        return ", ".join(ALL_THUMB_COLUMNS)

    def _update_where(
            self,
            where_col: str,
            where_val: Any,
            paths: Mapping[ThumbSpec, Optional[str]],
            *,
            base_dir: str | None | UnsetType = UNSET,
    ) -> int:
        """`update_paths` / `update_paths_by_uuid` 的公共实现。

        where_col 只接受白名单值（"id" / "uuid"），避免 SQL 注入面扩大。
        """
        if where_col not in ("id", "uuid"):
            raise ValueError(f"unsupported where column: {where_col!r}")

        sets, params = self._build_update_sets(
            paths, base_dir=base_dir, context=f"{where_col}={where_val!r}",
        )
        if not sets:
            return 0
        params.append(where_val)
        sql = f"UPDATE assets SET {', '.join(sets)} WHERE {where_col} = ?"
        return self._execute(sql, params).rowcount

    def _task_input_where(
            self,
            where_col: str,
            where_val: Any,
            *,
            include_deleted: bool,
    ) -> Optional[ThumbnailTaskInput]:
        """`get_task_input` / `get_task_input_by_uuid` 的公共实现。"""
        if where_col not in ("a.id", "a.uuid"):
            raise ValueError(f"unsupported where column: {where_col!r}")

        where = f"{where_col} = ?"
        if not include_deleted:
            where += " AND a.is_deleted = 0"

        row = self._fetchone(
            f"""
                SELECT a.id            AS id,
                       a.uuid          AS uuid,
                       a.source_id     AS source_id,
                       s.source_path   AS source_path,
                       a.file_path     AS file_path
                  FROM assets a
                  LEFT JOIN ingest_source s ON s.id = a.source_id
                 WHERE {where}
                """,
            (where_val,),
        )
        if row is None:
            return None
        return ThumbnailTaskInput.from_row(row)

    @staticmethod
    def _escape_like(s: str) -> str:
        """转义 SQLite LIKE 的通配符（配合 ESCAPE '\\' 使用）。"""
        return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    @staticmethod
    def _build_update_sets(
            paths: Mapping[ThumbSpec, Optional[str]],
            *,
            base_dir: str | None | UnsetType = UNSET,
            context: str = "",
    ) -> tuple[list[str], list]:
        """构造 SET 子句；未知 spec 记录 warning 并跳过。

        base_dir 用 UNSET 哨兵区分「不更新」与「置 NULL」。
        """
        sets: list[str] = []
        params: list = []

        if base_dir is not UNSET:
            sets.append(f"{BASE_COLUMN} = ?")
            params.append(base_dir)

        for spec, rel_path in paths.items():
            col = SPEC_TO_COLUMN.get(spec)
            if col is None:
                logger.warning(
                    "unknown thumbnail spec: %r%s, skipped",
                    spec,
                    f" ({context})" if context else "",
                )
                continue
            sets.append(f"{col} = ?")
            params.append(rel_path)

        return sets, params

    @staticmethod
    def _row_to_paths(row) -> Optional[ThumbnailPaths]:
        if row is None:
            return None
        return ThumbnailPaths.from_row(row)

    # ------------------------------------------------------------------ #
    # 向后兼容别名（一个版本周期后删除）
    # ------------------------------------------------------------------ #
    # 旧名 → 新名
    purge = clear_and_snapshot
    purge_bulk = clear_and_snapshot_bulk
    clear_all_for_missing_base = clear_paths_under_base_prefix

    @staticmethod
    def resolve_path(base_dir: Optional[str], spec_path: Optional[str]) -> Optional[str]:
        """[deprecated] 用 ThumbnailPaths.resolve 或 utils.path.join_path 代替。"""
        return join_path(base_dir, spec_path)
