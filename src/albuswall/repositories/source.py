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
    """Ingest source repository.

    依赖：

    * ``assets.source_id`` 上的外键约束 **必须为 RESTRICT**，且每个连接
      都执行过 ``PRAGMA foreign_keys = ON``（SQLite 默认关闭）。
      本仓库的 ``delete`` 会先做一次显式 ``count_assets`` 预检查以获得
      友好错误信息，但真正的并发兜底仍由 FK 完成（TOCTOU）。
    """

    # 同步候选列集合。
    # 注意：DB 列名是 `target_path`，DTO 字段是 `target`，二者映射
    # 由 `IngestSourceSyncCandidate.from_row` 负责，此处不得改名。
    _SYNC_CANDIDATE_COLUMNS = """
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

    # ------------------------------------------------------------------
    # 触发配置解析策略（统一在此声明，避免各处语义漂移）
    #
    #  * 批量 / 单源读取：解析失败 → WARNING 日志 + 跳过该条
    #    （等价于"该源没有触发配置"），**不向上抛异常**；
    #    目的是单条脏数据不能拖垮整个调度器。
    #  * 调用方若需要严格语义（比如"必须拿到配置否则拒绝操作"），
    #    请使用 `get_trigger_config` 并显式判断 None，
    #    此时 None 既表示"无配置"也表示"解析失败"，
    #    需结合日志侧判断。
    # ------------------------------------------------------------------
    _TRIGGER_PARSE_POLICY = "warn-and-skip"

    # ==================================================================
    # 查询：同步候选（批量 / 单源）
    # ==================================================================

    def get_source_candidates(self) -> List[IngestSourceSyncCandidate]:
        """获取所有需要自动同步的导入源候选（排除 id=0 虚拟根）。"""
        query = f"""
            SELECT {self._SYNC_CANDIDATE_COLUMNS}
            FROM ingest_source
            WHERE id != 0
            ORDER BY id ASC
        """
        rows = self._fetchall(query)
        if not rows:
            return []
        return [IngestSourceSyncCandidate.from_row(row) for row in rows]

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
        row = self._fetchone(query, (source_id,))
        if row is None:
            return None
        return IngestSourceSyncCandidate.from_row(row)

    # ==================================================================
    # 查询：触发配置
    # ==================================================================

    def get_all_trigger_configs(self) -> List[TriggerConfig]:
        """获取全部**存在** trigger_config 的导入源的强类型配置列表。

        与老实现的区别：

        * SQL 层直接过滤掉 ``NULL`` / 空串，避免把"无触发配置"的源也
          带进内存（调度侧再做一次全量过滤是浪费）；
        * 单条解析失败 → WARNING 并跳过（见 ``_TRIGGER_PARSE_POLICY``）；
        * 解析成功后自动把外部导入源 id 注入 ``config.id``。
        """
        query = """
            SELECT id, trigger_config
            FROM ingest_source
            WHERE trigger_config IS NOT NULL
              AND TRIM(trigger_config) != ''
            ORDER BY id ASC
        """
        rows = self._fetchall(query)
        if not rows:
            return []

        configs: List[TriggerConfig] = []
        for row in rows:
            source_id = row["id"]
            raw_config = row["trigger_config"]
            try:
                config = TriggerConfig.from_json(raw_config)
            except (ValueError, json.JSONDecodeError) as e:
                self.logger.warning(
                    "解析 trigger_config 失败 (source_id=%s): %s",
                    source_id, e,
                )
                continue
            if config is None:
                continue
            # 覆盖 JSON 内部可能出现的 id，保证与 DB 主键一致
            config.id = source_id
            configs.append(config)
        return configs

    def get_trigger_config(self, source_id: int) -> Optional[TriggerConfig]:
        """单源 trigger_config 强类型读取。

        返回 ``None`` 覆盖三种语义（调用方需自行结合日志判断）：
        - 源不存在；
        - ``trigger_config`` 为 NULL / 空串；
        - 解析失败（此时会打 WARNING，遵循 ``_TRIGGER_PARSE_POLICY``）。
        """
        query = "SELECT id, trigger_config FROM ingest_source WHERE id = ?"
        row = self._fetchone(query, (source_id,))
        if row is None:
            return None

        raw_config = row["trigger_config"]
        if not raw_config:
            return None

        try:
            config = TriggerConfig.from_json(raw_config)
        except (ValueError, json.JSONDecodeError) as e:
            self.logger.warning(
                "解析 trigger_config 失败 (source_id=%s): %s",
                source_id, e,
            )
            return None
        if config is None:
            return None
        config.id = row["id"]
        return config

    def get_id_list(self) -> List[int]:
        rows = self._fetchall("SELECT id FROM ingest_source ORDER BY id ASC")
        if not rows:
            return []
        return [row["id"] for row in rows]

    # ==================================================================
    # 写入：创建 / 更新
    # ==================================================================

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
        """单条查询导入源；不存在返回 None。"""
        query = f"""
            SELECT {self._VIEW_COLUMNS}
            FROM ingest_source
            WHERE id = ?
        """
        row = self._fetchone(query, (source_id,))
        if row is None:
            return None
        return IngestSourceViewDTO.from_row(row)

    # ==================================================================
    # 查询：存在性 / 引用计数
    # ==================================================================

    def exists(self, source_id: int) -> bool:
        """判断指定 id 的导入源是否存在。"""
        row = self._fetchone(
            "SELECT 1 FROM ingest_source WHERE id = ? LIMIT 1",
            (source_id,),
        )
        return row is not None

    # alias：语义更贴近调用方
    def has_source(self, source_id: int) -> bool:
        return self.exists(source_id)

    def count_assets(self, source_id: int) -> int:
        """统计该源关联的 assets 数量（删除前置检查用）。"""
        row = self._fetchone(
            "SELECT COUNT(*) AS cnt FROM assets WHERE source_id = ?",
            (source_id,),
        )
        if row is None:
            return 0
        try:
            return int(row["cnt"])
        except (KeyError, TypeError):
            return 0

    # ==================================================================
    # 写入：删除
    # ==================================================================

    def delete(self, source_id: int) -> bool:
        """硬删除指定导入源。

        行为：

        * ``MANUAL_SOURCE_ID`` → ``ValueError``；
        * 若仍有 assets 引用 → ``ValueError``（显式预检查，给出可读信息）；
          并发窗口内由 ``assets.source_id`` 的 FK RESTRICT 兜底；
        * 返回是否实际删除（``rowcount > 0``）。

        注意：显式预检查存在 TOCTOU，因此 **DB 侧 FK RESTRICT 与
        PRAGMA foreign_keys=ON 是必需的**，二者缺一不可。
        """
        if source_id == MANUAL_SOURCE_ID:
            raise ValueError(
                f"Cannot delete manual/virtual source "
                f"(id={MANUAL_SOURCE_ID})"
            )

        # 显式预检查：给出可读错误，避免只依赖底层 FK 抛错
        asset_count = self.count_assets(source_id)
        if asset_count > 0:
            raise ValueError(
                f"Cannot delete ingest source {source_id}: "
                f"{asset_count} asset(s) still reference it"
            )

        cursor = self._execute(
            "DELETE FROM ingest_source WHERE id = ?",
            (source_id,),
        )
        return cursor.rowcount > 0
