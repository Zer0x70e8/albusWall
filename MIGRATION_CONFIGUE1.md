# albuswall.configue → albuswall.configue1 升级指南

> 面向：本项目的使用者（改 `config.ini` 的人）与未来的维护者（读/改配置层的人）。
> 状态（历史）：本次迁移把旧包拆成新包 `albuswall.configue1`；迁移完成后旧包已删除
> （commit `f664c69`，删除前最后一个仍含旧包的提交是
> `972f6b867ff3f9b748785870200526afe8843a69`，下文简称 `972f6b8`）。
> 状态（当前）：紧随其后的一轮已把 `configue1` **改名回 `configue`**，仓库里现在只有
> 一个配置包 `albuswall.configue`（即本文所说的「新包」），`import albuswall.configue1`
> 会抛 `ModuleNotFoundError`。**下文凡写 `configue1` 的地方都是历史叙述**，阅读时理解为
> 「现在的 `albuswall.configue`」即可。

---

## 1. 一句话总结

`configue`（旧包）把「声明 / 加载 / 访问」混在一个静态单例里；`configue1`（新包）
把它们拆成 `registry`（声明）、`resolver`（值）、`backends`（格式）、`configue`
（生命周期）、`tree`（视图），并新增 `optional` 文件、`portable` 目录布局、
加载钩子、可插拔后端。

对**用户**来说只有三件事变了（第 4 节逐条给出）：

1. `[log]` 段的键**真的生效了**（以前是死键）；
2. 没有 `config.ini` 时不再报错，用默认值启动、第一次 `save()` 时创建文件；
3. `[path]` 四段的默认值仍指向用户目录，但可以通过 `portable=True` 全部收进
   一个目录。

---

## 2. API 映射表

| 旧 API | 新写法 | 备注 |
|---|---|---|
| `setup_static_config(container, path)` | `setup_config(container, base_dir=..., file_name=...)` | 见 5.1；装配代码在 `albuswall/configue1/bootstrap.py` |
| `setup_dynamic_config(container)` | **删除** | 全仓库无人使用动态子树 |
| `Configue()`（无参单例） | `Configue(base_dir)` | 单例语义保留：同 `base_dir` 幂等，不同则 `RuntimeError` |
| `get_config().resolver` | `c.resolver` | `c` = `container.require("configue")` 或 `setup_config` 返回值 |
| `config.static.<sec>.<key>` | `c.tree.<file>.<sec>.<key>`（本项目只有 `file="main"`） | 树视图**不做类型校验** |
| `config.static.<sec>.ensure(key, T)` | 直接读（类型由 `@configclass` 声明承担） | `ensure()` 无对应物 |
| `ConfigField("sec", "key", default=v)` | `@configclass("sec", file)` + 类属性 `key: T = v` | 声明方式变化，**不是**机械替换 |
| `ConfigField("sec", default={})`（段根） | **删除** | 新包只支持 2 层 path；空段在首次写入时产生 |
| `PathField(...)` | `@configclass` + `field(default=Path(...))` | |
| `config.set/remove(...)` | `c.resolver.set/remove(...)`，或 `c.tree.<file>.<sec>.<key> = v` | |
| `config.flush()` / `config.save()` | `c.flush()` / `c.tree.<file>.save()` | |
| `configure_persistence(autosave=..., default_write_file=...)` | `reg(..., autosave=...)`（**文件级**） | 全局开关已删除 |
| `register_load_hook(h)` | 同名，签名不变 | 异常改为收集成 `LoadError(kind="hook_failed")`，不再炸启动 |
| `register_converter(t, reader, writer=)` | 同名 | 构造前登记会入队，`load()` 时应用 |
| `load_typed(TypedDict, ...)` | **删除** | 全仓库无人使用 |
| `@dynamic_state` / `StatefulNamespace` / `StateField` / `Observable*` | **删除** | 全仓库无人使用；状态文件可用 `ConfigFile.JSON` + `@configclass` 表达 |
| `StaticConfig` / `DynamicConfig` | **删除** | 由 `Config()` 视图 + `c.tree` 取代 |
| `SETTING_SCHEMA` / `SettingGroup` / `SettingItem` | **删除** | 全仓库无人使用 |
| `ConfigDeclaration` | **删除** | 内建段改为 `@configclass` 声明（见 5.2） |
| `get_user_config_dir` 等（`configue.utils`） | `configue1.utils.platform_dir` 同名函数 | 实现逐字一致 |
| `[configue] include` 多文件合并 | **未移植** | 业务代码未使用；需要时再补 |

容器别名保持不变：`container.require("config")` 与 `container.require("configue")`
都指向同一个 `Configue` 实例（由 `setup_config` 注册）。

---

## 3. 停用 API 清单（C 决策：不迁移、随后续删除）

按评审结论，以下 API **不提供替代实现**，因为迁移前全仓库（除旧包自身）零使用：

- 动态配置子树：`@dynamic_state`、`StatefulNamespace`、`StateField`、
  `ObservableNamespace`、`ObservableContext`、`StateContext`、
  `register_dynamic_state`、`register_dynamic_module`、`DynamicMountSpec`
- TypedDict 视图：`load_typed`
- 配置结构登记：`SETTING_SCHEMA`、`SettingGroup`、`SettingItem`、`SettingSchemaRegistry`、
  `setting_group`、`setting_item`、`register_setting_group`、`register_setting_item`、
  `dump_setting_schema*`、`save_setting_group/schema`
- 静态视图：`StaticConfig`、`DynamicConfig`、`ConfigDeclaration`、`ConfigMeta`、
  `get_config`、`PathField`、段根 `ConfigField(section)`
- 启动函数：`setup_static_config`、`setup_dynamic_config`、`setup_config`（旧包里的
  兼容别名）

对应的新写法见第 2 节。

---

## 4. 行为修正清单（用户可见）

### 4.1 日志配置键（死键修正）

旧 `LogConf` 读的是：

| 旧代码读的键 | 随包 `conf.ini` 里实际写的键 |
|---|---|
| `[logger] level` | `[log] level = WARNING` |
| `[files] logger` | `[files] log = log_config.ini` |
| `[logger] _config` | `[log] config = ${path:config}/${files:log}` |

也就是说，发布出去的 `conf.ini` 里 `[log]` 段**从来没被读过**。迁移后改为读
`[log] config` / `[log] level` / `[files] log`。

**可见变化**：`[log] level = WARNING` 现在真的生效——`albuswall` logger 的级别会
从「NOTSET（继承 root，`main.py` 设的 TRACE）」变成 `WARNING`。想要旧的啰嗦日志：

```ini
[log]
; level = WARNING     ← 注释掉即「不改级别」
```

### 4.2 缺 `config.ini` 的行为

| 场景 | 旧包 | 新包默认 | 新包在 `main.py` 中 |
|---|---|---|---|
| 首次启动没有 `config.ini` | 容错为空 → 全默认值 | **抛 `ConfigError`** | `ignore_missing=True` → 默认值启动 |
| 用户改配置后保存 | 写回主文件 | 写回 `base_dir/config.ini` | 同左；**首次 `save()` 创建文件** |

新包用 `reg(..., optional=True)`（`setup_config(..., ignore_missing=True)` 透传）表达
「文件可以不存在」：

- 缺文件时用 `base_dir / file_name` 作为占位路径把文件标成「已加载」，默认值照常可读；
- 占位路径**不走后端候选规则**（不会补 `.ini` / `.conf.ini`）：语义是「假设文件将出现在
  声明的位置」。若之后文件以候选路径出现，`load()` 优先读候选，此前写进占位路径的内容
  不会被自动合并；
- **歧义（多候选同时存在）仍然报错**，`optional` 不豁免；
- 文件存在但内容坏（INI 语法错、JSON 坏）仍然 `read_failed`，不豁免；
- `reg(exists_check=True, optional=True)` 语义冲突 → `ValueError`。

### 4.3 `[path]` 四段与 `portable`

`[path]` 默认值由 `setup_config` 决定（不是写死在 `conf.ini` 里的）：

| 参数 | `path.config` | `path.data` / `path.cache` / `path.temp` |
|---|---|---|
| `portable=False`（默认） | `get_user_config_dir()` | `get_user_data_dir()` / `get_cache_dir()` / `get_temp_dir()` |
| `portable=True` | `base_dir` | `base_dir/data`、`base_dir/cache`、`base_dir/temp`（**不预先创建**，首次写入时按需创建） |

要点：

- `portable=True` **必须**同时给 `base_dir`，否则 `ValueError`（否则四段会静默退化成用户
  目录，与 `portable=False` 无异）；
- `portable` **只改 `[path]` 四段**，不改 `config.ini` 的落点（仍是 `base_dir/file_name`）；
- 生产启动（`main.py`）用的是 `portable=False`，所以 `config.ini` 在
  `~/.config/albuswall/`，与 `path.config` **同一目录**；
- 用户仍可在 `conf.ini` 里显式覆盖任意一段，文件值优先于默认值。

### 4.4 配置文件的写回落点

- 有 origin 的节点写回它读到的那个文件；
- 只有默认值的字段（文件里没写）→ 写回**声明它的那个文件**（本项目 = `main`）；
- 不再有「全局默认写入文件」概念（旧包 `configure_persistence(default_write_file=...)`）；
- 回写策略是文件级的 `autosave`：`off` / `manual` / `atexit` / `debounce` / `now`。
  本项目主文件用 `"atexit"`。

### 4.5 `[log] config` 不再是路径字面量

`[log] config = ${path:config}/${files:log}` 会被 `${...}` 展开（然后按 `str` 读取）：

- 默认情形解析到 `~/.config/albuswall/log_config.ini`，与旧包的兜底路径**相同**；
- 因此改 `[files] log` 或 `[path] config` 都能改变日志配置文件的位置。

---

## 5. 装配方式（维护者视角）

> 本节代码片段里的 `albuswall.configue1` 是历史名；当前可直接照抄，只需把
> `configue1` 换成 `configue`。

### 5.1 `setup_config`

```python
from albuswall.configue1.bootstrap import setup_config

config = setup_config(
    container,                 # 依赖容器
    base_dir=CONFIG_FILE_PATH, # 配置文件目录；None → get_user_config_dir()
    file_name="config.ini",    # 注册名默认 "main"
    name="main",
    autosave="atexit",         # 文件级回写策略
    order=10,
    register_aliases=True,     # 注册 container 的 "config" / "configue"
    ignore_missing=True,       # 缺文件不算错（main.py 就是这么用的）
    portable=False,            # True → [path] 四段收进 base_dir
)
```

`setup_config` 的职责：`reg()` 配置文件 → 声明 `[path]` / `[files]` 默认值 →
`Configue(base_dir).load()` → 注册容器别名。它替代旧包的 `setup_static_config`。

### 5.2 声明一个配置段

```python
from albuswall.configue1 import configclass, field

@configclass("task", "main")     # (section, file)
class TaskConfig:
    initial_max_concurrency: int = 10
    max_concurrency: int | None = None

TaskConfig().max_concurrency      # 读：无值且无默认 → ConfigMissingError
TaskConfig().max_concurrency = 4  # 写：严格 isinstance；按文件 autosave 策略落盘
```

- 字段必须在 `@configclass` 里声明；没声明的 key 只能走树视图
  （`config.tree.main.task.new_key = 1`，不校验类型）；
- `Config()` 的 `repr` 会列出全部字段（`<missing>` 表示没有值）；
- `int | None` 这类 PEP 604 联合类型在旧包 Python 3.10–3.13 上是坏的，本次一并修复。

---

## 6. 旧包状态：已移除（包名后来被复用）

- 旧实现 `src/albuswall/configue/` 已随 commit `f664c69` 删除；旧包唯一的回归防线
  `tests/test_configue_legacy_smoke.py` 一并删除。
- **旧实现有 tag 可追**：annotated tag `configue-legacy-final` → `972f6b8`，即旧包
  删除前的最后一个完整状态（`src/albuswall/configue/` 共 18 个文件：顶层 10 + `utils/` 8）。
  查旧代码：

  ```bash
  git ls-tree -r configue-legacy-final --name-only   # 18 个文件的清单
  git show configue-legacy-final:src/albuswall/configue/configue.py
  ```

- **`origin/archive/old-implementation` 不是旧包最后状态，别拿它当参照**：该分支
  （`ce8febc`）的 `src/albuswall/configue/` 只有 12 个文件，缺
  `dynamic_declaration.py`、`resolver.py`、`setting_schema.py`、`state.py`、`typed.py`、
  `utils/annotation.py` 这 6 个，是早期的 scaffold。tag 之所以要补，就是因为当初
  「archive 分支兜底了旧包」这个前提不成立。
- 冗余锚点：即使 tag 被误删，删除前最后一个包含旧包的 commit 仍是
  `972f6b867ff3f9b748785870200526afe8843a69`，`git show 972f6b8:src/albuswall/configue/...`
  照旧可用。
- **包名已被复用，验证方式要跟着反过来**：紧随其后的一轮把 `src/albuswall/configue1/`
  改名回 `src/albuswall/configue/`，所以 `import albuswall.configue` 现在导入的是**新实现**
  （成功是正常的），要确认旧包确实不在，断言应改为
  `python -c "import albuswall.configue1"` → `ModuleNotFoundError`。
- 迁移期间旧包 `utils/ini_parser.py::_convert` 修过 3 个 bug，这些修复只对旧包有意义，
  随包一并消失（新包的转换实现是独立重写的）：
  1. 容器分支（`dict/list/tuple/set`）被缩进在 Union 分支里 → 非 Union 泛型永远走不到
     （`list[str]` 退化成逐字符）；
  2. `origin is Union` 不认 `types.UnionType` → Python 3.10–3.13 上 `int | None` 抛
     `TypeError`（3.14 里两者同一对象，测不出来）；
  3. `dict[str, Path]` 是第 1 条的连带受害者。

---

## 7. 验证清单

```bash
python -m pytest -q                     # 全量
python -m pytest tests/test_configue -q # 配置包
```

旧包已删除，按顺序做三项强断言（顺序有意义：第 1 项没做，第 2 项会假绿）：

1. **清字节码残留。** 残留的 `__pycache__` 会让一个只剩空目录的包路径变成 PEP 420
   namespace package，`import` 会**静默成功**而非报错。验证前先执行
   `find src/albuswall tests -name '__pycache__' -type d -prune -exec rm -rf {} +`
   （或 `git clean -fdx src/`），并确认 `find src/albuswall -name configue1 -type d`
   无输出，再跑下面第 2 项。
2. **旧包的名字不可导入，且异常类型必须是 `ModuleNotFoundError`。**
   `python -c "import albuswall.configue1"`
   → 期望 `ModuleNotFoundError: No module named 'albuswall.configue1'`。
   （注意不是 `import albuswall.configue`：那个名字现在属于改名后的新包。）
3. **业务链路能 bootstrap 起来。**
   `QT_QPA_PLATFORM=offscreen python -m albuswall`（无头；把 `XDG_CONFIG_HOME` /
   `XDG_DATA_HOME` / `XDG_CACHE_HOME` / `XDG_STATE_HOME` / `XDG_RUNTIME_DIR` 指到可写
   位置，例如临时目录）→ 期望日志中出现 `Program boot registry finished.` 与
   `Program bootstrap completed.`。该进程是常驻主循环，确认这两行后 Ctrl-C 即可。

关键测试文件：

| 文件 | 覆盖点 |
|---|---|
| `tests/test_configue/test_configue.py` | 单例 / check / load / unload / flush / close / autosave 策略 |
| `tests/test_configue/test_optional_files.py` | `optional` / `ignore_missing` / 不隐藏 read 错误 |
| `tests/test_configue/test_app_bootstrap.py` | `setup_config`、`portable` 布局、log/task/db 读取 |
| `tests/test_configue/test_load_hooks.py` | 加载钩子、hook 失败语义 |
| `tests/test_configue/test_backends_*.py` | INI / JSON 后端 |
| `tests/test_configue/test_mixed_backends.py` | 两种格式混用、跨格式覆盖 |
| `tests/test_configue/test_registry.py`、`test_slots.py`、`test_registry` 等 | 声明层 / 描述符 / repr |

---

## 8. 已知限制与未做项

- **TOML / YAML 后端**：`Backend` 协议已就位，但没有实现；`reg(ConfigFile.TOML/...)`
  会在 `load()` 时报 `read_failed`（no backend registered）。
- **`[configue] include` 多文件合并**：未移植。
- **N 层 path**：只支持 `section.key` 两层；`("ui", "theme", "color")` 不支持。
- **`path.theme` 当前无消费者**：随包 `conf.ini` 仍保留 `theme = ${path:config}/theme`；
  读它的 UI 代码（`ui/bootstrap.py` 等）在本轮重构中被移除，新包只保证该值能读出来。
- **`[logger]` 旧键不再被读取**：`[logger] level`、`[files] logger`、`[logger] _config`
  已废弃；`_config` 这个下划线 key 在新模型里既不能声明（`@configclass` 跳过 `_` 开头）
  也不能经树视图访问（`_FileView` / `_SectionView` 屏蔽 `_` 开头）。
- **树视图共享 Resolver**：`tree.main.x = v` 改的是同一份值、只把「归属文件」改成
  `main`；`tree.machine.x` 读到的还是 `v`。视图隔离的是写回落点，不是值。
- **`unload()` 只回退内存**：写过盘之后再 `unload()`，磁盘上的值不会回滚。
- **`declared_keys()` / `as_typed_dict()`**：尚未提供。
- **`configue/utils/ini_parser.py` 硬编码 `albuswall.configue` 作 logger 名**（这是
  复制旧代码带来的，本轮改名时刻意没动），故 `resources/config/log_config.ini` 的
  `[logger_albuswall.configue]`（以及 `keys=`、`qualname=` 行）**是活配置，不是死配置**。
  实测证据（改名后）：
  - `configue/utils/ini_parser.py` 的 `logger.name == "albuswall.configue"`
    —— 与 ini 里的 `qualname` 精确匹配；
  - `configue/configue.py` / `configue/registry.py` 走 `logging.getLogger(__name__)`，
    名字随改名为 `albuswall.configue.configue` / `albuswall.configue.registry`，
    作为 `albuswall.configue` 的子 logger 传播到 ini 配置的那套 handler（全量生效）。
  - 注意 ini 里 `[logger_albuswall.configue]` 的 `handlers=` 是**空的**，这是原设计：
    该条目只负责给这一族 logger 定级别和 `qualname`，handler 挂在 `[logger_root]`
    （即 `logging.getLogger("")`）上，靠 propagate 落盘/打屏。所以「
    `logging.getLogger("albuswall.configue").handlers` 非空」**不是**这条配置的正确
    断言，正确的断言是：这条 logger 的 `level` 被 ini 设上了，且子 logger 的日志能
    从 ini 的 handler 出来。
  **不要因为「看起来像旧包遗留」就删掉 `log_config.ini` 里的这一段。**
- **首次启动不复制默认 `conf.ini`**：README 里「defaults copied to ~/.config」这句在代码
  中没有对应实现；当前行为是「没有文件就按默认值跑，首次保存时才创建」。
