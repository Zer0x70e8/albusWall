#
""""""

import json
from typing import Any, Callable, Final, List, Optional

from albuswall.dto.source import (
    IngestSource,
    IngestSourceCreate,
    IngestSourceUpdate,
    SourcePurgeReport,
    MANUAL_SOURCE_ID,
)
from albuswall.dto.trigger import TriggerConfig

from .base import BaseRepository
from ..dto.sentinel import UNSET
from ..utils.time import now_iso


# noinspection SpellCheckingInspection
class IngestSourceRepository(BaseRepository):
    """Ingest source repository.

    依赖
    ----
    * ``assets.source_id`` 上的外键约束 **必须为 RESTRICT**，且每个连接
      都执行过 ``PRAGMA foreign_keys = ON``（SQLite 默认关闭）。
      本仓库的 ``delete`` 会先做一次显式 ``count_assets`` 预检查以获得
      友好错误信息，但真正的并发兜底仍由 FK 完成（TOCTOU）。

    与旧实现的差异
    --------------
    * 单一列集合：原 ``_SYNC_CANDIDATE_COLUMNS`` / ``_VIEW_COLUMNS`` 合并为
      ``_ALL_COLUMNS``。理由见 DTO 层 ``IngestSource`` 的设计取舍。
    * 单一 DTO：所有读方法返回 ``IngestSource``。
    * ``create`` 内联 INSERT 参数拼装（不再依赖
      ``IngestSourceCreate.to_row_params``）。
    * ``update`` 走 ``UNSET`` / ``None`` 二分语义。

    注意：``_ALL_COLUMNS`` 必须与 ``IngestSource.from_row`` 读取的列
    一一对应。任何新列加入 DTO 时，务必同步更新此处——``from_row`` 用
    ``_row_get(row, key, default)`` 兜底，漏列不会报错，但新字段会被
    静默吞成默认值（例如 ``disabled`` 漏掉会让所有源看起来都是启用态）。
    """

    _ALL_COLUMNS: Final[str] = """
        id, title, description, tags, 
        source_path, target_path, mount_point,
        auto_mount, file_type_check, file_types, 
        subfolder_recursion, subfolder_recursion_depth,
        trigger_config, 
        disabled, is_deleted, 
        deleted_at, created_at, modified_at
    """

    # copy exactly
    _EXACTLY = lambda v: v

    _UPDATE_SERIALIZERS: Final[dict[str, Callable[[Any], Any]]] = {
        "title": _EXACTLY,
        "description": _EXACTLY,
        "source_path": _EXACTLY,
        "target_path": _EXACTLY,
        "mount_point": _EXACTLY,
        "auto_mount": int,
        "disabled": int,
        "file_type_check": lambda m: m.value,
        "file_types": lambda v: json.dumps(v, ensure_ascii=False),
        "tags": lambda v: json.dumps(v, ensure_ascii=False),
        "subfolder_recursion": int,
        "subfolder_recursion_depth": _EXACTLY,
        "trigger_config": lambda v: json.dumps(v, ensure_ascii=False),
    }

    # ==================================================================
    # 查询：IngestSource（统一读模型）
    # ==================================================================

    def get_source_candidates(self) -> List[IngestSource]:
        """获取所有需要自动同步的导入源候选（排除 id=0 虚拟根）。

        注意：此处**不**过滤 ``disabled``。禁用与否是语义判断，交给
        ``IngestSource.is_void()`` 在 service 层处理——repo 只负责
        把行读出来，保持"读路径零业务规则"。
        """
        query = f"""
            SELECT {self._ALL_COLUMNS}
            FROM ingest_source
            WHERE id != 0 AND is_deleted = 0
            ORDER BY id ASC
        """
        rows = self._fetchall(query)
        if not rows:
            return []
        return [IngestSource.from_row(row) for row in rows]

    def get_sync_candidate(self, source_id: int) -> Optional[IngestSource]:
        """单源同步候选；``MANUAL_SOURCE_ID`` 与不存在的 id 都返回 ``None``。"""
        if source_id == MANUAL_SOURCE_ID:
            return None
        query = f"""
            SELECT {self._ALL_COLUMNS}
            FROM ingest_source
            WHERE id = ? AND is_deleted = 0
        """
        row = self._fetchone(query, (source_id,))
        if row is None:
            return None
        return IngestSource.from_row(row)

    def list_view_dtos(self) -> List[IngestSource]:
        """列出所有导入源（含 id=0 虚拟根），按 id 升序。"""
        query = f"""
            SELECT {self._ALL_COLUMNS}
              FROM ingest_source
             WHERE is_deleted = 0
             ORDER BY id ASC
        """
        rows = self._fetchall(query)
        if not rows:
            return []
        return [IngestSource.from_row(row) for row in rows]

    def list_pending_purge_ids(self) -> List[int]:
        """列出所有 is_deleted=1 但物理行仍存在的 source id。
        用于启动时补交中断的清理任务。
        排除 id=0（虚拟源不会被标删）。
        """
        rows = self._fetchall("""
            SELECT id FROM ingest_source
             WHERE is_deleted = 1 AND id != 0
             ORDER BY id ASC
        """)
        return [r["id"] for r in rows] if rows else []

    def get_view_dto(self, source_id: int) -> Optional[IngestSource]:
        """单条查询导入源；不存在返回 ``None``。"""
        query = f"""
            SELECT {self._ALL_COLUMNS}
            FROM ingest_source
            WHERE id = ? AND is_deleted = 0
        """
        row = self._fetchone(query, (source_id,))
        if row is None:
            return None
        return IngestSource.from_row(row)

    def get_id_list(self) -> List[int]:
        rows = self._fetchall("""
        SELECT id 
          FROM ingest_source 
         WHERE is_deleted = 0
         ORDER BY id ASC 
        """)
        if not rows:
            return []
        return [row["id"] for row in rows]

    # ==================================================================
    # 查询：触发配置（轻查询，只读两列）
    #
    # 解析策略：warn-and-skip —— 单条解析失败不拖垮调度器启动。
    # 需要严格语义（"必须拿到配置否则拒绝操作"）的调用方请显式判断
    # 返回的 None，并结合日志判断。这里 None 覆盖三种语义：
    #   1) 源不存在；
    #   2) trigger_config 为 NULL / 空串；
    #   3) 解析失败。
    # ==================================================================

    # noinspection broad-exception
    def get_all_trigger_configs(self) -> List[TriggerConfig]:
        """获取全部**存在** trigger_config 的导入源的强类型配置列表。

        * SQL 层过滤掉 NULL / 空串；
        * 单条解析失败 -> WARNING + 跳过；
        * 成功后把 DB 主键注入 ``config.id``，保证与行数据一致。

        注意：本方法**不**过滤 ``disabled``。禁用源的 trigger_config
        若仍在库中，会被一并返回。调度层的消费方需自行按 source_id
        结合 ``get_sync_candidate`` 判断是否跳过——或者更干净的做法是
        禁用时把 ``trigger_config`` 也清空（由 service 决定策略）。
        """
        query = """
            SELECT id, trigger_config
             FROM ingest_source
            WHERE trigger_config IS NOT NULL AND is_deleted = 0
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
            except Exception as exc:  # noqa: BLE001 —— 见方法 docstring
                self.logger.warning(
                    "解析 trigger_config 失败 (source_id=%s): %s",
                    source_id, exc,
                )
                continue
            if config is None:
                continue
            try:
                config.id = source_id
            except Exception:
                # TriggerConfig 可能 frozen；忽略注入失败。
                pass
            configs.append(config)
        return configs

    # noinspection broad-exception
    def get_trigger_config(self, source_id: int) -> Optional[TriggerConfig]:
        """单源 trigger_config 强类型读取。

        返回 ``None`` 覆盖三种语义（调用方需自行结合日志判断）：
        * 源不存在；
        * ``trigger_config`` 为 NULL / 空串；
        * 解析失败（此时会打 WARNING）。
        """
        query = "SELECT id, trigger_config " \
                "  FROM ingest_source " \
                " WHERE id = ? AND is_deleted = 0"
        row = self._fetchone(query, (source_id,))
        if row is None:
            return None

        raw_config = row["trigger_config"]
        if not raw_config:
            return None

        try:
            config = TriggerConfig.from_json(raw_config)
        except Exception as exc:  # noqa: BLE001 —— 见方法 docstring
            self.logger.warning(
                "解析 trigger_config 失败 (source_id=%s): %s",
                source_id, exc,
            )
            return None
        if config is None:
            return None
        try:
            config.id = row["id"]
        except Exception:
            pass
        return config

    # ==================================================================
    # 写入：创建 / 更新
    # ==================================================================

    def create(self, source: IngestSourceCreate) -> int:
        """创建新的导入源，返回新记录的 ID。

        INSERT 参数在这里内联拼装 —— SQL 占位符顺序属于 repo，
        不再反向要求 DTO 提供 ``to_row_params()``。

        ``disabled`` 允许创建时就为 1：DTO 层已经声明该字段，
        repo 不做额外校验（"建一个禁用的源"是合法意图，
        service 侧的 ``update_source`` 会立刻把它从跟踪集剔除）。
        """
        query = """
            INSERT INTO ingest_source (
                title, description, source_path, target_path, mount_point,
                auto_mount, disabled, file_type_check, file_types, tags,
                subfolder_recursion, subfolder_recursion_depth, trigger_config
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        trigger_json = (
            json.dumps(source.trigger_config, ensure_ascii=False)
            if source.trigger_config is not None
            else None
        )
        params = (
            source.title,
            source.description,
            source.source_path,
            source.target_path,
            source.mount_point,
            int(source.auto_mount),
            int(source.disabled),
            source.file_type_check.value,
            json.dumps(source.file_types, ensure_ascii=False),
            json.dumps(source.tags, ensure_ascii=False),
            int(source.subfolder_recursion),
            source.subfolder_recursion_depth,
            trigger_json,
        )
        cursor = self._execute(query, params)
        return cursor.lastrowid  # type: ignore[return-value]

    def update(self, source_id: int, source: IngestSourceUpdate) -> bool:
        """PATCH 更新：仅更新已显式设置的字段。

        * 字段值 ``UNSET`` -> 跳过（不生成 SET 子句）；
        * 字段值 ``None``  -> ``SET col = NULL``；
        * 其他值           -> ``SET col = serialize(value)``。

        没有任何 SET 子句时返回 ``False``（不触碰 ``modified_at``）。

        ``disabled`` 的 PATCH 语义与别的布尔字段一致：
        ``IngestSourceUpdate(disabled=True)`` → ``SET disabled = 1``；
        ``disabled=False`` → ``SET disabled = 0``。``enable_source`` /
        ``disable_source`` 就是这条路径的薄封装。
        """
        if source.is_empty():
            return False

        set_parts: list[str] = []
        params: list[Any] = []

        for name, serialize in self._UPDATE_SERIALIZERS.items():
            value = getattr(source, name)
            if value is UNSET:
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
    # 存在性 / 引用计数
    # ==================================================================

    def exists(self, source_id: int, *, include_deleted: bool = False) -> bool:
        """判断指定 id 的导入源是否存在。"""
        where = "id = ?" if include_deleted else "id = ? AND is_deleted = 0"
        row = self._fetchone(
            f"SELECT 1 FROM ingest_source WHERE {where} LIMIT 1",
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
    # 删除
    # ==================================================================
    def purge(self, source_id: int) -> SourcePurgeReport:
        """硬删源及旗下所有 assets 行，返回被删资产清单。

        只在源仍处于 is_deleted=1 时生效——restore 后本方法空转，
        保证「误删 → 恢复」不丢数据。

        整个操作在一个事务里完成：
            1. 校验源仍被软删
            2. 读 assets 的 id/uuid 清单
            3. 删 assets 行（FK 是 RESTRICT，必须先删）
            4. 删 source 行（asset_candidate_cache 由 CASCADE 顺带清）
        """
        if source_id == MANUAL_SOURCE_ID:
            raise ValueError(
                f"Cannot purge manual/virtual source (id={MANUAL_SOURCE_ID})"
            )

        with self._transaction() as conn:
            # 1. 校验
            row = conn.execute(
                "SELECT 1 FROM ingest_source WHERE id = ? AND is_deleted = 1",
                (source_id,),
            ).fetchone()
            if row is None:
                return SourcePurgeReport(
                    source_id=source_id, purged=False,
                    asset_ids=[], asset_uuids=[],
                )

            # 2. 收清单
            rows = conn.execute(
                "SELECT id, uuid FROM assets WHERE source_id = ?",
                (source_id,),
            ).fetchall()
            asset_ids = [r["id"] for r in rows]
            asset_uuids = [r["uuid"] for r in rows]

            # 3. 删 assets
            conn.execute("DELETE FROM assets WHERE source_id = ?", (source_id,))

            # 4. 删 source
            conn.execute("DELETE FROM ingest_source WHERE id = ?", (source_id,))

        return SourcePurgeReport(
            source_id=source_id, purged=True,
            asset_ids=asset_ids, asset_uuids=asset_uuids,
        )

    #
    def list_deleted_dtos(self) -> List[IngestSource]:
        """回收站列表：所有 is_deleted=1 的源，按删除时间倒序。"""
        rows = self._fetchall(
            f"""
            SELECT {self._ALL_COLUMNS}
            FROM ingest_source
            WHERE is_deleted = 1 AND id != 0
            ORDER BY deleted_at DESC, id DESC
            """
        )
        return [IngestSource.from_row(r) for r in rows] if rows else []

    def soft_delete(self, source_id: int) -> bool:
        """标记软删。已是删除态 / 不存在 / id=0 时返回 False。"""
        if source_id == MANUAL_SOURCE_ID:
            raise ValueError(
                f"Cannot delete manual/virtual source (id={MANUAL_SOURCE_ID})"
            )
        now = now_iso()
        cursor = self._execute(
            """
            UPDATE ingest_source
               SET is_deleted = 1, deleted_at = ?, modified_at = ?
             WHERE id = ? AND is_deleted = 0
            """,
            (now, now, source_id),
        )
        return cursor.rowcount > 0

    def restore(self, source_id: int) -> bool:
        """撤销软删。行已硬删或从未被软删时返回 False。"""
        cursor = self._execute(
            """
            UPDATE ingest_source
               SET is_deleted = 0, deleted_at = NULL, modified_at = ?
             WHERE id = ? AND is_deleted = 1
            """,
            (now_iso(), source_id),
        )
        return cursor.rowcount > 0
