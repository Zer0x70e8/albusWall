# DTO 契约约定

> 跨层契约（枚举值域 / 异常位置 / 常量分域 / 并发工具边界 / 两条待定项）的**正文**见
> `common/CONVENTIONS.md`。本文件只写 DTO 层落地约束；引用跨层规则一律用指针，不复述正文。
>
> 本文件同时是 **DTO 归属与命名**规则的正文（`common/CONVENTIONS.md` · 文档维护 指向此处）。

## 一、DTO 归属与命名

### 背景：两侧仓储并不是按文件分开的

| 仓储 | 侧 | 依据 |
|------|----|------|
| `repositories/view.py` `ViewRepository` | **查询侧（纯）** | 只读；返回 `Album` / `Asset` / `list[UUID]` |
| `repositories/asset.py` `AssetRepository` | **命令侧（纯）** | 只写，返回 `int`（rowcount），不返回 DTO |
| `IngestSourceRepository` / `TagRepository` / `ThumbnailRepository` / `TrashRepository` / `TrashStateRepository` | **混合** | 同一文件里既有 `get_*/list_*/count_*/search_*`，也有 `create/update/soft_delete/restore/move/purge/clear_snapshot` |
| `ImportRepository` | **命令侧主导** | 候选生命周期写入 + 写前读（`find_missing_paths` / `get_pending_candidates`） |

**结论：DTO 的侧只能按「调用路径」判定，不能按仓储文件判定。** 判据是 DTO 的产生点与
消费点在调用链上的位置：出现在写路径（repo 写方法入参、worker/进程池载荷、UI 提交、状态
回写）的是 Command；出现在读路径（repo 读方法返回 → service 视图 → presenter/UI）的是 Query。

### 归属分类（四类，穷尽）

| 类别 | 定义 | 命名 | 现状位置 |
|------|------|------|----------|
| **Command** | 只由命令路径产生/消费的写模型：入参、PATCH 意图、表单快照、提交描述 | 动词/意图后缀：`*Create` / `*Update` / `*Form` / `*Spec` | `dto/tag/command.py`、`dto/source/command.py`、`dto/thumbnail/command.py`、`dto/import_.py`（`AssetCreate`）、`dto/task.py`（`TaskSpec`） |
| **Query** | 读路径的返回：实体读模型、详情、列表项、聚合视图、统计 | `<实体>` / `*Detail` / `*ListItem`（或等价 `*Row`）/ `*View` / `*Stats` / `*WithStats` / `*TreeNode` / `*Match` | `dto/tag/query.py`、`dto/source/query.py`、`dto/thumbnail/query.py`、`dto/album.py`、`dto/trash.py`（`TrashedAsset`）、`dto/task.py`（`TaskServiceStats`） |
| **事件与结果** | 命令侧产出、供订阅方或调用方消费；既不是浏览视图也不是写模型 | `*Event` / `*Report` / `*Result` | `dto/source/events.py`、`dto/trash.py`（`RestoreReport` / `PurgeReport` / `CleanupReport`）、`dto/thumbnail/command.py`（`ThumbnailResult`）、`dto/task.py`（`BackpressureEvent`） |
| **共享值对象 / 配置值对象** | 两侧都合法引用、不归属任何一侧的纯数据 | 值语义名（无 `Command`/`Query` 暗示） | `dto/thumbnail/paths.py`（`ThumbnailPaths`）、`dto/trigger.py`（`TriggerConfig` / `DeviceTrigger` / `ScheduledTrigger`）、`dto/sentinel.py`（`UNSET` / `PatchField` / `MaybeUnset`，命令侧支撑） |

### R1 定侧按路径

- 一个 DTO 属于哪一侧，由它**被谁产生、被谁消费**决定；不因它所在模块的仓储读写混杂而摇摆。
- 同一文件里可以有两侧 DTO（如 `dto/trash.py`），此时用分区注释 `# ---- Command ----` / `# ---- Query ----` 标注。

### R2 交叉引用与写前读白名单

- **查询路径零引用 Command DTO**：`ViewService`、`repositories/view.py`、任何 presenter / UI 列表与详情
  路径，不得 import `*/command.py`、不得把 Command DTO 作为返回类型或参数类型。
- **命令路径允许"写前读"**：为了写而先读，天然要读 Query 侧读模型。白名单（只允许这些、且只读）：

  | 读模型 | 写前读场景 |
  |--------|-----------|
  | `Tag` | `TagRepository.update/move/soft_delete` 前置校验 |
  | `IngestSource` | 导入 worker 的源配置快照（用别名 `IngestSourceSnapshot` 标注） |
  | `Asset` | 资产存在性 / 可见性前置判断、`ThumbnailPaths.from_asset_dto` 迁移桥 |
  | `ThumbnailPaths` | `clear_and_snapshot*` / `update_paths*` 的路径快照 |
  | `ThumbnailTaskInput` | `get_task_input*` → 生成任务 → 写 `thumb_*` |
  | `PurgeCandidate` | `find_expired/find_any_deleted` → `purge_batch` |

- 白名单只放行"读"，**不得**把读模型当命令入参再传回 repo（例如不得新增
  `repo.update(source: IngestSource)` 这类签名）。
- 读模型上**禁止**出现"构造 Command DTO"的方法（历史 `IngestSource.to_form_data()` 已迁出为
  `dto/source/command.py::form_from_source`）；需要跨侧桥接时，桥放在命令侧或 presenter。

### R3 分包门槛与包内结构

- **门槛**：同模块内 Command 与 Query DTO **均 ≥2 且字段差异明显**（一侧是"改动意图 + 可选/`UNSET`"，
  另一侧是"整行映射 + 展示字段"）才分包；否则单文件只靠命名。
- **包内结构**：`dto/<domain>/__init__.py` + `command.py` + `query.py`（+ 需要时 `events.py` / `paths.py`）。
- `__init__.py` 只做**显式**再导出（列名写死，禁止 `import *`、禁止在 `__init__` 里加语义）；
  新代码建议直接 import 所属子模块，让"读/写侧"出现在 import 行上。
- 现状核算：

  | 模块 | Command | Query | 结论 |
  |------|---------|-------|------|
  | `tag` | 2（`TagCreate` / `TagUpdate`） | 4（`Tag` / `TagTreeNode` / `TagWithStats` / `TagMatch`） | **已分包** |
  | `source` | 3（`Create` / `Update` / `Form`）+ 事件 2 | 1（`IngestSource`）+ 常量 | **已分包**（含 `events.py`） |
  | `thumbnail` | 2（`TaskInput` / `Result`） | 3（`Missing` / `HashRow` / `Stats`）+ 值对象 | **已分包**（含 `paths.py`） |
  | `album` | 0（相簿写尚未实现） | 2（`Album` / `Asset`） | 不分包 |
  | `import_` | 2（`AssetCreate` / `AssetCandidateCache`） | 0 | 不分包 |
  | `trash` | 1（`PurgeCandidate`）+ 报告 6 | 1（`TrashedAsset`）+ 状态 1 | 不分包（Query 仅 1，未达门槛） |
  | `task` | 2（`TaskSpec` / `BackpressureEvent`） | 1（`TaskServiceStats`） | 不分包（Query 未达门槛） |
  | `trigger` | — | — | 不分包（配置值对象） |

### 命名契约

- **禁止新增 `*DTO` 后缀**（`dto/` 包名已隐含）。历史 `*DTO` 已全部去后缀，映射见下表。
- Command 侧不出现 `View` / `Detail` / `List` 字样；Query 侧不出现 `Create` / `Update` / `Form` 字样。
- 单一来源：视图 DTO 只在 `dto/<domain>/query.py` 定义，**不得**在 service / ui 里再声明同名副本
  （`TagTreeNode` / `TagWithStats` / `TagMatch` 原先在 `services/tag.py` 有副本，已下沉）。
- 仅由单一 service 生产并消费、不跨层外流的命令结果（如 `services/tag.py::RestoreResult`）可以留在
  service 内，不算 DTO 归属违规；一旦被 UI 或第二个 service 消费，必须下沉到 `dto/<domain>/`。

改名对照（本次已落地）：

| 旧名 | 现名 | 侧 |
|------|------|----|
| `AssetDTO` | `Asset` | Query（实体读模型） |
| `AssetCreateDTO` | `AssetCreate` | Command |
| `AssetCandidateCacheDTO` | `AssetCandidateCache` | Command（见「例外」） |
| `IngestSourceFormData` | `IngestSourceForm` | Command |
| `TrashedAssetDTO` | `TrashedAsset` | Query |
| `TrashStateDTO` | `TrashState` | Query（单例行状态） |
| `PurgeCandidateDTO` | `PurgeCandidate` | 命令侧写前读 |
| `ClockJumpDTO` | `ClockJump` | 事件与结果 |
| `RestoreConflictDTO` | `RestoreConflict` | 事件与结果 |
| `ThumbFailureDTO` | `ThumbFailure` | 事件与结果 |
| `RestoreReportDTO` | `RestoreReport` | 事件与结果 |
| `PurgeReportDTO` | `PurgeReport` | 事件与结果 |
| `CleanupReportDTO` | `CleanupReport` | 事件与结果 |
| `TagSelection` | （删除） | 零引用死代码 |
| `ThumbnailTask` | （删除） | 零引用死代码 |

保留名（已符合约定，不改）：`Album`、`Tag`、`TagCreate`、`TagUpdate`、`TagTreeNode`、`TagWithStats`、
`TagMatch`、`IngestSource`、`IngestSourceCreate`、`IngestSourceUpdate`、`IngestSourceSnapshot`（别名）、
`SourceScanFinished`、`SourcePurgeReport`、`ThumbnailPaths`、`MissingThumbnailRow`、`ThumbnailHashRow`、
`ThumbnailStats`、`ThumbnailTaskInput`、`ThumbnailResult`、`BackpressureEvent`、`TaskServiceStats`、
`TaskSpec`、`TriggerConfig`、`DeviceTrigger`、`ScheduledTrigger`、`UNSET`、`PatchField`、`MaybeUnset`、
`MANUAL_SOURCE_ID`。

## 二、已定

### UUID 统一

- 结论：DTO 对外字段统一为 `UUID`；**Query DTO** 由 `from_row` 负责 `str→UUID`，**Command DTO** 由
  `to_insert_params()`（`dto/import_.py`）或 repo 绑定参数负责 `UUID→str`。
- 理由：类型转换收敛在 DTO 边界，避免业务层混用 `str`/`UUID`；service 层一律不手转。
- 锚点：TODO（当前无 DTO 契约测试，见文末「锚点现状」）
- 影响文件：`dto/album.py`、`dto/import_.py`、`dto/tag/query.py`、`dto/source/query.py`、
  `dto/thumbnail/{paths,query,command}.py`、`dto/trash.py`

### is_* 布尔口径

- 结论：**Query DTO** 的 `is_favorite` 用 `bool`；`AssetCreate.is_favorite: int` 保留（未来星级）；
  其余 `is_*` 一律 `bool`，含 `AssetCreate.is_deleted` 改 `bool`。
- 理由：读模型对外布尔化，写模型仅为未来星级保留一个 `int`。
- 锚点：TODO（当前无 DTO 契约测试）
- 影响文件：`dto/album.py`、`dto/import_.py`、`dto/trash.py`、`dto/source/query.py`、`dto/tag/query.py`

### source_id 默认值

- 结论：`source_id` 默认值统一为 `0`；`Asset` 现有 `-1` 只记录为待改，本阶段不改。
- 理由：与手动导入虚拟根及 schema DEFAULT 对齐。
- 锚点：TODO（当前无 DTO 契约测试）
- 影响文件：`dto/album.py`、`dto/import_.py`、`dto/source/query.py`

### 可变性

- 结论：不强制 `frozen`，DTO 在业务逻辑中不会被原地修改；`BackpressureEvent` 单独 `frozen=True`。
- 理由：可变性由使用约定保证，不把 `frozen` 作为统一硬约束；只读回调 payload 例外。
- 锚点：TODO（可变性本身不可测；`BackpressureEvent` 只读由 `test_backpressure_event_is_frozen` 覆盖，
  该测试当前**不存在**）
- 影响文件：`dto/album.py`、`dto/import_.py`、`dto/sentinel.py`、`dto/source/*.py`、`dto/tag/*.py`、
  `dto/task.py`、`dto/thumbnail/*.py`、`dto/trash.py`、`dto/trigger.py`

### __all__

- 结论：**分包模块**的 `__init__.py` 必须显式列出导出（列名写死），禁止通配再导出；
  单文件模块的 `__all__` 维持现状，新增类时同步补上。禁止在 `__init__` 里添加额外语义。
- 理由：分包后 `__init__` 成为包级公开面，必须可控；通配导出会让"谁属哪侧"不可见。
- 锚点：TODO（结构性规则，无可测锚点）
- 影响文件：`dto/tag/__init__.py`、`dto/source/__init__.py`、`dto/thumbnail/__init__.py`、
  `dto/album.py`、`dto/import_.py`、`dto/source/query.py`

### from_row 字段容错分层

- 结论：**Query DTO** 的 `from_row` 中 **身份 / 必填字段直取 `row[key]`**（缺列即 schema/SELECT 错误，
  应暴露）；**可选 / 展示字段走 `dto.utils.row_get`**（缺列返回 default）。
- 理由：容错是给"本来就可能没有的列"的，不是给"应该有但没有"的列。全容错会把 SELECT/schema 错误静默成
  `-1` / `""` / `None`，错误拖到 UI 才暴露；全严格会杀掉轻查询（只 select 部分列）的能力。
- 限定：本规则只适用于行投影（Query DTO 与命令侧写前读）——写模型（`*Create` / `*Update` / `*Form` /
  `*Spec`）不得有 `from_row`（见「from_row 固定吃 sqlite3.Row」）。
- 锚点：TODO（当前无 DTO 契约测试）
- 影响文件：`dto/album.py`、`dto/source/query.py`、`dto/thumbnail/{paths,query,command}.py`、`dto/trash.py`
- 备注：`Asset.from_row` 采用 `dict(row)` + `.get()` 风格，语义等价（身份直取、可选容错），属于
  **已承认的并存风格**，本阶段不迁移。

### Album.description

- 结论：`Album.description: str = ""`。
- 理由：按拍板用空字符串，不用 `Optional[str]`。
- 锚点：TODO（当前无 DTO 契约测试）
- 影响文件：`dto/album.py`

### 全改 dataclass

- 结论：撤销 TypedDict 例外，`trash` / `task` / `source` 所有 TypedDict 改 dataclass；`BackpressureEvent` 合并为单
  dataclass，复用 `UNSET` / `MaybeUnset` 区分"未携带 vs 显式 None"。
- 理由：例外无实际收益，统一形状。
- 锚点：TODO（当前无 DTO 契约测试）
- 影响文件：`dto/trash.py`、`dto/task.py`、`dto/source/query.py`

### from_row 固定吃 sqlite3.Row

- 结论：`from_row` 协议固定接受 `sqlite3.Row`，不兼容 `Mapping`。
- **限定（本次修订）**：本协议适用于所有**行投影**——Query DTO，以及命令侧写前读的行投影
  （`ThumbnailTaskInput`、`PurgeCandidate`）。**命令侧"写模型"不得提供 `from_row`**：
  `*Create` / `*Update` / `*Form` / `*Spec` 一律没有 `from_row`，写路径用 `to_insert_params()`
  或 repo 内联绑定。唯一例外：`AssetCandidateCache`（它是写模型，但需要同事务回读，见「例外」）。
- 理由：不假设将来更换数据库；把 `from_row` 与"写模型"分开，避免写模型也能从行构造而模糊读写界限。
- 锚点：TODO（当前无 DTO 契约测试）
- 影响文件：`dto/album.py`、`dto/import_.py`、`dto/source/query.py`、`dto/thumbnail/{paths,query,command}.py`、
  `dto/trash.py`、`repositories/base.py`（`RowConstructible` 协议）

### to_dict 浅转换

- 结论：`to_dict` 只变 dict，保持 `UUID` 和 `Enum` 原样，不要求实现 `to_jsonable`。
- 理由：`to_dict` 面向日志/调试，不强加 json 序列化责任。
- 锚点：TODO（当前无 DTO 契约测试）
- 影响文件：`dto/album.py`、`dto/trigger.py`

### 枚举容错

- 跨层规则（枚举位置 / 读降级 / 写抛错 / 禁止 `_missing_` / `NONE` 保留）见
  `common/CONVENTIONS.md` · 枚举值域与容错。
- 本层落地约束：所有读路径枚举容错统一走 `dto.utils.coerce_enum`，禁止各 DTO 自建 `_coerce_*` 私有函数。
    - 有默认值：`coerce_enum(value, EnumCls, default)` → 返回 `EnumCls`；
    - 无默认值：`coerce_enum(value, EnumCls)` → 返回 `EnumCls | None`，写路径拿到 `None` 后由调用方 `raise ValueError`。
- 本层不留枚举定义：`dto/task.py` 的 `ExecutorType` / `BackpressureReason` / `PermitKind` 已迁入
  `common/enums/task.py`，只保留入参类型别名 `ExecutorArg`。
- 理由：容错只在 DTO 边界发生一次，避免各 DTO 各写一份；读侧不阻塞，写侧不吞错。
- 锚点：TODO（当前无 DTO 契约测试）
- 影响文件：`dto/source/query.py`、`dto/import_.py`、`dto/trigger.py`、`dto/task.py`、`dto/utils/convert.py`

### 对外用 uuid

- 结论：对外（service 以上）统一用 `uuid` 取对象；`id` 留给 repo 排序 / 定位。
- **本次修订**：撤销原「未来分离 uuid 入口 / id 入口两类 DTO」的说法 —— DTO 的分类轴**只有一条**，
  即 Command / Query（见「一、DTO 归属与命名」）。`id` / `uuid` 降为**字段口径**：
  对外字段用 `uuid`；`id` 只允许出现在 Query 实体读模型（内部排序用）与命令侧内部行模型
  （`AssetCandidateCache.id` / `PurgeCandidate.id` / `ThumbnailTaskInput.asset_id`）。
- 理由：分类轴必须唯一，否则命名规则无处落地；`id` 是可观测字段而不是架构分区。
- 锚点：TODO（建议补 `test_dto_ownership.py` 的 grep 断言）
- 影响文件：`dto/album.py`、`dto/trash.py`、`dto/thumbnail/*.py`、`dto/import_.py`

### TriggerConfig.to_dict

- 结论：`TriggerConfig.to_dict` 保留 `id`、`update_mode` 保持 `Enum` 原样；存储形态走
  `IngestSourceForm.to_trigger_config()`。
- 理由：运行时视图与存储视图职责分离，运行时字段不污染存储。
- 锚点：TODO（当前无 DTO 契约测试）
- 影响文件：`dto/trigger.py`、`dto/source/command.py`

### COLUMNS 与 fields ()

- 结论：**带 `COLUMNS` 的 Command 写模型**（`AssetCreate` / `AssetCandidateCache`）的 `COLUMNS`
  与 `dataclasses.fields()` 名称集合一致（不多不少），顺序不强求。Query DTO 不得声明 `COLUMNS`。
- 理由：顺序一致在 `AssetCandidateCache` 上不可行（`id` 有默认值会破坏构造），集合一致足以防漏/拼错；
  把 `COLUMNS` 限制在写模型，避免把它误解成"所有 DTO 都有列清单"。
- 锚点：TODO（当前无 DTO 契约测试）
- 影响文件：`dto/import_.py`

### sentinel 集中维护

- 结论：`dto/sentinel.py` 集中维护 `UNSET` / `_UnsetType` / `PatchField`；新增中性别名 `MaybeUnset[T] = PatchField[T]`，
  `BackpressureEvent` 用 `MaybeUnset`。
- 理由：单例语义需全局唯一，不能各域自建；命名与语义对齐。
- 锚点：TODO（当前无 DTO 契约测试）
- 影响文件：`dto/sentinel.py`、`dto/task.py`、`dto/source/command.py`、`dto/tag/command.py`

### 转换工具集中维护

- 结论：DTO 层的边界转换统一收敛到 `dto/utils/convert.py`：
    - `to_uuid` —— `str` / `UUID` → `UUID`；
    - `coerce_enum` —— 读路径枚举容错（见「枚举容错」）；
    - `row_get` —— `sqlite3.Row` 缺列兜底（见「from_row 字段容错分层」）；
    - `dump_json` / `json_default` —— JSON 序列化兜底（`Enum → value`）；原名 `to_json`，已改名并删除
      `dump_json = to_json` 别名，避免与 DTO 的 `to_json()` 方法同名混淆；
    - `normalize_tag_name` —— tag 名归一化契约。
- 结论（禁止）：不得在各 DTO 内自建 `_coerce_*` / `_row_get` / `_get` / 私有 `normal` 等副本。
- 理由：契约只写一次，避免各域自建导致语义漂移。
- 锚点：TODO（当前无 DTO 契约测试）
- 影响文件：`dto/utils/convert.py`、`dto/utils/__init__.py`、`dto/source/query.py`、`dto/import_.py`、
  `dto/thumbnail/*.py`、`dto/trigger.py`

## 三、待定

> 本层待定项已上移至 `common/CONVENTIONS.md` · 待定（单一正文），本文件不再维护候选方案。

| # | 问题 | 状态 | 正文 |
|---|------|------|------|
| 1 | `FileTypeCheckMode.NONE` 与 `IngestSource.is_void()` 的联动 | 已拍板：排除哨兵（`is NONE` → void） | `common/CONVENTIONS.md` · 5.1 |
| 2 | `trigger_config.update_mode` 的形态 | 已拍板：单值 `UpdateMode`（列表形态已删除） | `common/CONVENTIONS.md` · 5.2 |

## 四、例外

- `dto/utils/convert.py`：DTO 层共享转换工具（`to_uuid` / `coerce_enum` / `row_get` / `dump_json` / `json_default` /
  `normalize_tag_name`），被所有 `from_row` / `__post_init__` / 写路径依赖。不映射表、不套表 DTO 契约；
  必须集中维护、不得各域自建私有副本。
- `AssetCandidateCache`（`dto/import_.py`）：Command DTO，但同时提供 `from_row`。理由：`claim_next_candidate`
  在同一个事务里 UPDATE（抢占）后回读整行，回读只喂给命令侧 worker（`process_candidate`），
  不外流给查询路径或 UI。它仍带 `to_insert_params()`，写模型身份不变。
- `services/tag.py::RestoreResult`：仅由 `TagService.restore` 生产并消费的命令结果，不外流给 UI；
  按「命名契约」最后一条留在 service 内。一旦有第二个消费方，下沉到 `dto/tag/`。
- `dto/thumbnail/paths.py::ThumbnailPaths`：共享值对象，两侧都允许 import；不得为某一侧复制一份。
- `dto/thumbnail/` 不再定义 schema 常量：`ALL_SPECS` / `SPEC_TO_COLUMN` / `COLUMN_TO_SPEC` / `BASE_COLUMN` /
  `ALL_THUMB_COLUMNS` 已搬到 `common/constants/thumbnail.py`（见 `common/CONVENTIONS.md` · 常量）。
- 原 `dto/trash.py` 的 TypedDict 例外已撤销：全部改 dataclass，纳入通用契约（见「已定 · 全改 dataclass」）。
- 「包内模块不算例外」：`dto/tag/`、`dto/source/`、`dto/thumbnail/` 的 `command.py` / `query.py` /
  `events.py` / `paths.py` 全部适用本文件的通用契约，子模块不因分包而豁免。

## 五、锚点现状

本文件历史锚点全部指向 `tests/dto/test_contracts.py` 与 `tests/dto/test_utils_convert.py`，
**这两个文件在当前仓库都不存在**（测试树是扁平 `test/`，且没有任何 DTO 契约测试）。
因此各条目的「锚点」暂统一记为 `TODO`。恢复锚点的最低方案是新增 `test/test_dto_ownership.py`
（结构断言，不需要数据库）：

```python
# 1) 查询路径不得引用 Command DTO（R2 反向）
#    允许的例外见「例外」与「写前读白名单」
# 2) 视图 DTO 不得在 service/ui 里重复定义（单一来源）
# 3) 分包模块的 __init__ 导出与子模块 __all__ 一致
# 4) Command DTO 不得出现 from_row（AssetCandidateCache 除外）
```

<!-- grep-checklist
- 归属：查询路径引用 Command 子模块（应为 0；命令入口 presenter / 表单控件不算查询路径）
  `grep -rn "dto\.tag\.command\|dto\.source\.command\|dto\.thumbnail\.command" --include=*.py src/albuswall/repositories/view.py src/albuswall/services/view.py src/albuswall/services/asset_tag.py src/albuswall/ui/builtin/presenter/album_presenter.py src/albuswall/ui/builtin/presenter/viewer_presenter.py`
- 归属：读模型上出现"构造 Command DTO"的方法（应为 0，桥在命令侧）
  `grep -rn "def to_form_data\|def to_create\|def to_update" --include=*.py src/albuswall/dto`
- 归属：视图 DTO 在 service/ui 重复定义（应为 0）
  `grep -rn "class TagTreeNode\|class TagWithStats\|class TagMatch" --include=*.py src/albuswall/services src/albuswall/ui`
- 命名：新增 *DTO 后缀（应为 0）
  `grep -rnE "class [A-Za-z]+DTO\b" --include=*.py src/albuswall`
- 契约：写模型（Create / Update / Form / Spec）里的 from_row（应为 1，唯一例外 `AssetCandidateCache`）
  `grep -rnE "^class (AssetCreate|AssetCandidateCache|TagCreate|TagUpdate|IngestSourceCreate|IngestSourceUpdate|IngestSourceForm|TaskSpec)" -A 40 --include=*.py src/albuswall/dto -r | grep -c "def from_row"`
- 契约：`COLUMNS` 只出现在 Command 写模型（Query 侧应为 0）
  `grep -rnE "^\s+COLUMNS:" --include=*.py src/albuswall/dto`
- UUID：写路径 str(uuid) 转换位置（应收敛在 to_insert_params）
  `grep -rn "INSERT INTO assets" --include=*.py src`
- UUID：service 以上层消费 DTO.id 的调用点
  `grep -rn "\.id\b" --include=*.py src/albuswall/services src/albuswall/ui`
- TriggerConfig.to_dict / to_json / trigger_config 的写入与存储调用点
  `grep -rn "TriggerConfig\|trigger_config\|to_json()" --include=*.py src/albuswall`
- update_mode 的单值形态（列表形态应为 0）
  `grep -rn "update_mode" --include=*.py src/albuswall`
- 枚举 `_missing_` 定义（应为 0；别被 find_missing_paths 之类子串误导）
  `grep -rn "def _missing_" --include=*.py src/albuswall`
- DTO 内定义异常类（应为 0）
  `grep -rn "class .*Exception\|class .*Error" --include=*.py src/albuswall/dto`
- DTO 内定义枚举（应为 0）
  `grep -rn "class .*Enum" --include=*.py src/albuswall/dto`
- repo/service 反向从 dto 取 schema 常量（应为 0；DTO 类本身从 dto 导入是正常的，只盯常量名）
  `grep -rn "from albuswall.dto.thumbnail" --include=*.py src/albuswall/repositories src/albuswall/services | grep -E "ALL_SPECS|SPEC_TO_COLUMN|COLUMN_TO_SPEC|BASE_COLUMN|ALL_THUMB_COLUMNS"`
- TypedDict 定义（应为 0）
  `grep -rn "class .*TypedDict" --include=*.py src/albuswall/dto`
- 旧别名 `to_json = dump_json` 残留（应为 0；`getattr(value, "to_json")` 属正常调用）
  `grep -rn "^to_json = \|^dump_json = " --include=*.py src`
-->

## ⚠️ 风险提示

- `AssetCreate.is_deleted` 改 `bool` 会波及现有 `int` 绑定/写入点，需 grep 确认。
- `TriggerConfig.update_mode` 已统一为**单值** `UpdateMode`（列表形态已删除），见
  `common/CONVENTIONS.md` · 5.2；新增 / 修改触发方式时只动单值判断，不要复活 `list` 形态或归一函数。
- `FileTypeCheckMode.NONE` 与 `IngestSource.is_void()` 已按 `common/CONVENTIONS.md` · 5.1 改判为**排除哨兵**；
  `OFF` / `MAGIC_LAX` 是 schema 值域内的合法成员（v0.1.3 新增），不得再被 `is_void()` 判为不可用。
- 删除 `_missing_` 后，任何 `EnumCls(未知值)` 直调都会抛 `ValueError`；读路径必须走 `coerce_enum`，不得依赖枚举自身降级。
- TypedDict 全改 dataclass 后，`BackpressureEvent` 消费方原先的键存在性判断需改为 `is UNSET`，调用点需排查。
- **分包/改名后的 import 面**：`dto/tag`、`dto/source`、`dto/thumbnail` 由模块变包，旧路径
  `from albuswall.dto.source import X` 仍可用（`__init__` 显式再导出），但新代码应 import 子模块；
  重命名对外部插件（`plugin/`）是**破坏性变更**，若有第三方插件按旧名 import，需要 `AssetDTO = Asset`
  这类别名过渡（当前仓库内无此类引用）。
- **`IngestSource` 仍是单一类**：命令侧用 `IngestSourceSnapshot` 别名标注"只读快照"，二者同一个类；
  如果将来真的需要给命令侧不同字段，必须新开类，不要往 `IngestSource` 上继续加命令侧专属字段。
- **`Asset` 的 `thumb_*` 镜像字段**与 `ThumbnailPaths` 并存：新代码一律用 `ThumbnailPaths`；
  不得再给 `Asset` 增加与 canonical 值对象重复的镜像字段。

## 变更记录

### 2026-10-25 · DTO 按 Command / Query 归位

**结论变更**

1. 新增「一、DTO 归属与命名」正文：R1 定侧按路径（六个混合仓储不按文件定侧）、R2 交叉引用与写前读白名单、
   R3 分包门槛（两侧均 ≥2 且字段差异明显）、四类归属（Command / Query / 事件与结果 / 共享值对象）与命名契约。
2. 撤销「对外用 uuid · 未来分离 uuid 入口 / id 入口两类 DTO」：分类轴唯一为 Command / Query，
   `id`/`uuid` 降为字段口径。
3. 「from_row 固定吃 sqlite3.Row」「from_row 字段容错分层」「COLUMNS 与 fields ()」全部限定侧别：
   前两条只约束 Query DTO，第三条只约束带 `COLUMNS` 的 Command 写模型；新增"Command DTO 禁止 `from_row`"。
4. 「is_* 布尔口径」「UUID 统一」术语从"读 DTO / 写 DTO"改为 Query DTO / Command DTO，
   并明确 UUID→str 收敛在 Command 的 `to_insert_params()`。
5. 「__all__」由"后补"改为"分包模块 `__init__` 显式导出、禁止通配"。
6. 例外章节重写：原先"写 DTO、事件 DTO、报告 DTO、service/UI 视图按通用契约的不同子类处理"的笼统说法
   作废，改为逐类落位 + 单一来源 + `AssetCandidateCache` 回读例外 + `RestoreResult` 留在 service 的条件。
7. 锚点全部标记 `TODO` 并说明原因（`tests/dto/*` 不存在），给出 `test/test_dto_ownership.py` 的最低方案；
   grep-checklist 按新路径重写，并收紧 `_missing_` / `to_json =` 等易假阳性的匹配。

**代码落地（同批）**

- 分包：`dto/tag/{__init__,command,query}.py`、`dto/source/{__init__,command,query,events}.py`、
  `dto/thumbnail/{__init__,command,query,paths}.py`；`album` / `import_` / `trash` / `task` / `trigger` 保持单文件。
- 去 `*DTO` 后缀：`Asset`、`AssetCreate`、`AssetCandidateCache`、`IngestSourceForm`、`TrashedAsset`、
  `TrashState`、`PurgeCandidate`、`ClockJump`、`RestoreConflict`、`ThumbFailure`、`RestoreReport`、
  `PurgeReport`、`CleanupReport`。
- 下沉视图 DTO：`TagTreeNode` / `TagWithStats` / `TagMatch` 从 `services/tag.py` 下沉到 `dto/tag/query.py`
  （原 `dto/tag.py` 的重复副本删除，终结两处字段漂移：`path: str` vs `tuple[str, ...]`）。
- 切断 Query→Command 倒挂：删 `IngestSource.to_form_data()`，改为 `dto/source/command.py::form_from_source()`，
  由 presenter 调用。
- 命名语义：`IngestSourceSnapshot`（`IngestSource` 的别名）用于命令侧写前读的签名标注（如
  `ImportService._load_source_map`）。
- 删除零引用死代码：`TagSelection`、`ThumbnailTask`。
- 调用方 import 全部改为子模块导入（repo / service / ui / test 各一处），行为零变化。
