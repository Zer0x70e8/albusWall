# DTO 契约约定

> 跨层契约（枚举值域 / 异常位置 / 常量分域 / 并发工具边界 / 两条待定项）的**正文**见
> `common/CONVENTIONS.md`。本文件只写 DTO 层落地约束；引用跨层规则一律用指针，不复述正文。

## 一、已定

### UUID 统一

- 结论：DTO 对外字段统一为 `UUID`；`from_row` 负责 `str→UUID`，写路径负责 `UUID→str`。
- 理由：类型转换收敛在 DTO 边界，避免业务层混用 `str`/`UUID`。
- 锚点：`tests/dto/test_contracts.py::test_uuid_fields_are_uuid_and_write_paths_convert`
- 影响文件：`dto/album.py`、`dto/import_.py`、`dto/tag.py`、`dto/thumbnail.py`、`dto/trash.py`

### is_* 布尔口径

- 结论：读 DTO 的 `is_favorite` 用 `bool`；`AssetCreateDTO.is_favorite: int` 保留（未来星级）；其余 `is_*` 一律 `bool`，含
  `AssetCreateDTO.is_deleted` 改 `bool`。
- 理由：读模型对外布尔化，写模型仅为未来星级保留一个 `int`。
- 锚点：`tests/dto/test_contracts.py::test_is_star_fields_bool_except_asset_create_favorite_int`
- 影响文件：`dto/album.py`、`dto/import_.py`、`dto/trash.py`、`dto/source.py`、`dto/tag.py`

### source_id 默认值

- 结论：`source_id` 默认值统一为 `0`；`AssetDTO` 现有 `-1` 只记录为待改，本阶段不改。
- 理由：与手动导入虚拟根及 schema DEFAULT 对齐。
- 锚点：`tests/dto/test_contracts.py::test_source_id_default_is_zero`
- 影响文件：`dto/album.py`、`dto/import_.py`、`dto/source.py`

### 可变性

- 结论：不强制 `frozen`，DTO 在业务逻辑中不会被原地修改；`BackpressureEvent` 单独 `frozen=True`。
- 理由：可变性由使用约定保证，不把 `frozen` 作为统一硬约束；只读回调 payload 例外。
- 锚点：TODO（可变性本身不可测，不硬凑锚点；`BackpressureEvent` 只读由 `test_backpressure_event_is_frozen` 覆盖）
- 影响文件：`dto/album.py`、`dto/import_.py`、`dto/sentinel.py`、`dto/source.py`、`dto/tag.py`、`dto/task.py`、
  `dto/thumbnail.py`、`dto/trash.py`、`dto/trigger.py`

### __all__

- 结论：`__all__` 后补，不在阶段 0 处理。
- 理由：优先产出决策表，不扩大导出面治理范围。
- 锚点：TODO（本阶段不处理，无法硬凑锚点）
- 影响文件：`dto/__init__.py`、`dto/source.py`

### from_row 字段容错分层

- 结论：`from_row` 中 **身份 / 必填字段直取 `row[key]`**（缺列即 schema/SELECT 错误，应暴露）； **可选 / 展示字段走
  `dto.utils.row_get`**（缺列返回 default）。
- 理由：容错是给“本来就可能没有的列”的，不是给“应该有但没有”的列。全容错会把 SELECT/schema 错误静默成 `-1` / `""` / `None`
  ，错误拖到 UI 才暴露；全严格会杀掉轻查询（只 select 部分列）的能力。
- 锚点：`tests/dto/test_utils_convert.py::test_row_get_strict_vs_lenient`
- 影响文件：`dto/album.py`、`dto/import_.py`、`dto/source.py`、`dto/thumbnail.py`、`dto/trash.py`
- 备注：`AssetDTO.from_row` 采用 `dict(row)` + `.get()` 风格，语义等价（身份直取、可选容错），属于 **已承认的并存风格**
  ，本阶段不迁移。

### Album.description

- 结论：`Album.description: str = ""`。
- 理由：按拍板用空字符串，不用 `Optional[str]`。
- 锚点：`tests/dto/test_contracts.py::test_album_description_default_empty_str`
- 影响文件：`dto/album.py`

### 全改 dataclass

- 结论：撤销 TypedDict 例外，`trash` / `task` / `source` 所有 TypedDict 改 dataclass；`BackpressureEvent` 合并为单
  dataclass，复用 `UNSET` / `MaybeUnset` 区分“未携带 vs 显式 None”。
- 理由：例外无实际收益，统一形状。
- 锚点：`tests/dto/test_contracts.py::test_all_dto_are_dataclass_no_typeddict`
- 影响文件：`dto/trash.py`、`dto/task.py`、`dto/source.py`

### from_row 固定吃 sqlite3.Row

- 结论：`from_row` 协议固定接受 `sqlite3.Row`，不兼容 `Mapping`。
- 理由：不假设将来更换数据库。
- 锚点：`tests/dto/test_contracts.py::test_from_row_accepts_sqlite3_row`
- 影响文件：`dto/album.py`、`dto/import_.py`、`dto/source.py`、`dto/thumbnail.py`、`dto/trash.py`

### to_dict 浅转换

- 结论：`to_dict` 只变 dict，保持 `UUID` 和 `Enum` 原样，不要求实现 `to_jsonable`。
- 理由：`to_dict` 面向日志/调试，不强加 json 序列化责任。
- 锚点：`tests/dto/test_contracts.py::test_to_dict_preserves_uuid_and_enum`
- 影响文件：`dto/album.py`、`dto/trigger.py`、`dto/thumbnail.py`

### 枚举容错

- 跨层规则（枚举位置 / 读降级 / 写抛错 / 禁止 `_missing_` / `NONE` 保留）见
  `common/CONVENTIONS.md` · 枚举值域与容错。
- 本层落地约束：所有读路径枚举容错统一走 `dto.utils.coerce_enum`，禁止各 DTO 自建 `_coerce_*` 私有函数。
    - 有默认值：`coerce_enum(value, EnumCls, default)` → 返回 `EnumCls`；
    - 无默认值：`coerce_enum(value, EnumCls)` → 返回 `EnumCls | None`，写路径拿到 `None` 后由调用方 `raise ValueError`。
- 本层不留枚举定义：`dto/task.py` 的 `ExecutorType` / `BackpressureReason` / `PermitKind` 已迁入
  `common/enums/task.py`，只保留入参类型别名 `ExecutorArg`。
- 理由：容错只在 DTO 边界发生一次，避免各 DTO 各写一份；读侧不阻塞，写侧不吞错。
- 锚点：`tests/dto/test_contracts.py::test_enum_read_degrades_write_raises`、
  `tests/dto/test_utils_convert.py::test_coerce_enum`
- 影响文件：`dto/source.py`、`dto/import_.py`、`dto/trigger.py`、`dto/task.py`、`dto/utils/convert.py`

### 对外用 uuid

- 结论：对外（service 以上）统一用 `uuid` 取对象；`id` 留给 repo 排序/定位；现有 DTO 的 `id` 先不动，未来分离 uuid 入口 / id
  入口两类 DTO。
- 理由：外部标识稳定，内部排序可用整数。
- 锚点：`tests/dto/test_contracts.py::test_service_layer_does_not_consume_dto_id`（建议，需配合 grep 清单确认调用点）
- 影响文件：`dto/album.py`、`dto/trash.py`、`dto/thumbnail.py`

### TriggerConfig.to_dict

- 结论：`TriggerConfig.to_dict` 保留 `id`、`update_mode` 保持 `Enum` 原样；存储形态走
  `IngestSourceFormData.to_trigger_config()`。
- 理由：运行时视图与存储视图职责分离，运行时字段不污染存储。
- 锚点：`tests/dto/test_contracts.py::test_trigger_config_to_dict_keeps_id_and_enum`
- 影响文件：`dto/trigger.py`、`dto/source.py`

### COLUMNS 与 fields ()

- 结论：`COLUMNS` 与 `dataclasses.fields()` 名称集合一致（不多不少），顺序不强求。
- 理由：顺序一致在 `AssetCandidateCacheDTO` 上不可行（`id` 有默认值会破坏构造），集合一致足以防漏/拼错。
- 锚点：`tests/dto/test_contracts.py::test_columns_name_set_equals_dataclass_fields`
- 影响文件：`dto/import_.py`

### sentinel 集中维护

- 结论：`dto/sentinel.py` 集中维护 `UNSET` / `_UnsetType` / `PatchField`；新增中性别名 `MaybeUnset[T] = PatchField[T]`，
  `BackpressureEvent` 用 `MaybeUnset`。
- 理由：单例语义需全局唯一，不能各域自建；命名与语义对齐。
- 锚点：`tests/dto/test_contracts.py::test_maybe_unset_alias_matches_patch_field`
- 影响文件：`dto/sentinel.py`、`dto/task.py`、`dto/source.py`、`dto/tag.py`

### 转换工具集中维护

- 结论：DTO 层的边界转换统一收敛到 `dto/utils/convert.py`：
    - `to_uuid` —— `str` / `UUID` → `UUID`；
    - `coerce_enum` —— 读路径枚举容错（见「枚举容错」）；
    - `row_get` —— `sqlite3.Row` 缺列兜底（见「from_row 字段容错分层」）；
    - `dump_json` / `json_default` —— JSON 序列化兜底（`Enum → value`）；原名 `to_json`，已改名并删除
      `dump_json = to_json` 别名，避免与 DTO 的 `to_json()` 方法同名混淆；
    - `normalize_tag_name` —— tag 名归一化契约。
- 结论（禁止）：不得在各 DTO 内自建 `_coerce_*` / `_row_get` / `_get` / 私有 `normal` 等副本；历史副本已删除（
  `_coerce_status`、`_coerce_file_type_check`、`_row_get`、`thumbnail._get`、`normal(v, t)`），由 grep 清单守护。
- 理由：同上（`sentinel`）——契约只写一次，避免各域自建导致语义漂移。
- 锚点：`tests/dto/test_utils_convert.py::test_coerce_enum`、`::test_row_get_strict_vs_lenient`
- 影响文件：`dto/utils/convert.py`、`dto/utils/__init__.py`、`dto/source.py`、`dto/import_.py`、`dto/thumbnail.py`、
  `dto/trigger.py`

## 二、待定

> 本层待定项已上移至 `common/CONVENTIONS.md` · 待定（单一正文），本文件不再维护候选方案。

| # | 问题 | 状态 | 正文 |
|---|------|------|------|
| 1 | `FileTypeCheckMode.NONE` 与 `IngestSource.is_void()` 的联动 | 已拍板：排除哨兵（`is NONE` → void） | `common/CONVENTIONS.md` · 5.1 |
| 2 | `trigger_config.update_mode` 的形态 | 已拍板：单值 `UpdateMode`（列表形态已删除） | `common/CONVENTIONS.md` · 5.2 |

## 三、例外

- `dto/utils/convert.py`：DTO 层共享转换工具（`to_uuid` / `coerce_enum` / `row_get` / `dump_json` / `json_default` /
  `normalize_tag_name`），被所有 `from_row` / `__post_init__` / 写路径依赖。不映射表、不套表 DTO 契约；必须集中维护、不得各域自建私有副本（历史副本
  `_coerce_status` / `_coerce_file_type_check` / `_row_get` / `_get` 已删除）。
- `dto/thumbnail.py` 不再定义 schema 常量：`ALL_SPECS` / `SPEC_TO_COLUMN` / `COLUMN_TO_SPEC` / `BASE_COLUMN` /
  `ALL_THUMB_COLUMNS` 已搬到 `common/constants/thumbnail.py`（见 `common/CONVENTIONS.md` · 常量）；DTO 只从该模块
  import 自己用到的 `ALL_SPECS`，不再对外转口 schema 常量。
- 原 `dto/trash.py` 的 TypedDict 例外已撤销：全部改 dataclass，纳入通用契约（见「已定 · 全改 dataclass」）。
- 其余写 DTO、事件 DTO、报告 DTO、service/UI 视图（如 `TagTreeNode`、`ThumbnailPaths`、`SourceScanFinished`、
  `SourcePurgeReport`）按通用契约的不同子类处理，不列为例外。

<!-- grep-checklist
- UUID 统一后所有 INSERT assets 的绑定点的 str(uuid) 转换位置
  `grep -rn "INSERT INTO assets" --include=*.py`
- AssetDTO / AssetCreateDTO uuid 字段的消费点
  `grep -rn "AssetDTO\\|AssetCreateDTO" --include=*.py`
- source_id 默认 -1 的使用点
  `grep -rn "source_id.*-1\\|source_id=-1" --include=*.py`
- AssetCreateDTO.is_deleted 的 int 绑定/写入点
  `grep -rn "is_deleted" --include=*.py`
- service 以上层消费 DTO.id 的调用点（用于“对外用 uuid”契约测试）
  `grep -rn "\\.id\\b" --include=*.py albuswall/service albuswall/ui`
- from_row 的调用点与入参形态
  `grep -rn "from_row" --include=*.py`
- to_dict / as_dict 的调用点（确认无 json.dumps 依赖 UUID/Enum）
  `grep -rn "to_dict\\|as_dict" --include=*.py`
- TriggerConfig.to_dict / to_json / trigger_config 的写入与存储调用点
  `grep -rn "TriggerConfig\\|trigger_config\\|to_json()" --include=*.py`
- update_mode 的单值形态与列表形态残留（列表形态应为 0）
  `grep -rn "update_mode" --include=*.py`
- `active_modes` 归一函数残留（应为 0，统一走 `SCHEDULED_MODES` + 单值判断）
  `grep -rn "active_modes" --include=*.py`
- FileTypeCheckMode.NONE / _missing_ 的定义与使用点（`_missing_` 应为 0）
  `grep -rn "FileTypeCheckMode.NONE\\|_missing_" --include=*.py`
- 旧别名 `to_json = dump_json` 残留（应为 0）
  `grep -rn "dump_json = \\|to_json = " --include=*.py src`
- schema 常量是否仍从 dto 反向取（`ALL_SPECS` 等常量应为 0，DTO 类与 `ThumbSpec` 除外）
  `grep -rn "from albuswall.dto.thumbnail import" --include=*.py src/albuswall/repositories src/albuswall/services`
- COLUMNS 与 dataclasses.fields() 的消费点
  `grep -rn "COLUMNS\\|dataclasses.fields" --include=*.py`
- TypedDict 定义（确认改 dataclass 范围无遗漏）
  `grep -rn "TypedDict" --include=*.py albuswall/dto`
- BackpressureEvent 消费方对键存在性的判断
  `grep -rn "BackpressureEvent\\|\"seq\" in\\|'seq' in" --include=*.py`
-->

## ⚠️ 风险提示

[//]: # (- `AssetDTO.uuid`、`AssetCreateDTO.uuid`、`AssetCandidateCacheDTO.uuid` 等仍为 `str`，UUID 统一的迁移边界未被阶段 0 覆盖。)
[//]: # (修好了)

- `AssetCreateDTO.is_deleted` 改 `bool` 会波及现有 `int` 绑定/写入点，需 grep 确认。
- `TriggerConfig.update_mode` 已统一为**单值** `UpdateMode`（列表形态已删除），见
  `common/CONVENTIONS.md` · 5.2；新增 / 修改触发方式时只动单值判断，不要复活 `list` 形态或归一函数。
- `FileTypeCheckMode.NONE` 与 `IngestSource.is_void()` 已按 `common/CONVENTIONS.md` · 5.1 改判为**排除哨兵**；
  `OFF` / `MAGIC_LAX` 是 schema 值域内的合法成员（v0.1.3 新增），不得再被 `is_void()` 判为不可用。
- 删除 `_missing_` 后，任何 `EnumCls(未知值)` 直调都会抛 `ValueError`；读路径必须走 `coerce_enum`，不得依赖枚举自身降级。
- TypedDict 全改 dataclass 后，`BackpressureEvent` 消费方原先的键存在性判断需改为 `is UNSET`，调用点需排查。
