# AlbusWall 插件作者指南

面向给自己或第三方写插件的人。看完这一篇，你应该能写出一个**能被发现、
能安全启动、能干净卸载**的插件。

相关源码：

| 文件 | 作用 |
| --- | --- |
| `src/albuswall/core/plugins.py` | `@plugin` / `PluginContext` / `PluginManager` |
| `src/albuswall/plugin/discovery.py` | 发现层（内置包 / `entry_points` / 用户目录） |
| `src/albuswall/core/container.py` | 依赖容器（last-wins 语义） |
| `src/albuswall/core/registry.py` | `hooks_map`（6 个生命周期事件） |
| `src/albuswall/core/application.py` | 编排：什么阶段跑什么 |

---

## 1. 快速开始

最小插件就是一个被 `@plugin` 装饰的工厂函数：

```python
# my_package/plugins/hello.py.txt
from albuswall.core.plugins import plugin


@plugin("hello", version="1.0.0")
def hello(container):
    """工厂：在 setup 阶段被调用，参数永远是依赖容器。"""
    logger = container.require("logger") if "logger" in container else None
    print("hello from my plugin")
```

装饰器只做**声明**：它把插件追加到 `_pending`，工厂本身要到 `PluginManager`
的 `setup` 阶段才执行。带依赖时：

```python
@plugin("gallery-stats", version="0.2.0", requires=("hello",))
def gallery_stats(container):
    repo = container.require("asset_repo")
    ...
```

`requires` 里写的是**插件名**。`setup_all` 会做拓扑排序：被依赖的先 setup、
先 start、后 stop。

### 工厂的三种返回值

工厂返回什么，决定插件有没有 `start` / `stop`：

```python
# 1) None —— 只有 setup（声明服务、挂 hook 的典型形态）
@plugin("declarative")
def declarative(container):
    container.register("clock", lambda: time.time)


# 2) (start, stop) 二元组 —— 最常见
@plugin("worker")
def worker(container):
    stop_event = threading.Event()
    thread = threading.Thread(target=run, args=(stop_event,))

    def start():
        thread.start()

    def stop():
        stop_event.set()
        thread.join(timeout=5)

    return start, stop


# 3) 带 .start / .stop 的对象
@plugin("scheduler")
def scheduler(container):
    return Scheduler(container.require("config"))   # Scheduler.start / .stop
```

其他返回值（数字、字符串、没有 `start`/`stop` 的对象）等价于 `None`，只记一条
debug 日志。单元素元组 `(start,)` 会被当作「只有 start」。

> 想让插件被 `PluginManager` 直接收编也可以不用装饰器：继承 `BasePlugin` 或
> 提供 `name` / `version` / `requires` / `setup` / `start` / `stop` 五个成员，
> 然后 `app.plugins().add(YourPlugin())`。

---

## 2. 顶层轻量契约

**模块顶层只允许做声明，重活留到工厂函数体内。**

```python
# 好：顶层只 import 插件系统本身
from albuswall.core.plugins import plugin


@plugin("pdf-export")
def pdf_export(container):
    # 重依赖在工厂体里 import——到这个时刻才需要它
    from reportlab.pdfgen import canvas
    ...
```

原因：发现层（`discover_package` / `discover_entry_points` /
`discover_directory`）靠 **import 你的模块**来触发声明。如果你在顶层
`import PySide6` 或连数据库，那么「扫一遍插件目录」的代价就等于「把每个插件
的重依赖全加载一遍」，启动时间被插件数量乘上去。

`PluginManager` 会 measure 每个插件的 **setup 耗时**，超过
`setup_slow_threshold_ms`（默认 `200.0` 毫秒）就打 warning：

```
plugin 'pdf-export' setup took 1532.4 ms (threshold 200 ms); 
move heavy imports into the factory body or lazily
```

看到这条日志，说明你的插件正在拖慢启动：把 import 挪进工厂体、或改成真正用到
时再加载。阈值是实例属性，宿主要调就 `app.plugins().setup_slow_threshold_ms = 500`。

---

## 3. `PluginContext` API 全表

`setup` 收到的唯一参数 `ctx`：

| 成员 | 说明 |
| --- | --- |
| `ctx.name` | 插件名（就是 `@plugin` 里的名字） |
| `ctx.container` | 依赖容器，**真实对象**，可以直接 `register` / `require` |
| `ctx.app` | `CoreApplication` 实例；要 app 就取它，别等 hook 参数 |
| `ctx.require(name)` | 强依赖获取，未注册抛 `MissingDependencyError` |
| `ctx.get(name, default=None)` | 宽松获取，未注册返回 `default` |
| `ctx.provide(name, factory, *, singleton=True, returns=None)` | 注册服务**并记录**，setup 失败时自动撤销 |
| `ctx.on(event)` | 注册 hook 的装饰器形态，同样记录以便撤销 |
| `ctx.register(event, hook)` | 直接注册一个**已经是最终形态**的 hook |
| `ctx.log` | 本插件的 `logging.Logger`（`albuswall.plugins.<name>`） |

`ctx.on` 的用法（注意 `loop` 也是零参）：

```python
def setup(ctx):
    @ctx.on("boot")
    def on_boot() -> None:
        ctx.log.debug("boot")

    @ctx.on("loop")            # 不要写 def on_loop(app)
    def on_loop() -> None:
        ...
```

`ctx.on("loop")` 内部会把零参回调包成 `hook(app)`。需要 app 时用 `ctx.app`。

`provide` 的工厂签名统一是 `Callable[[Container], Any]`：

```python
ctx.provide("pdf_exporter", lambda c: PdfExporter(c.require("config")))
```

---

## 4. `_wants_container` 陷阱

`Container` 判断「要不要注入容器」的规则是**看签名形状，不看形参名**：

* `type`（类）→ **永远零参构造**，不注入；
* 其他 callable → **只要存在必填位置参数**，就注入 `Container` 本身。

所以下面三个写法里，前两个拿到的是容器，最后一个才是你以为的那个：

```python
def make(config):       # ✗ 形参名叫 config，收到的却是 Container
    ...

def make(container):    # ✓ 名字随意，收到 Container
    ...

def make():             # ✓ 零参：什么都不注入（最安全）
    ...
```

推论（写插件时最容易踩的坑）：

1. `container.register("config_reader", MyReader)` —— `MyReader` 是类，零参
   构造，构造不出「读配置」的实例。要读配置就用函数工厂或在类里另开 `create`。
2. 想按需注入某个具体依赖，**先取容器再自己取**：

   ```python
   ctx.provide("report", lambda c: Report(c.require("config")))
   ```
3. 别忘了 `@plugin` 的工厂**也**收容器——不是 `ctx`。要 ctx 就在工厂里自己
   建，或者用 `BasePlugin.setup(ctx)` 的形态。

---

## 5. last-wins 的边界

`Container` 的语义是「**后注册覆盖先注册**」，没有 `override` 开关：

```python
container.register("clock", lambda: time.time)
container.register("clock", lambda: time.monotonic)   # 覆盖，旧工厂丢弃
```

覆盖时只打一条 **debug** 日志 `overriding dependency 'clock'`，不会 warning。
由此产生三条必须知道的边界：

1. **不追踪已解析引用。** 如果别人在覆盖之前已经 `require("clock")` 拿到了
   旧对象（尤其 `singleton=True` 的缓存实例），那个引用不会更新。想在运行期
   换实现，请让使用方每次 `require`（`singleton=False`）或自己加一层间接。
2. **回滚不「复原」。** setup 失败时 `ctx._rollback()` 只**删除**本插件通过
   `ctx.provide` 新增的名字，不会把被你覆盖掉的旧工厂塞回去。所以插件要覆盖
   别人的依赖时，想清楚失败后对方还剩什么。
3. **类级单例不受容器覆盖影响。** `Configue._instance`、`Application._instance`、
   `hooks_map` 都是进程级全局，容器里的同名 entry 与它们无关。

> 需要「一定要插队生效」的语义时，别在容器上造新概念：注册时包一层
> `lambda c: mine(c)`，把决策写进工厂体。

---

## 6. setup 失败时 exit hooks 仍会跑

`CoreApplication.exec` 的外层是 `try/finally`：

```python
def exec(self):
    loop = self._loop
    try:
        ...setup / start / hooks / loop.run()...
    finally:
        self.finally_()          # 一定会跑
```

而 `finally_` 里依次执行 `plugins.stop_all()` 和 `hooks_map["exit"]`。所以：

* 你的插件 setup 抛异常 → 异常继续往上冒（`Runtime` 把它变成退出码 `1`）→
  **exit hook 仍然执行**；
* 此时系统处于「部分初始化」状态：config 有，logger 可能还没装配，你的服务
  可能只注册了一半。

因此 **exit hook 必须幂等，并且必须容忍部分初始化**：

```python
@ctx.on("exit")
def on_exit() -> None:
    conn = ctx.get("my_connection")      # 用 get，而不是 require
    if conn is None:
        return                            # 没初始化到这一步，正常退出
    conn.close()
```

`ctx.on(...)` 注册的 hook 本来就是「setup 里挂的」，所以它们只在 setup 走到
那一步之后才存在——但 hook 自己仍要用 `ctx.get` 而不是 `ctx.require`。

---

## 7. 插件 stop 早于内置 exit hook

`finally_` 的顺序是**先 `stop_all()`，再跑 exit 钩子**：

```
plugins.stop_all()          # 逆序：后 start 的先 stop
run_hooks(hooks_map["exit"])
```

推论：**不要在你的 `exit` hook 里使用自己已经 stop 掉的资源**。`stop` 已经
把线程停了、连接关了，exit hook 再去用它们只会拿到半死对象或抛异常。

需要「退出前再动一次插件资源」的场景，正确做法是让 `stop()` 自己做完，别把
责任丢给 exit hook。

---

## 8. hook 撤销

通过 `ctx` 注册的 hook 会被记录，`setup` 失败时 `PluginManager` 逐个
`unregister`：

```python
def test_unregister():
    from albuswall.core.registry import unregister
    removed = unregister("boot", my_hook)     # -> True / False
```

但注意这条**边界**：

> 如果插件绕过 `ctx` 直接 `from albuswall.core.registry import register`，
> 那么回滚时**不会**被撤销——`PluginContext` 只记得自己代理过什么。

所以永远优先：

```python
@ctx.on("init")            # ✓ 会记录、会回滚
def on_init(): ...

register("init", my_hook)  # ✗ 不受管控
```

同理，`container.register(...)` 直接调用也不在回滚范围内；想被撤销就用
`ctx.provide(...)`。回滚只删「本插件新增」的名字，覆盖场景见第 5 节。

---

## 9. 异常语义

| 阶段 | 抛出时发生什么 |
| --- | --- |
| `boot` / `init` / `loop` hook | `run_hooks` **不吞异常**：后面的 hook 不再执行，异常一路上冒到 `exec` 的 `finally_`，进程以 `ExitCode.ERROR`（1）退出 |
| 插件 `setup` | 撤销本插件的注册 + hooks，逆序 stop 已 setup 的插件，然后**原样抛出** |
| 插件 `start` | 直接传播；`_started` 里已经记下该插件，所以 `stop_all` 会清理它 |
| 插件 `stop` | **吞掉**并 `logger.exception`，继续停下一个（退出路径不允许崩） |
| `exit` hook | 吞掉并 `logger.exception("exit hook failed")` |
| `on_error` hook | 返回 `True` 表示「我处理了」，其后注册的 on_error hook 不再执行（短路） |
| `on_fatal` hook | 目前**没有消费者**，注册了也不会被调用 |

`on_error` 的触发点有两个：

* `sys.excepthook`（主线程未捕获异常，见 `core/excepthook.py`）；
* `Runtime` 安装的 `threading.excepthook`（非主线程未捕获异常）——没人认领时
  才打印 traceback，然后请求退出码 `ExitCode.ERROR`。

**不要 `raise SystemExit`。** 它绕开正常退出路径：`exec` 的 `finally_` 虽然
仍会跑，但你期望的退出码会被 `Runtime._system_exit_code` 重新解释。要退出就
让异常自然传播，或者 `app.instance()._loop.quit(code)`（内部 API）。

---

## 10. 日志

`setup_log(container)` 会把配置好的根 logger 放进容器：

```python
def start(self):
    logger = self.ctx.require("logger")   # 就是 logging.getLogger("albuswall")
    logger.addHandler(my_handler)
```

可以做：加 handler、改 formatter、调 `setLevel`、加 `Filter`。
**不能做**：替换 logger 对象本身（`logging` 的树结构按名字查找，塞一个新对象
进容器不会影响别人已经拿到的引用）。

注意时机：`setup` 早于 `setup_log`，所以在 **setup 阶段** `ctx.require("logger")`
会 `MissingDependencyError`。setup 里要写日志就用 `ctx.log`：

```python
def setup(self, ctx):
    ctx.log.debug("my plugin setting up")   # ✓ 任何阶段都能用
```

`ctx.log` 命名空间是 `albuswall.plugins.<你的插件名>`，因此会继承
`albuswall` logger 的级别与 handler。

---

## 11. 入口点声明

第三方分发（pip 安装的包）用 `entry_points` 声明，**无需**让用户改配置：

```toml
# 你的包的 pyproject.toml
[project.entry-points."albuswall.plugins"]
foo = "albuswall_foo"

# 或者指向一个 callable（由它自己完成声明）
bar = "albuswall_bar.declare"
```

两种 target：

| target | 行为 |
| --- | --- |
| **模块**（`"albuswall_foo"`） | 被 import；模块顶层的 `@plugin` 在 import 期完成声明 |
| **callable**（`"albuswall_bar.declare"`） | 先 `load()` 拿到函数，再调用一次；由函数内部调用 `@plugin` 完成声明 |

模块形态最省事：

```python
# albuswall_foo/__init__.py —— entry point 直接指向包，import 即声明
from albuswall.core.plugins import plugin


@plugin("foo", version="0.1.0")
def foo(container):
    ...
```

callable 形态适合「声明前要先算点什么」的场景：

```python
# albuswall_bar/declare.py
def declare() -> None:
    from albuswall.core.plugins import plugin

    @plugin("bar")
    def bar(container):
        ...
```

发现层的容错：单个 entry point `load()` 或执行失败只记
`logger.exception(... skipped)`，**不影响其它插件**。

宿主侧怎么触发发现（三选一或组合）：

```python
app = Application()
app.plugins().load_builtin("albuswall.plugins")   # 第一方：扫内置包
app.plugins().load_entry_points()                 # 第三方：entry_points
app.plugins().load_directory(Path("~/.local/share/albuswall/plugins").expanduser())
app.plugins().setup_all(disabled={"builtin_ui"})  # 过滤在 setup 阶段生效
```

`Application.load_plugins(group=...)` 是 `load_entry_points` 的门面。

### 用配置禁用插件

主程序 `main._boot` 会读 `config.ini` 的 `[plugin_disables]` 段，**段里的 key
就是被禁用的插件名**（value 不参与判断）：

```ini
[plugin_disables]
builtin_ui = true
ipc = true
```

未知名字只记 debug（插件被卸掉后残留配置是常态）；而 `setup_all(enabled=[...])`
的白名单里出现未知名字会直接 `KeyError`——写错名字说明配置有 bug。

---

## 12. 调试

**看容器里有什么**：

```python
print(app.container())      # pformat：factories（含 returns/singleton）+ instances
repr(app.container())       # 简洁：<Container [a, b, c]>
"logger" in app.container() # 是否注册
app.container().has_instance("clock")   # 是否已有缓存实例（singleton=False 恒 False）
```

**看插件管理器状态**：

```python
manager = app.plugins()
print(manager)              # <PluginManager [a, b] setup, started=2>
list(manager)               # 声明顺序
manager.setup_done          # 是否已经 setup 过
manager.started             # 是否跑完 start_all
```

**setup 耗时 warning**：把阈值调到 0 就能看到每个插件的 setup 耗时（调试用）：

```python
manager.setup_slow_threshold_ms = 0     # 每个插件都会 warning
```

**坏插件为什么没加载**：发现层对每个失败都有一条日志，把 `albuswall` logger
开到 DEBUG：

```python
logging.getLogger("albuswall").setLevel(logging.DEBUG)
```

会看到 `failed to load plugin module 'xxx', skipped`（含 traceback）、
`plugin dir ... does not exist, skipping`、`disabled list contains unknown
plugins, ignored: [...]` 等。

**手动检查 `_pending`**：`@plugin` 只是往这里追加，还没被收编时可以直查：

```python
from albuswall.core.plugins import _pending
print([p.name for p in _pending])       # 只有模块已被 import 的插件在列
```

`load_pending()` 会把它清空并 `add` 进管理器；重复调用不会重复收编。

**在测试里复现**：`tests/conftest.py` 有一个 `autouse` fixture
（`_reset_global_plugin_state`）会在每个用例后复位 `hooks_map`、`_pending` 与
`Application` / `CoreApplication` / `Configue` 的单例字段。你自己的插件测试
可以直接受益——不必手动清理。
