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

- 已拍板 **B：显式白名单**。`is_void()` 改为 `file_type_check not in (SUFFIX, MAGIC)` → void。
- 理由：删 `_missing_` 后读路径（`coerce_enum` 默认 `SUFFIX`）拿不到 `NONE`，schema 的
  `CHECK (file_type_check IN ('suffix','magic'))` 也不允许 `NONE` 落库；白名单与 `is NONE` 行为等价，
  但自解释，且对未来新增成员默认收紧。
- 仍未决：若将来 `NONE` 代表「关闭文件类型检查」（合法意图），再决定是否让它进入 schema 值域，
  并同步调整 `is_void()` 语义与读降级口径。

### 5.2 `trigger_config.update_mode` 的 `str` / `list[str]` 形态

- 状态：**本阶段不改形态，只留 TODO**。
- 现状（两形态分工，非两套写入路径）：
    - **存储形态**：`IngestSourceFormData.to_trigger_config()` 恒写**单值 `str`**，是唯一存储出口；
    - **运行时形态**：`TriggerConfig.update_mode: list[UpdateMode]`，`from_dict` 把 `str` 包成单元素
      列表，`SchedulerService.active_modes()` 再归一成 frozenset，兼容历史多值 JSON。
- 遗留隐患：`TriggerConfig.__post_init__` 未归一化 `str`，`to_dict()` 对 `str` 用 `list()` 会拆成字符；
  统一形态时一并处理。
- 未决选项：A 统一 `str` / B 统一 `list[str]` / C 维持两形态并文档化。
- 决定时机：等「文件类型检查」设计落地、确认是否需要表达「多模式并存」后再定。
- 代码锚点：`dto/source.py` `IngestSourceFormData.to_trigger_config`、`dto/trigger.py`
  `TriggerConfig.update_mode`（均已留 TODO）。

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
