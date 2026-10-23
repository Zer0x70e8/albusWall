# 跨层契约约定（common）

> 平级文档：`dto/CONVENTIONS.md`（DTO 层）。
> 本文件只写**跨层规则正文**；DTO 层文档用指针引用本文件，不复述正文。
> 维护规则见文末「六、文档维护」。

## 一、枚举值域与容错

### 位置

- 按域分文件：`common/enums/<domain>.py`，`__init__.py` 汇总导出；DTO / repo / service / UI 一律从
  `albuswall.common.enums` 取，不在各层声明平行枚举。
- 枚举只做**纯值域定义**：只声明成员与值，不写容错、不做 IO、不依赖其他层。
- **DTO 内不得定义枚举**：历史 `dto/task.py` 的 `ExecutorType` / `BackpressureReason` / `PermitKind`
  已迁入 `common/enums/task.py`；DTO 只保留入参类型别名（如 `ExecutorArg`）。
- 现状：`import_.py`（`FileTypeCheckMode`）、`source.py`（`CandidateStatus`）、`task.py`
  （`ExecutorType` / `BackpressureReason` / `PermitKind`）、`thumbnail.py`（`ThumbSpec`）、
  `trigger.py`（`UpdateMode`）。

### 禁止 `_missing_`

- 不得定义 `_missing_`（或任何把非法值静默映射成合法成员的逻辑）。
- 理由：静默降级与 DTO 边界 `coerce_enum` 重复，且会让写路径永远拿不到 `ValueError`。
- 现状：`FileTypeCheckMode._missing_` / `UpdateMode._missing_` 已删除；`CandidateStatus` 本就没有。

### 读写容错分工

- **读路径**：只在 DTO 边界用 `dto.utils.coerce_enum(value, EnumCls, default)` 降级到**显式默认**，
  单条脏数据不阻塞读取。
- **写路径**：直接 `EnumCls(value)`，非法值抛 `ValueError`，不吞错。
- 除 DTO 边界外，其他层不做枚举容错。
- `FileTypeCheckMode.NONE` 等合法成员一律保留，只删 `_missing_` 静默映射。

### 值域与持久化

- 枚举值域应与 schema 约束一致；不在 schema 值域内的成员（如 `FileTypeCheckMode.NONE`）只是
  Python 侧哨兵，不得靠它落库。

### 值域变更与老库升级（`file_type_check` 先例）

SQLite 的 `CHECK` 约束**无法 ALTER**，所以"加一个枚举值"必然同时动三处：枚举成员、
`is_void()` 白名单口径、schema 白名单。以 `ingest_source.chk_file_type_check` 从
`('suffix','magic')` 扩到 `('suffix','magic','off','magic_lax')` 为例：

- **存储层变 / 不变**：只改 `chk_file_type_check` 白名单。`asset_candidate_cache` **不**加
  `type_check` / `skip_reason` 列（`candidate.source_id` 已能反查源配置，`type_check` 由
  `ImportService` 每轮内存快照带给进程池）；`assets` / `ingest_source` 其他列、索引、触发器、
  FK 全不动；**不迁移任何数据**（历史 `file_types` 的 `"jpg"` / `".jpg"` / `"*.jpg"` 与脏路径
  都只在读取路径归一）。
- **老库红线**：新库直接生效；老库沿用 `suffix` / `magic` 完全兼容，但写入 `off` /
  `magic_lax` 会被旧 `CHECK` 拒绝（`IntegrityError`）。升级前必须明确选择**删库重建**，或
  **接受新值在该库不可用**。当前仓库没有迁移框架（`infrastructure/database/connector.py`
  只有 `SCHEMA_VERSION` 注释提到 `_run_migrations`，函数并不存在），因此不提供 migration
  文件；真要做时按 SQLite 12-step 重建表并同步版本判断。
- **哨兵不放行**：`FileTypeCheckMode.NONE` 仍只是 Python 侧哨兵；`is_void()` 用"排除哨兵"
  （`is NONE`）而不是白名单，保证新增成员默认放行（见 5.1）。

### 候选缓存批量插入的返回值口径

`ImportRepository.create_candidate_cache_batch` 返回的是**提交尝试数**（源存活过滤后的
`kept`），不是 `COUNT(*)` 增量：SQL 是 `INSERT OR IGNORE`，撞 `(source_id, path)` 唯一索引的
重复行、以及违约列约束的行（典型：`mime_type=None` 撞 `mime_type TEXT NOT NULL`）都会被
**静默丢弃**。

- "调用了但表里没多出行"要先查 `OR IGNORE` 吞行，**不要**误判为事务未提交——
  `BaseRepository._transaction` 是标准 `@contextmanager`，块内 `return` 会走正常路径 commit。
- `AssetCandidateCacheDTO.mime_type` 声明为 `str`，传 `None` 属调用方违约（会被静默吞掉）。

## 二、异常

### 位置

- 跨层业务异常定义在 `common/exceptions.py`。
- `dto/*.py` 不得定义异常类；DTO 只允许 `raise ValueError` 等标准异常表达「输入非法」。

### 层次

- `AlbusError` 基类按 **YAGNI 暂不建**：出现第二个业务异常、且确需统一 catch 时再建，避免空基类。
- 现有：`BackpressureError(RuntimeError)`。

## 三、常量

### 按域分文件

- 跨层常量放 `common/constants/<domain>.py`。
- **禁止**集中式 `common/constants.py` 垃圾桶。
- `common/constants/__init__.py` **不做汇总 re-export**（同「不建垃圾桶」的意图），消费方显式 import 域模块。

### 归属判据

- **schema 契约**（列名、spec 映射、表/列清单）→ `common`；
- 单模块私有配置 / 默认值 → 留在原模块；
- 只被单域显式 import 的域常量（如 `dto/source.py` 的 `MANUAL_SOURCE_ID`）→ 留在原处，
  被 repo/service 显式 import 属正常依赖，不构成倒挂。

### 现状

- `common/constants/thumbnail.py`：`ALL_SPECS` / `SPEC_TO_COLUMN` / `COLUMN_TO_SPEC` /
  `BASE_COLUMN` / `ALL_THUMB_COLUMNS`。
- 依赖方向：repo / service / 迁移脚本 → `common`；**禁止**反向 import `albuswall.dto` 取这些常量。

## 四、并发工具边界

- `common/futures.py` 只放**跨域复用**的并发原语（当前 `wait_with_timeout`）。
- 单域使用的辅助函数不得上提到 common：`_safe_call` / `_normalize_trigger_dict` 目前只服务
  source 域，留在 `dto/source.py`。
- 任务提交、背压、线程池策略归 `infrastructure.task`；`common` 不依赖 `dto` / `infrastructure` /
  `services`。

## 五、待定（单一正文）

### 5.1 `FileTypeCheckMode.NONE` 与 `IngestSource.is_void()`

- 现状（**v0.1.3 改判**）：`is_void()` 改为 **排除哨兵** `if self.file_type_check is FileTypeCheckMode.NONE: return True`。
- 早期方案 B（显式白名单 `not in (SUFFIX, MAGIC)`）在新增 `OFF` / `MAGIC_LAX` 时会把合法配置判成 void，
  因此改为排除哨兵：新增成员默认放行，只有 `NONE` 视为"配置不完整"。
- 理由：删 `_missing_` 后读路径（`coerce_enum` 默认 `SUFFIX`）拿不到 `NONE`，schema 的
  `CHECK (file_type_check IN ('suffix','magic','off','magic_lax'))` 也不允许 `NONE` 落库。
- 已落地：`OFF = "off"`（不校验，mime 用 guess，表达"归档目录全都要"）与
  `MAGIC_LAX = "magic_lax"`（读魔数识别不出 → 接受并降级 `application/octet-stream`）。
- 仍未决：`OFF` 与 `SUFFIX` 当前行为一致，将来给 `SUFFIX` 加更严格校验时再区分二者。

### 5.2 `trigger_config.update_mode` 的形态

- 状态：**已拍板（原「待定」撤销）——统一为单值枚举 `UpdateMode`**。
- 存储形态：`trigger_config` JSON 里的 `update_mode` 恒为**单值字符串**（`"manual"` / `"scheduled_time"` /
  `"interval_time"` / `"device_trigger"`），与 schema 注释一致；唯一写出口仍是
  `IngestSourceFormData.to_trigger_config()`。
- 运行时形态：`TriggerConfig.update_mode: UpdateMode`（单值），与存储一一对应，不再有 `list[UpdateMode]`。
  `from_dict` / `__post_init__` 经 `coerce_enum` 归一（`str` → 枚举，未知 / NULL → `MANUAL`）。
- 已删除的旧形态（不留过渡码）：`list[UpdateMode]` 字段、`SchedulerService.active_modes()` /
  `_active_modes()` 归一函数、`from_dict` 把 `str` 包成列表、`_apply_trigger_config_to_form` 对
  `list` / `tuple` 的兼容分支。历史多值 JSON 不再被理解，值域外的值一律由 `coerce_enum` 降级为 `MANUAL`。
- 判断口径：调度 / 设备触发一律对单值直接判断 —— `config.update_mode in SCHEDULED_MODES`
  （`SCHEDULED_MODES = {SCHEDULED_TIME, INTERVAL_TIME}`，定义在 `infrastructure/trigger/scheduler.py`）、
  `config.update_mode is UpdateMode.DEVICE_TRIGGER`。语义上「多模式并存」不再支持，唯一触发方式由
  `IngestSourceFormData.to_trigger_config()` 的优先级规则（device_trigger 优先）决定。
- 代码锚点：`dto/trigger.py` `TriggerConfig`、`dto/source.py` `IngestSourceFormData.to_trigger_config` /
  `_apply_trigger_config_to_form`、`infrastructure/trigger/scheduler.py` `SCHEDULED_MODES`、
  `infrastructure/trigger/trigger.py` 的两个 `_filter_*_configs`。

## 六、文档维护

- **每条规则只有一个正文**：正文在 `common` 的规则，`dto/CONVENTIONS.md` 只写
  `见 common/CONVENTIONS.md · 条目名` 指针。
- **`common` 变更优先**：先改本文件，再改 `dto/CONVENTIONS.md` 的指针。
- 两份文档**平级交叉引用**，不搞父子层级；**不给 service / repo / ui 建 CONVENTIONS.md**。
- 指针写法统一为：`见 common/CONVENTIONS.md · 条目名`（条目名用二级/三级标题原文）。

<!-- grep-checklist
- 枚举值域里出现 _missing_（应为 0）
  `grep -rn "_missing_" --include=*.py src/albuswall`
- DTO 内定义异常类（应为 0）
  `grep -rn "class .*Exception\\|class .*Error" --include=*.py src/albuswall/dto`
- DTO 内定义枚举（应为 0）
  `grep -rn "class .*Enum" --include=*.py src/albuswall/dto`
- repo/service 反向从 dto 取 schema 常量（应为 0）
  `grep -rn "from albuswall.dto.thumbnail import" --include=*.py src/albuswall/repositories src/albuswall/services`
- 集中式常量垃圾桶（应不存在）
  `ls src/albuswall/common/constants.py`
- 枚举构造点（`EnumCls(` 直调，非法值应抛 ValueError）
  `grep -rn "FileTypeCheckMode(\\|UpdateMode(" --include=*.py src`
-->
