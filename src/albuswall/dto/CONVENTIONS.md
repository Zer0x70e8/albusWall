# DTO 契约约定

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

### from_row / to_dict Protocol

- 结论：`from_row` / `to_dict` 需要 Protocol 声明。
- 理由：统一读/写转换入口。
- 锚点：TODO（Protocol 未落地前不可执行）
- 影响文件：`dto/album.py`、`dto/import_.py`、`dto/source.py`、`dto/thumbnail.py`、`dto/trash.py`、`dto/trigger.py`

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

- 结论：读路径遇非法枚举值降级到显式默认；写路径抛 `ValueError`；`FileTypeCheckMode.NONE` 保留枚举值，只删 `_missing_` 静默映射。
- 理由：读侧不阻塞，写侧不吞错，去掉有害降级。
- 锚点：`tests/dto/test_contracts.py::test_enum_read_degrades_write_raises`
- 影响文件：`dto/source.py`、`dto/import_.py`、`dto/trigger.py`

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

## 二、待定

> 原 9 条待定中 8 条已在 review 中拍板并落入「已定」；此处只列仍待定的项，按影响面小 → 大排序。

| # | 问题                                                                                                                        | 候选方案                                                       | 影响文件                          | 影响面评估                                                                                          | 我的建议（标注“待你拍板”）                                        |
|---|-----------------------------------------------------------------------------------------------------------------------------|----------------------------------------------------------------|-----------------------------------|-----------------------------------------------------------------------------------------------------|-------------------------------------------------------------------|
| 1 | `FileTypeCheckMode.NONE` 保留后，`is_void()` 中 `file_type_check is FileTypeCheckMode.NONE` 分支是否随之调整？              | A. 保持现状；B. 改为显式默认值判断；C. 删除该分支              | `dto/source.py`                   | 小：仅 `IngestSource.is_void()` 一处判断                                                            | 建议随文件类型检查设计一起定，先不动；待你拍板                    |
| 2 | 文件类型检查的设计与使用方式未完成，连带 `trigger_config.update_mode` 的 `list[str]` / `str` 形态（两条写入路径目前不一致） | A. 统一 `str`；B. 统一 `list[str]`；C. 存储 `list`、表单 `str` | `dto/source.py`、`dto/trigger.py` | 大：跨 `IngestSourceFormData.to_trigger_config()` 与 `TriggerConfig.update_mode`，存储/表单两套形态 | 建议先完成文件类型检查许愿再定；当前只留 TODO，不改形态；待你拍板 |

## 三、例外

- `dto/sentinel.py`：DTO 层共享基础设施（`UNSET` / `_UnsetType` / `PatchField` / `MaybeUnset`），不映射表、不套表 DTO 契约；被
  `IngestSourceUpdate`、`TagUpdate`、`BackpressureEvent` 等依赖，必须集中维护、不得各域自建。
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
- update_mode 的字符串/列表写入路径
  `grep -rn "update_mode" --include=*.py`
- FileTypeCheckMode.NONE / _missing_ 的定义与使用点
  `grep -rn "FileTypeCheckMode.NONE\\|_missing_" --include=*.py`
- COLUMNS 与 dataclasses.fields() 的消费点
  `grep -rn "COLUMNS\\|dataclasses.fields" --include=*.py`
- TypedDict 定义（确认改 dataclass 范围无遗漏）
  `grep -rn "TypedDict" --include=*.py albuswall/dto`
- BackpressureEvent 消费方对键存在性的判断
  `grep -rn "BackpressureEvent\\|\"seq\" in\\|'seq' in" --include=*.py`
-->

## ⚠️ 风险提示

- `AssetDTO.uuid`、`AssetCreateDTO.uuid`、`AssetCandidateCacheDTO.uuid` 等仍为 `str`，UUID 统一的迁移边界未被阶段 0 覆盖。
- `AssetCreateDTO.is_deleted` 改 `bool` 会波及现有 `int` 绑定/写入点，需 grep 确认。
- `TriggerConfig.update_mode` 与 `IngestSourceFormData.to_trigger_config()` 仍两套形态，存储/表单桥未统一。
- `FileTypeCheckMode.NONE` 保留后，`IngestSource.is_void()` 的行为是否随文件类型检查设计调整尚未定。
- TypedDict 全改 dataclass 后，`BackpressureEvent` 消费方原先的键存在性判断需改为 `is UNSET`，调用点需排查。
