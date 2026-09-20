#
""""""

import json
from typing import List

from albuswall.dto.source import (
    IngestSourceSyncCandidate, IngestSourceCreate, IngestSourceUpdate)
from albuswall.dto.trigger import TriggerConfig
from albuswall.common.enums import FileTypeCheckMode

from .base import BaseRepository
from ..utils.time import now_iso


class IngestSourceRepository(BaseRepository):
    """Ingest source repository."""

    def get_source_candidates(self) -> List[IngestSourceSyncCandidate]:
        """
        获取所有需要自动同步的导入源候选。
        """
        query = """
            SELECT id, source_path, target_path, mount_point,
                   auto_mount, file_type_check, file_types, 
                   subfolder_recursion, subfolder_recursion_depth,
                   trigger_config
            FROM ingest_source
        """
        rows = self._fetchall(query)
        if not rows:
            return []

        candidates = []
        for row in rows:
            # 解析 JSON 字段，异常时使用安全默认值
            try:
                file_types = json.loads(row["file_types"]) \
                    if row["file_types"] else []
            except (json.JSONDecodeError, TypeError):
                file_types = []
            try:
                trigger_config = json.loads(row["trigger_config"]) \
                    if row["trigger_config"] else None
            except (json.JSONDecodeError, TypeError):
                trigger_config = None

            candidates.append(IngestSourceSyncCandidate(
                id=row["id"],
                source_path=row["source_path"],
                target=row["target_path"],
                mount_point=row["mount_point"],
                auto_mount=bool(row["auto_mount"]),
                file_type_check=FileTypeCheckMode(row["file_type_check"]),
                file_types=file_types,
                subfolder_recursion=bool(row["subfolder_recursion"]),
                subfolder_recursion_depth=row["subfolder_recursion_depth"],
                trigger_config=trigger_config,
            ))
        return candidates

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
        """
        创建新的导入源，返回新记录的 ID。
        """
        # 序列化 JSON 字段
        file_types_json = json.dumps(source.file_types or [])
        tags_json = json.dumps(source.tags or [])
        trigger_config_json = json.dumps(source.trigger_config) if source.trigger_config else None

        query = """
            INSERT INTO ingest_source (
                title, description, source_path, target_path, mount_point,
                auto_mount, file_type_check, file_types, tags,
                subfolder_recursion, subfolder_recursion_depth, trigger_config
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        params = (
            source.title,
            source.description,
            source.source_path,
            source.target_path,
            source.mount_point,
            int(source.auto_mount),  # bool -> 0/1
            source.file_type_check.value,  # 枚举转字符串
            file_types_json,
            tags_json,
            int(source.subfolder_recursion),
            source.subfolder_recursion_depth,
            trigger_config_json,
        )
        cursor = self._execute(query, params)
        return cursor.lastrowid  # type: ignore

    def update(self, source_id: int, source: IngestSourceUpdate) -> bool:
        """
        更新导入源信息，仅更新 DTO 中非 None 的字段。
        返回是否成功（影响行数 > 0）。
        """
        # 构建 SET 子句和参数列表
        set_parts = []
        params = []

        if source.title is not None:
            set_parts.append("title = ?")
            params.append(source.title)
        if source.description is not None:
            set_parts.append("description = ?")
            params.append(source.description)
        if source.source_path is not None:
            set_parts.append("source_path = ?")
            params.append(source.source_path)
        if source.target_path is not None:
            set_parts.append("target_path = ?")
            params.append(source.target_path)
        if source.mount_point is not None:
            set_parts.append("mount_point = ?")
            params.append(source.mount_point)
        if source.auto_mount is not None:
            set_parts.append("auto_mount = ?")
            params.append(int(source.auto_mount))
        if source.file_type_check is not None:
            set_parts.append("file_type_check = ?")
            params.append(source.file_type_check.value)
        if source.file_types is not None:
            set_parts.append("file_types = ?")
            params.append(json.dumps(source.file_types))
        if source.tags is not None:
            set_parts.append("tags = ?")
            params.append(json.dumps(source.tags))
        if source.subfolder_recursion is not None:
            set_parts.append("subfolder_recursion = ?")
            params.append(int(source.subfolder_recursion))
        if source.subfolder_recursion_depth is not None:
            set_parts.append("subfolder_recursion_depth = ?")
            params.append(source.subfolder_recursion_depth)
        if source.trigger_config is not None:
            set_parts.append("trigger_config = ?")
            params.append(json.dumps(source.trigger_config))

        if not set_parts:
            # 没有需要更新的字段
            return False

        # 始终更新 modified_at
        set_parts.append("modified_at = ?")
        params.append(now_iso())  # 可定义辅助函数生成当前时间戳

        query = f"UPDATE ingest_source SET {', '.join(set_parts)} WHERE id = ?"
        params.append(source_id)

        cursor = self._execute(query, params)
        return cursor.rowcount > 0
