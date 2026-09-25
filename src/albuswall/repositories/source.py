#
""""""

import json
from typing import List, Optional, Final, Callable, Any

from albuswall.dto.source import (
    IngestSourceSyncCandidate, IngestSourceCreate,
    IngestSourceUpdate, MANUAL_SOURCE_ID, IngestSourceViewDTO
)
from albuswall.dto.trigger import TriggerConfig

from .base import BaseRepository
from ..dto import UNSET
from ..utils.time import now_iso


class IngestSourceRepository(BaseRepository):
    """Ingest source repository."""

    _CANDIDATE_COLUMNS = """
        id, source_path, target_path, mount_point,
        auto_mount, file_type_check, file_types,
        subfolder_recursion, subfolder_recursion_depth,
        trigger_config
    """
    _VIEW_COLUMNS = """
        id, title, description, source_path, target_path, mount_point,
        auto_mount, file_type_check, file_types, tags,
        subfolder_recursion, subfolder_recursion_depth,
        trigger_config, created_at, modified_at
    """
    _SYNC_CANDIDATE_COLUMNS = """
        id, source_path, target_path, mount_point,
        auto_mount, file_type_check, file_types,
        subfolder_recursion, subfolder_recursion_depth,
        trigger_config
    """
    _UPDATE_SERIALIZERS: Final[dict[str, Callable[[Any], Any]]] = {
        "title": lambda v: v,
        "description": lambda v: v,
        "source_path": lambda v: v,
        "target_path": lambda v: v,
        "mount_point": lambda v: v,
        "auto_mount": int,
        "file_type_check": lambda m: m.value,
        "file_types": lambda v: json.dumps(v, ensure_ascii=False),
        "tags": lambda v: json.dumps(v, ensure_ascii=False),
        "subfolder_recursion": int,
        "subfolder_recursion_depth": lambda v: v,
        "trigger_config": lambda v: json.dumps(v, ensure_ascii=False),
    }

    def get_source_candidates(self) -> List[IngestSourceSyncCandidate]:
        """
        获取所有需要自动同步的导入源候选。
        """
        query = f"""
                SELECT {self._SYNC_CANDIDATE_COLUMNS}
                FROM ingest_source
                WHERE id != 0
            """
        rows = self._fetchall(query)
        if not rows:
            return []
        return [IngestSourceSyncCandidate.from_row(row) for row in rows]

    def get_all_trigger_configs(self) -> List[TriggerConfig]:
        """
        获取全部导入源的 trigger_config，解析为 TriggerConfig 对象列表。

        解析成功后，会将所属导入源的主键 id 赋值给 TriggerConfig.id 字段，
        确保每个配置对象都能直接关联到具体的导入源。
        忽略 trigger_config 为 NULL、空字符串或解析失败的记录。
        """
        query = "SELECT id, trigger_config FROM ingest_source"
        rows = self._fetchall(query)
        if not rows:
            return []

        configs: List[TriggerConfig] = []
        for row in rows:
            source_id = row["id"]
            raw_config = row["trigger_config"]
            if not raw_config:
                continue
            try:
                config = TriggerConfig.from_json(raw_config)
                if config is not None:
                    # 将外部导入源 ID 注入到配置对象中，
                    # 覆盖可能从 JSON 内部解析出的 id（通常 JSON 中不包含此字段）
                    config.id = source_id
                    configs.append(config)
            except (ValueError, json.JSONDecodeError) as e:
                self.logger.warning(
                    "解析 trigger_config 失败 (source_id=%s): %s",
                    source_id, e
                )
        return configs

    def get_id_list(self) -> List[int]:
        rows = self._fetchall("SELECT id FROM ingest_source")
        if not rows:
            return []
        return [row["id"] for row in rows]

    def create(self, source: IngestSourceCreate) -> int:
        """创建新的导入源，返回新记录的 ID。"""
        query = """
            INSERT INTO ingest_source (
                title, description, source_path, target_path, mount_point,
                auto_mount, file_type_check, file_types, tags,
                subfolder_recursion, subfolder_recursion_depth, trigger_config
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        cursor = self._execute(query, source.to_row_params())
        return cursor.lastrowid  # type: ignore

    def update(self, source_id: int, source: IngestSourceUpdate) -> bool:
        set_parts: list[str] = []
        params: list[Any] = []

        for name, serialize in self._UPDATE_SERIALIZERS.items():
            value = getattr(source, name)
            if value is UNSET:  # PATCH 语义：UNSET 表示不更新
                continue
            set_parts.append(f"{name} = ?")
            params.append(None if value is None else serialize(value))

        if not set_parts:
            return False

        # 始终更新 modified_at
        set_parts.append("modified_at = ?")
        params.append(now_iso())
        params.append(source_id)

        query = f"UPDATE ingest_source SET {', '.join(set_parts)} WHERE id = ?"
        cursor = self._execute(query, params)
        return cursor.rowcount > 0

    # ==================================================================
    # 查询：视图 DTO
    # ==================================================================

    def list_view_dtos(self) -> List[IngestSourceViewDTO]:
        """列出所有导入源（含 id=0 的虚拟根 / 手动导入源），按 id 升序。"""
        query = f"""
            SELECT {self._VIEW_COLUMNS}
            FROM ingest_source
            ORDER BY id ASC
        """
        rows = self._fetchall(query)
        if not rows:
            return []
        return [IngestSourceViewDTO.from_row(row) for row in rows]

    def get_view_dto(self, source_id: int) -> Optional[IngestSourceViewDTO]:
        """
        单条查询导入源.
        :return : 不存在返回 None
        """
        query = f"""
            SELECT {self._VIEW_COLUMNS}
            FROM ingest_source
            WHERE id = ?
        """
        rows = self._fetchall(query, (source_id,))
        if not rows:
            return None
        return IngestSourceViewDTO.from_row(rows[0])

    # ==================================================================
    # 查询：同步候选（单源）
    # ==================================================================

    def get_sync_candidate(
            self, source_id: int
    ) -> Optional[IngestSourceSyncCandidate]:
        if source_id == MANUAL_SOURCE_ID:
            return None
        query = f"""
            SELECT {self._SYNC_CANDIDATE_COLUMNS}
            FROM ingest_source
            WHERE id = ?
        """
        rows = self._fetchall(query, (source_id,))
        if not rows:
            return None
        return IngestSourceSyncCandidate.from_row(rows[0])

    # ==================================================================
    # 查询：存在性 / 引用计数
    # ==================================================================

    def exists(self, source_id: int) -> bool:
        """判断指定 id 的导入源是否存在。"""
        rows = self._fetchall(
            "SELECT 1 FROM ingest_source WHERE id = ? LIMIT 1",
            (source_id,),
        )
        return bool(rows)

    # alias：语义更贴近调用方
    def has_source(self, source_id: int) -> bool:
        return self.exists(source_id)

    def count_assets(self, source_id: int) -> int:
        """统计该源关联的 assets 数量（删除前置检查用）。

        依赖 ``assets.source_id`` 上的外键约束（RESTRICT）：若计数 > 0，
        数据库层会拒绝 ``DELETE FROM ingest_source``。
        """
        # TODO TOCTOU 改 FK RESTRICT

        rows = self._fetchall(
            "SELECT COUNT(*) AS cnt FROM assets WHERE source_id = ?",
            (source_id,),
        )
        # if not rows:
        #     return 0
        # row = rows[0]
        # try:
        #     return int(row["cnt"])
        # except (KeyError, TypeError, IndexError):
        #     # 兼容 tuple-like row
        #     return int(row[0]) if row else 0
        return int(rows[0]["cnt"]) if rows else 0

    # ==================================================================
    # 写入：删除
    # ==================================================================

    def delete(self, source_id: int) -> bool:
        """硬删除指定导入源。

        - 拒绝删除虚拟根 / 手动导入源（``MANUAL_SOURCE_ID``），抛 ``ValueError``；
        - 若 ``assets.source_id`` 存在外键 RESTRICT 引用，由 DB 抛错；
        - 返回是否实际删除（``rowcount > 0``）。
        """
        # TODO 软删除功能以后加
        if source_id == MANUAL_SOURCE_ID:
            raise ValueError(
                f"Cannot delete manual/virtual source "
                f"(id={MANUAL_SOURCE_ID})"
            )
        cursor = self._execute(
            "DELETE FROM ingest_source WHERE id = ?",
            (source_id,),
        )
        return cursor.rowcount > 0
