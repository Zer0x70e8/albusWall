#
"""缩略图路径的读写仓储。

数据模型
--------
assets 表里与缩略图相关的列：
    thumb_path        —— 缩略图主目录（相对路径），所有 spec 文件的父目录
    thumb_small_path  —— small  缩略图相对 thumb_path 的相对路径
    thumb_medium_path —— medium 缩略图相对 thumb_path 的相对路径
    thumb_large_path  —— large  缩略图相对 thumb_path 的相对路径

完整路径 = os.path.join(thumb_path, thumb_<spec>_path)
"""

import logging
from typing import Iterable, Mapping, Optional, Sequence

from albuswall.log import TRACE, Logger
from albuswall.dto.thumbnail import (
    MissingThumbnailRow,
    ThumbnailHashRow,
    ThumbnailPaths,
    ThumbnailStats,
    ThumbnailTaskInput,
)

from .base import BaseRepository

logger: Logger = logging.getLogger(__name__)  # albuswall.database.thumbnail  # noqa
logger.trace = lambda msg, *args: logger.log(TRACE, msg, *args)

# spec 名 → assets 列名
SPEC_TO_COLUMN: dict[str, str] = {
    "small": "thumb_small_path",
    "medium": "thumb_medium_path",
    "large": "thumb_large_path",
}

COLUMN_TO_SPEC: dict[str, str] = {v: k for k, v in SPEC_TO_COLUMN.items()}

ALL_SPECS: tuple[str, ...] = ("small", "medium", "large")

# 缩略图主目录列，独立于 spec
BASE_COLUMN: str = "thumb_path"

# 哨兵：区分 “不更新 base” 与 “把 base 置 NULL”
_UNSET: object = object()


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
    """

    # ------------------------------------------------------------------ #
    # 写入
    # ------------------------------------------------------------------ #
    def update_paths(
            self,
            asset_id: int,
            paths: Mapping[str, Optional[str]],
            *,
            base_dir: object = _UNSET,
    ) -> int:
        """更新单个资产的缩略图路径。

        Args:
            asset_id: assets.id
            paths: {spec_name: relative_path_or_None}
                   仅更新传入的 spec；值为 None 表示清空该列。
            base_dir: 可选。传入时同步更新 thumb_path 主目录。
                      传 None 表示清空；不传则保持不变。

        Returns:
            受影响行数（0 表示 asset_id 不存在或无字段可更新）。
        """
        sets, params = self._build_update_sets(
            paths, base_dir=base_dir, context=f"asset_id={asset_id}"
        )
        if not sets:
            return 0
        params.append(asset_id)
        sql = f"UPDATE assets SET {', '.join(sets)} WHERE id = ?"
        return self._execute(sql, params).rowcount

    def update_paths_by_uuid(
            self,
            uuid: str,
            paths: Mapping[str, Optional[str]],
            *,
            base_dir: object = _UNSET,
    ) -> int:
        """同 update_paths，但用 uuid 定位。"""
        sets, params = self._build_update_sets(
            paths, base_dir=base_dir, context=f"uuid={uuid!r}"
        )
        if not sets:
            return 0
        params.append(uuid)
        sql = f"UPDATE assets SET {', '.join(sets)} WHERE uuid = ?"
        return self._execute(sql, params).rowcount

    def bulk_update_paths(
            self,
            rows: Iterable[tuple[int, Mapping[str, Optional[str]]]],
    ) -> int:
        """批量更新 spec 相对路径，单事务提交。

        Args:
            rows: 可迭代的 (asset_id, {spec: rel_path})。
                  如果同时要更新 base_dir，请改用 update_paths 逐条调用。

        Returns:
            成功更新的资产数。
        """
        updated = 0
        with self._transaction() as conn:
            for asset_id, paths in rows:
                sets, params = self._build_update_sets(
                    paths, context=f"asset_id={asset_id}"
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
            specs: Sequence[str] = ALL_SPECS,
            *,
            clear_base: bool = False,
    ) -> int:
        """清空指定 spec 的路径列。

        Args:
            :param asset_id:
            :param specs: 要清空的 spec 名序列。
            :param clear_base: 是否同时清空 thumb_path 主目录。
        """
        paths = {s: None for s in specs}
        base_dir = None if clear_base else _UNSET
        return self.update_paths(asset_id, paths, base_dir=base_dir)

    def clear_all_for_missing_base(self, base_prefix: str) -> int:
        """缓存主目录被删/迁移后调用：清空所有以 base_prefix 开头的缩略图记录。

        由于 spec 路径现在是相对路径，版本前缀只出现在 thumb_path 上，
        这里只需要按 thumb_path 做前缀匹配即可。

        Args:
            base_prefix: thumb_path 的路径前缀（如 "v1/"）。
                         调用方负责传对（切换 SPEC_VERSION 时传 "v0/"）。

        Raises:
            ValueError: base_prefix 为空。
        """
        if not base_prefix:
            raise ValueError("base_prefix must be a non-empty string")

        sql = r"""
            UPDATE assets
               SET thumb_path        = NULL,
                   thumb_small_path  = NULL,
                   thumb_medium_path = NULL,
                   thumb_large_path  = NULL
             WHERE thumb_path LIKE ? ESCAPE '\'
        """
        like = f"{self._escape_like(base_prefix)}%"
        cursor = self._execute(sql, (like,))
        logger.info(
            "cleared thumbnail paths under %r: %d rows",
            base_prefix, cursor.rowcount,
        )
        return cursor.rowcount

    # ------------------------------------------------------------------ #
    # 读取
    # ------------------------------------------------------------------ #
    def get_paths(self, asset_id: int) -> Optional[ThumbnailPaths]:
        """返回 ThumbnailPaths；资产不存在返回 None。"""
        row = self._fetchone(
            """
            SELECT thumb_path,
                   thumb_small_path, thumb_medium_path, thumb_large_path
              FROM assets
             WHERE id = ?
            """,
            (asset_id,),
        )
        return self._row_to_paths(row)

    def get_paths_by_uuid(self, uuid: str) -> Optional[ThumbnailPaths]:
        """同 get_paths，按 uuid 查。资产不存在返回 None。"""
        row = self._fetchone(
            """
            SELECT thumb_path,
                   thumb_small_path, thumb_medium_path, thumb_large_path
              FROM assets
             WHERE uuid = ?
            """,
            (uuid,),
        )
        return self._row_to_paths(row)

    @staticmethod
    def resolve_path(
            base_dir: Optional[str], spec_path: Optional[str]
    ) -> Optional[str]:
        """把 (base_dir, 相对路径) 拼成完整路径。任一为空则返回 None。

        仓储本身不碰文件系统，这只是给调用方一个统一的拼接口径。
        """
        if not spec_path:
            return None
        if spec_path.startswith("/"):
            return spec_path  # 兜底：已经是绝对路径
        if not base_dir:
            return None
        return f"{base_dir.rstrip('/')}/{spec_path.lstrip('/')}"

    # ------------------------------------------------------------------ #
    # 预热 / 巡检
    # ------------------------------------------------------------------ #
    def list_missing(
            self,
            specs: Sequence[str] = ALL_SPECS,
            limit: int = 500,
            source_id: Optional[int] = None,
    ) -> list[MissingThumbnailRow]:
        """列出缺少任意一个 spec 缩略图的活跃资产（预热 / 补偿用）。"""
        cols = [SPEC_TO_COLUMN[s] for s in specs if s in SPEC_TO_COLUMN]
        if not cols:
            return []

        where_null = " OR ".join(f"{c} IS NULL" for c in cols)

        params: list = []
        source_clause = ""
        if source_id is not None:
            source_clause = "AND source_id = ?"
            params.append(source_id)
        params.append(limit)

        sql = f"""
            SELECT id, uuid, source_id, file_path, file_hash,
                   thumb_path,
                   thumb_small_path, thumb_medium_path, thumb_large_path
              FROM assets
             WHERE is_deleted = 0
               AND (thumb_path IS NULL OR ({where_null}))
               {source_clause}
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
            """
            SELECT id, uuid, file_hash,
                   thumb_path,
                   thumb_small_path, thumb_medium_path, thumb_large_path
              FROM assets
             WHERE is_deleted = 0
             ORDER BY id
             LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ) or []
        return [ThumbnailHashRow.from_row(r) for r in rows]

    def count_by_status(self) -> ThumbnailStats:
        """统计有/无缩略图的数量，用于监控面板。"""
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
    # 任务输入
    # ------------------------------------------------------------------ #
    def get_task_input(self, asset_id: int) -> Optional[ThumbnailTaskInput]:
        """取渲染缩略图所需的上下文（join ingest_source 拿 source_path）。"""
        row = self._fetchone(
            """
            SELECT a.id            AS id,
                   a.uuid          AS uuid,
                   a.source_id     AS source_id,
                   s.source_path   AS source_path,
                   a.file_path     AS file_path
              FROM assets a
              LEFT JOIN ingest_source s ON s.id = a.source_id
             WHERE a.id = ? AND a.is_deleted = 0
            """,
            (asset_id,),
        )
        if row is None:
            return None
        return ThumbnailTaskInput.from_row(row)

    def get_task_input_by_uuid(self, uuid: str) -> Optional[ThumbnailTaskInput]:
        """同 get_task_input，但按 assets.uuid 定位。"""
        row = self._fetchone(
            """
            SELECT a.id            AS id,
                   a.uuid          AS uuid,
                   a.source_id     AS source_id,
                   s.source_path   AS source_path,
                   a.file_path     AS file_path
              FROM assets a
              LEFT JOIN ingest_source s ON s.id = a.source_id
             WHERE a.uuid = ? AND a.is_deleted = 0
            """,
            (uuid,),
        )
        if row is None:
            return None
        return ThumbnailTaskInput.from_row(row)

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    @staticmethod
    def _escape_like(s: str) -> str:
        """转义 SQLite LIKE 的通配符（配合 ESCAPE '\\' 使用）。"""
        return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    @staticmethod
    def _build_update_sets(
            paths: Mapping[str, Optional[str]],
            *,
            base_dir: object = _UNSET,
            context: str = "",
    ) -> tuple[list[str], list]:
        """构造 SET 子句；未知 spec 记录 warning 并跳过。

        base_dir 用 _UNSET 哨兵区分“不更新”与“置 NULL”。
        """
        sets: list[str] = []
        params: list = []

        if base_dir is not _UNSET:
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
