# PR: 实现 albuswall 插件系统（`core/plugins.py` + 发现层复用 + 文档）

> 本文件是本次改动的 **PR 描述**。实现范围严格限定在规格给定的决策
> D1–D10，不引入新的设计选择；规格未覆盖的细节集中在文末「待确认」。
>
> 分支：`wip/configue1-refactor`（基于 `39652d2`）

## 改动概览

| 类型 | 文件 |
| --- | --- |
| 新增 | `src/albuswall/core/plugins.py` |
| 新增 | `src/albuswall/plugins/__init__.py`（第一方插件目录占位，见「待确认 1」） |
| 恢复 | `src/albuswall/plugin/__init__.py`、`src/albuswall/plugin/discovery.py`（本分支 `55b1b17` 删除，从历史恢复并微调） |
| 新增 | `tests/test_plugins.py`（50 例）、`tests/test_container.py`（11 例）、`tests/test_application.py`（13 例） |
| 新增 | `docs/plugin-author-guide.md`（12 节） |
| 修改 | `src/albuswall/core/{container,registry,application,types,runtime,__init__}.py`、`src/albuswall/log/bootstrap.py`、`src/albuswall/main.py`、`tests/conftest.py` |

验证：`pytest tests/` **388 passed**（基线 314 + 新增 74）；`pyright` 对本次
修改/新增文件**零新增错误**（`core/plugins.py` 仅 2 条既有风格类的
`reportPrivateUsage`，见「待确认 3」）；`ruff check`（除仓库既有的中文标点
RUF002/003 与既有 UP009/I001）对新文件全绿。

---

## 决策落地对照表（D1–D10）

| 决策 | 落地位置 | 说明 |
| --- | --- | --- |
| **D1** UI 子插件独立 | `core/plugins.py` 全文 | 不 import `plugin/declaration.py` / `plugin/manager.py`，也没有任何 `ui_loader` / `activate_ui` 痕迹。这两个模块在本分支不存在（`55b1b17` 已删除），本次**不恢复**。 |
| **D2** 最小 API、不设围墙 | `PluginContext`（`provide` / `on` / `register` / `require` / `get` / `log`）、`_DecoratedPlugin.setup` | `container` / `app` 是真实对象；`provide` / `on` / `register` 只记录以便回滚。工厂签名统一 `Callable[[Container], Any]`：`_DecoratedPlugin.setup` 直调 `self._factory(ctx.container)`，**不**走 `_wants_container`。无 ServiceProxy。 |
| **D3** `@plugin` 三形态 + pending | `plugin()`、`_pending`、`_drain_pending()`、`PluginManager.load_pending()` | 装饰返回原函数（保持可调用），把 `_DecoratedPlugin` append 到 `_pending`；`load_pending` 取快照后清空。三种返回值在 `_resolve_lifecycle()` 归一。 |
| **D4** 实例级、三阶段、fail-fast | `PluginManager.__init__` / `setup_all` / `start_all` / `stop_all` / `_rollback_setup` / `_safe_stop` | 字段全部 `self.*`；setup 失败 → 撤销本插件注册 + 逆序 `_safe_stop` + `raise`；start 失败 → `_started.append` 在 `plugin.start()` **之前**，异常传播；stop 失败 → 吞 + `logger.exception` + 继续。 |
| **D5** 过滤在 setup 时 | `setup_all(enabled, disabled)` + `_resolve_allow()` | `None`=全开；白名单未知名 `raise KeyError`；黑名单未知名 `logger.debug`；二者互斥 `raise ValueError`；`set(dict)` 自动取 keys。 |
| **D6** 配置加载时机 / 条件调用 | `main._boot`、`CoreApplication.exec` | `_boot` 顺序：`register("app")` → `setup_config` → `discover_all` + `load_pending` + `setup_all(disabled=…)` → `setup_log` → `gc` → `registry_*` → `register_service` → `on_init`/`on_exit`。`exec` 里 `if not self.plugins.setup_done: setup_all()` / `if not self.plugins.started: start_all()`。 |
| **D7** 发现层默认关用户目录 | `plugin/discovery.py:discover_all(builtin_package, user_dir=None)`、`main._boot` | `user_dir` 默认 `None` 直接跳过；`main` 传 `user_dir=None`（注释标明将来从配置读）。 |
| **D8** 容器 last-wins | `Container._put` / `Container.set` | 锁内首行 `if name in self._factories: logger.debug("overriding dependency %r", name)`；不加 `override` 参数；`has_instance` 补锁。 |
| **D9** hooks 可撤销 | `registry.unregister()`、`PluginContext.on/register/_rollback` | `on`/`register` 记录 `(event, hook)`，回滚时逐个 `unregister`。`__all__` 已加 `"unregister"`。 |
| **D10** 不做静态解析 | `setup_all` 内 `time.perf_counter()` 计时 + `PluginManager.setup_slow_threshold_ms`（默认 `200.0`，实例属性） | 超阈值 `logger.warning`（含耗时与阈值），否则 `logger.debug`。无任何静态急切加载分析。 |

### 其余交付项

| 规格条目 | 落地 |
| --- | --- |
| `types.py` `ErrorHook` docstring | 改为 `hooks registered after the first that returns True will not be invoked` |
| `runtime.py` `_thread_excepthook` 走 `on_error` | 先 `run_exception_hooks(hooks_map["on_error"], ...)`，未处理才 `logger.critical(format_exception(...))`；原有日志行保留，`_request_quit(ExitCode.ERROR)` 位置不变 |
| `log/bootstrap.py` | `setup_log` 末尾 `container.set("logger", logging.getLogger(albuswall.__name__))` |
| `core/__init__.py` | 导出 `Plugin`/`BasePlugin`/`plugin`/`PluginContext`/`PluginManager` + 4 个异常（`unregister` 一并导出） |
| `tests/conftest.py` | 追加 `autouse` fixture `_reset_global_plugin_state`（hooks 原地切片恢复、`_pending[:]` 恢复、`Application`/`CoreApplication`/`Configue` 单例字段复位），**不动**原有 fixture |
| `main.py` | 删掉旧的注释块，按 D6 重排；新增 `_plugin_disables(config)` 读 `[plugin_disables]` |

### 新增测试覆盖（对照规格清单）

`tests/test_plugins.py` 覆盖规格列出的**全部**条目：`@plugin` 三形态、三种
返回值、`_pending`/`load_pending`、`add` 重名、拓扑（无依赖/A→B/A→B→C/环/未知
依赖）、`setup_all` 过滤六态、setup 顺序、start 顺序 + stop 逆序、setup 失败回滚
（逆序 stop + `ctx._rollback` + 原异常传播）、start 失败（传播 + `_started` +
后续 `stop_all` 清理）、stop 失败（吞 + 记日志 + 继续）、setup 耗时 warning、
`ctx.on("loop")` 零参、`ctx._rollback`、`load_builtin`/`load_directory`/
`load_entry_points`（模块与 callable 双分支）。

`tests/test_container.py`：last-wins、覆盖 debug 日志、`_wants_container` 陷阱、
`has_instance`（`set` 后 True / `singleton=False` 恒 False）。

`tests/test_application.py`：`setup → boot → init → start → loop → run → stop →
exit` 顺序、已 setup/start 时不重复、start 失败传播 + 已清理、exit hook 失败只
记日志、`Application.plugins()` / `load_plugins()`、`plugin_disables` 三种输入。

---

## 待确认（规格未覆盖的细节与我的选择）

### 1. `src/albuswall/plugin/` 在本分支已被删除，我做了「最小恢复」

规格多处假设 `plugin/discovery.py` 存在（「复用 `src/albuswall/plugin/
discovery.py`」「不要删 `_import_from_path` / `_import_quietly` /
`discover_directory`」），但本分支 `55b1b17 WIP: configue1 配置层重构` **删掉了
整个 `plugin/` 包**（`origin/main` 与 `55b1b17^` 里还有）。

我的选择：

- 从 `55b1b17^` 恢复 `plugin/discovery.py`，只做规格要求的改动：`discover_all`
  的 `user_dir` 默认 `None`（`None` 即跳过）、docstring 改成中文并标注
  `discover_all` **不推荐**（优先 `PluginManager.load_*`）、类型标注收紧
  （`eps: Any`、`target: Any`，使 pyright strict 下该文件 **0 error**）；
  `discover_package` / `discover_entry_points` / `discover_directory` 签名不变，
  `_import_from_path` / `_import_quietly` 保留，`types.ModuleType` vs callable
  双分支保留。
- 重写 `plugin/__init__.py`：**只**导出发现层，不再 import `declaration` /
  `manager`（它们在本分支不存在）。
- **不恢复** `declaration.py` / `manager.py`（规格要求「不要碰」，且 UI 子系统
  在本分支未落地）。将来 UI 分支合回来时，`plugin/__init__.py` 的 `__all__`
  需要重新加上 `declare` / `ui_loader`。
- 新建 `src/albuswall/plugins/__init__.py`（空包 + 说明）：`_boot` 里声明的
  `builtin_package="albuswall.plugins"` 需要一个真实目录，否则每次启动都会打
  「plugin package is not available」。**如果更希望「不建空包、让它静默跳过」，
  删掉这一个文件即可，其它代码无需改动。**

### 2. 重复 `setup_all` / 未 setup 的 `start_all` / `started` 的判据

规格没有定义重复调用的行为，也没给出「已 start」的判据（字段表里只有
`_started: list[str]`）。

我的选择：

- `setup_all` 二次调用 → `raise PluginError("plugins are already set up")`
  （fail-fast，与 D4 整体基调一致；不是静默跳过）。
- `start_all` 在未 setup 时调用 → `raise PluginError("plugins must be set up
  before start_all()")`，避免「start 一个没有 ctx 的插件」这种半初始化状态。
- `started` 用**额外的私有字段** `_start_done: bool` 实现，而不是
  `bool(self._started)`：零插件时后者永远 `False`，会让 `CoreApplication.exec`
  的条件调用每次都再跑一遍 `start_all`。该字段只增不减（`stop_all` 后仍为
  `True`，因为「start_all 已经跑过」这个事实不变）。

### 3. 元组长度的容错 + 回滚时「失败者自己停不停」+ `reportPrivateUsage`

规格只写 `(start, stop) tuple`，且「逆序 stop 已 setup 的插件」没有明确
「setup 失败的那个插件要不要调 stop」。

我的选择：

- `_resolve_lifecycle` 额外接受**单元素**元组 `(start,)`（只取 start）；长度
  ≥3 时 `logger.debug` 并忽略生命周期（不报错，宁可少做事也不炸启动）。
- setup 失败时，**失败者只 `ctx._rollback()`，不调它的 `stop()`**（它从没
  start 过）；只有**之前成功 setup** 的插件才按逆序 `_safe_stop()` + `_rollback`。
- `PluginManager` 调 `PluginContext._rollback()` 会被 pyright strict 报
  `reportPrivateUsage`（全仓库已有 87 条同类）。规格要求该方法私有（「
  `PluginManager` 内部用」），我保留名字并接受这 2 条报告，没有加
  `# pyright: ignore`（仓库现有风格用 `# noinspection`，且 strict 全仓库本就
  未清零：`4505 errors` 基线）。

### 另外两处「规格写法和现状不符」，已就地变通

- **`conf.get("plugin_disables", {})` 在 D6 里不可用**：新版 `Configue` 是声明式
  树视图，没有 `dict.get`。改用 `"plugin_disables" in conf.tree.main` +
  对 `_SectionView` 迭代取 keys（`set(...)`），语义等同旧
  `parse_section_file(..., "plugin_disables")`（只看 key，忽略 value）。实测在
  真实 `~/.config/albuswall/config.ini` 上读到了既有的 `builtin_ui` 并正确走
  「未知插件名只 debug」分支。
- **`app.plugins.load_pending()` 在 D6 里会 AttributeError**：`Application` 是
  门面类，`plugins` 是**类方法**，实例上 `app.plugins` 拿到的是绑定方法。
  `main._boot` 里改成 `manager = app.plugins()` 再使用。

## 兼容性 / 已知边界（已写进文档）

- 容器 last-wins 不追踪已解析引用；回滚只删「本插件新增」的名字，不复原被
  覆盖的旧工厂；类级单例（`Configue._instance` / `Application._instance` /
  `hooks_map`）不受容器覆盖影响。
- 插件绕过 `ctx` 直接 `registry.register(...)` / `container.register(...)`
  时，回滚**不会**撤销。
- `setup` 早于 `setup_log`：setup 阶段 `ctx.require("logger")` 会
  `MissingDependencyError`，要写日志用 `ctx.log`。
- `CoreApplication.finally_` 顺序是**先 `plugins.stop_all()` 再跑 `exit` 钩子**，
  所以 exit 钩子不能用插件已 stop 的资源；exit 钩子异常只记日志。
- `on_fatal` 目前无消费者；`on_error` 由 `sys.excepthook` 与
  `Runtime` 的 `threading.excepthook` 两处触发。
