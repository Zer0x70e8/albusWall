# 快捷键系统（keymap）

> 状态：**已接通真实按键**。代码在 `src/albuswall/ui/keymap/`（解析 / 身份 /
> 绑定 / 配置 / Qt 事件过滤器），当前界面的 18 条命令由
> `src/albuswall/ui/presenter/shortcuts.py` 登记成槽，键位表在
> `src/albuswall/resources/config/shortcuts.ini`（用户覆盖
> `<user config dir>/shortcuts.ini`）。按 `Ctrl+Q` / `F11` / `G, G` 之类真的
> 会驱动界面。设置页已接回主窗口（改键位仍要改文件 + 重启）。

## 一句话模型

**槽（命令路径）登记处理函数；配置文件把键位绑到命令路径；`load()` 整表重读后
`trigger(键)` / `invoke(命令)` 调用处理函数，并带上记录里声明的固定实参。**

两个方向都是一对多：一个键位 → 多个命令，一个命令 → 多个键位。键位的唯一
事实来源是**配置文件**，不是代码：只有配置里没有出现的命令，才落到槽装饰器
声明的兜底键位。

## 模块分层

| 文件 | 职责 |
| --- | --- |
| `keymap/__init__.py` | 公开 API（`__all__`）+ 用法 docstring |
| `keymap/sequence.py` | `KeySequence` / `parse_sequence(s)` / `canonical_sequence` / `format_sequences` / `ShortcutKind` / `MODIFIERS` / `CHORD_SEPARATOR` / `MAX_CHORDS`（只碰 `QtGui.QKeySequence`） |
| `keymap/keys.py` | `KeyAlias`：按键**身份**枚举，一个成员挂着它的全部写法（`Shift+Tab` == `Shift+Backtab` == `Backtab`、`Enter` == `Return`、`Delete` == `Del`）+ `step_identity` / `sequence_identity` / `split_step`；`NAVIGATION_KEYS` / `WIDGET_KEYS` 两张「不归快捷键」的名单 |
| `keymap/model.py` | `ShortcutBinding`（键位 + 位置参数 + 关键字参数）、`ShortcutSlot`（`id` + `handler` + `help` + 兜底键位/实参）；两个 frozen dataclass，纯数据，不 import Qt |
| `keymap/registry.py` | `ShortcutRegistry`：槽注册、双向一对多绑定表、`load` / `persist`、与 `albuswall.command` 对接 |
| `keymap/config.py` | `ShortcutConfig`：默认表 + 用户表读 / 写、记录格式、原子写盘；`default_config_path()` / `user_config_path()` / `DEFAULT_FILE_NAME` / `DEFAULT_SECTION` |
| `keymap/errors.py` | `KeymapError` / `InvalidKeySequence` / `DuplicateShortcutSlot` / `ShortcutConfigError` |
| `keymap/qt_binder.py` | `ShortcutBinder`：挂在 `QApplication` 上的事件过滤器，`QKeyEvent` → `KeySequence` → `trigger()`；和弦状态机 + 超时；本包**唯一** import `QtWidgets` 的模块 |

`tests/test_ui_keymap.py` 是可执行规范：行为与边界以它为准；按键层由
`tests/test_ui_keymap_binder.py`（20 个用例：能触发 / 不越界 / 别名 / 安装面），
界面这一侧的槽登记由 `tests/test_ui_shortcuts.py` 与
`tests/test_ui_presenter_flow.py` 的「快捷键槽驱动界面」用例守着。

`model.py` **不是** Qt 视图模型，只是两个 dataclass；仓库里没有
`QAbstractItemModel`、没有设置页需要的行模型。

## 默认键位（`src/albuswall/resources/config/shortcuts.ini`）

`[shortcuts]` 段，一条命令一行；**18 条命令**：12 条有默认键位（14 个键位），
6 条默认解绑（写成 `= []`，功能在、只是没给键）。分组是文件里的注释分组。

| 分组 | 命令 | 键位 |
| --- | --- | --- |
| application | `app.quit` | `Ctrl+Q` |
| application | `app.settings` | `Ctrl+,` |
| window / view | `window.fullscreen` | `F11`, `Ctrl+Shift+F` |
| window / view | `window.close` | `Ctrl+W` |
| window / layer | `window.album` | **默认解绑**（例：`Ctrl+1`） |
| window / layer | `window.source` | **默认解绑**（例：`Ctrl+2`） |
| window / view | `view.zoom_in` | `Ctrl++`, `Ctrl+=` |
| window / view | `view.zoom_out` | `Ctrl+-` |
| window / view | `view.reset` | `Ctrl+0` |
| content / view | `content.search` | `Ctrl+F` |
| content / view | `content.preview` | **默认解绑**（例：`Ctrl+Return`） |
| content / view | `viewer.next` / `viewer.prev` | **默认解绑**（例：`Alt+Right` / `Alt+Left`） |
| albums / assets | `album.new` | `Ctrl+N` |
| albums / assets | `album.rename` | `F2` |
| albums / assets | `asset.favorite` | `Ctrl+D` |
| albums / assets | `asset.restore` | **默认解绑**（例：`Ctrl+R`） |
| albums / assets | `asset.delete` | `Delete`（规范化后是 `Del`） |

这些命令都是当前界面**真的实现了**的动作：`ui/presenter/shortcuts.py` 把它们
逐个登记成槽（见下一节）。全新的 `ShortcutRegistry()` 自己 `load()` 之后仍然
`slots() == ()` —— 槽只在界面 boot 调过 `install_ui_shortcuts()` 之后才存在。

`asset.favorite` 用 `Ctrl+D` 而不是 `Space`：空格在内容网格里是「快速预览」、
在按钮上是「点击」，属于控件的键，binder 会主动让渡（见「真实按键」一节）。

**默认解绑（`= []`）** 的 6 条命令是「功能已实现、但默认不给键位」：在
`<user config dir>/shortcuts.ini` 里把 `[]` 换成键位（如
`window.album = ["Ctrl+1"]`）就生效，不需要改代码。所以
`unbound_slots()` 现在报告的就是这 6 条，不是配置漏写。

## 配置文件格式

```ini
[shortcuts]
# 完整记录（save() 总是写这种）：命令 = [快捷键, 位置参数, 关键字参数]
album.open        = ["Alt+1", [1], {"mode": "fit"}]
view.fullscreen   = [["F11", "Ctrl+Shift+F"], [], {}]
# 简写 1：扁平字符串数组 = 多个键位 + 无参数（默认表用的就是它）
window.fullscreen = ["F11"]
# 简写 2：单个字符串 = 一个键位 + 无参数（引号可省：Ctrl+Q 等价）
app.quit          = "Ctrl+Q"
# 简写 3：空值 = 显式解绑（槽还在，但没有任何键位）
album.new         = []
```

| 位置 | 含义 |
| --- | --- |
| `[0]` 快捷键 | 单个写字符串（`"Alt+1"`），多个写字符串数组；`[]` = 解绑 |
| `[1]` 位置参数 | JSON 数组，触发时按位置传给处理函数 |
| `[2]` 关键字参数 | JSON 对象，触发时按关键字传给处理函数 |

* 记录结构必须是 `[快捷键, 位置参数, 关键字参数]`，少了第三项又不是扁平字符串
  数组 → `InvalidKeySequence` 包装成 `ShortcutConfigError`；
* 键位为 `[]` / `""` 时 `args` / `kwargs` 也可以非空——「不许按，但仍可
  `invoke`」是同一种记录；
* 命令名大小写有意义（`parser.optionxform = str`）；
* 命令行内的键位书写要用 JSON 数组，不用逗号分隔——逗号是**和弦分隔符**；
* 多键位列表（如 `["F11", "Ctrl+Shift+F"]`）**按书写顺序展示与比较**——换个
  顺序在语义上等价，但 `persist()` 会视为「改过」而重写一行（无损）。这是
  刻意设计：多键位的展示顺序由用户决定，实现不替用户排序。

### 两份文件与优先级（由配置器 `reg()` 登记）

| 优先级 | 注册名 | 文件 | 说明 |
| --- | --- | --- | --- |
| 1（高） | `shortcuts` | `<base_dir>/shortcuts.ini`（生产 = `~/.config/albuswall/shortcuts.ini`） | 可写，`order=30`，`autosave=manual`；只存与默认表**不同**的条目 |
| 2 | `shortcuts.default` | `src/albuswall/resources/config/shortcuts.ini` | 只读（`mark_readonly_file`），`order=20`，`autosave=off`；随包分发 |
| 3（低） | — | 槽装饰器的 `keys= / args= / kwargs=` | 只给配置里没有的命令补位 |

* 合并靠 `FileInfo.order` 的**升序 + `overwrite=True`**：用户表后注入、逐 key
  覆盖，用户没写的 key 仍拿默认值（不是整表覆盖）；
* 记录类型是 `albuswall.common.keymap.ShortcutRecord`（`keys` / `args` /
  `kwargs`，纯标准库）；转换器与加载钩子由 `setup_config` 登记，所以
  `[shortcuts]` 的每个 key 在 `load()` 的类型自检里就有类型；
* 两份文件都 `optional=True`：用户文件在第一次保存前本来就不存在；
* 写回目标由 `Configue.target_file_of` 推导：默认表只读 → 落到用户表
  （`register_section_file("shortcuts", "shortcuts")` 是最后一步兜底，
  因为 `[shortcuts]` 没有 `@configclass` 字段）；
* 写盘是原子的（同目录临时文件 + `os.replace`），用户文件里的注释不保留。

`ShortcutConfig`（旧路径）仍然保留：配置器没装配（单测、headless 诊断）时
`ShortcutRegistry.load()` 读它，行为与改造前完全一致。

### 配置器接管之后

```python
# 值一变就重建绑定（界面立即生效，不需要重启）
config.subscribe(registry._on_config_change)      # bind_config() 内部就是这么做的

# 写一条键位（走树视图：顺手把 origin 改成用户表，运行期新 key 也能写对文件）
registry.save_shortcut("app.quit", ShortcutRecord(keys=("Ctrl+Alt+Q",)))

# 恢复默认：内存回退随包默认值，flush 删掉用户文件里那一行
config.reset("shortcuts", "app.quit")
config.flush(file="shortcuts")
```

* 事件来源有四种：`set` / `remove` / `reset` / `reload`（`flush` **不**广播）；
  同一事件循环内的多次变更合并成一次重建（`QTimer.singleShot(0, ...)`），
  没有 `QCoreApplication` 时同步重建；
* `Configue.reset(section, key)` 只删**用户那一份**，随包默认表永远不动；
  只有只读文件定义过的 key 会直接返回 `False`（它本来就是默认值）。

### 查看当前配置：`albusctl ui shortcuts`

键位表不在 `config.ini` 里，所以 `setting.schema` 看不到它。只读视图由
`albuswall.ui.window.settings.shortcut_config_info()` 提供，命令入口是
`ui.shortcuts`（`service/command/handlers/ui.py`）：

```bash
albusctl ui shortcuts                  # 表格：命令 / 默认键 / 生效键 / 来源
albusctl ui shortcuts --verbose        # 多一列 help
albusctl ui shortcuts --source=user    # 只看用户覆盖过的条目
albusctl ui shortcuts --format=json    # 机器可读（设置页直接吃）
```

* 数据来源两处：`shortcuts.ini`（默认表 + 用户覆盖，**总是**可读）与运行期
  注册表（容器里的 `shortcuts` 键，界面起过才有）——后者提供 `help` 与
  「最终生效」的键位（配置里没有的命令用槽的兜底绑定）；
* 报告里带 `conflicts`（一键多命令）、`unbound`（没有任何键位的命令）、
  `files`（两份文件的路径与是否存在）与 `errors`（读失败的诊断）；
* **只读**：写键位仍是 `ShortcutRegistry.persist()` 或直接编辑用户表。
  同样只读的 `albusctl ui state` 输出 `[window]` 段（窗口几何 / 上次相册 /
  网格列数，见 `albuswall.ui.window.settings`）。

## 键位字符串语法

用 **Qt PortableText**，不用 NativeText（后者随平台变：macOS 上是 `⌘` 不是 `Ctrl`，写进配置不可移植）。

| 形态 | `ShortcutKind` | 例子 |
| --- | --- | --- |
| 单按键 | `SINGLE` | `F5` / `Space` |
| 修饰键 + 按键 | `MODIFIED` | `Ctrl+N` / `Ctrl+Shift+P` |
| 多步和弦 | `CHORD` | `G, G` / `Ctrl+K, Ctrl+C` / `Backspace, 0` |

* 修饰键集合与规范顺序：`MODIFIERS = ("Ctrl", "Shift", "Alt", "Meta")`；
* 和弦分隔符固定 `", "`（`CHORD_SEPARATOR`），Qt 上限 4 步（`MAX_CHORDS`）；
* 解析即规范化：`ctrl+shift+p` / `Shift+Ctrl+P` → `Ctrl+Shift+P`；
  `Delete` → `Del`；`Backspace,0` → `Backspace, 0`；
* 逗号键只能写成 `"Ctrl+,"`（紧跟 `+` 的逗号算按键本身，其余逗号算分隔符），
  所以「多个键位」写 JSON 数组而不是逗号分隔；
* `parse_sequences()` 接受单个字符串、JSON 数组字符串、空串/空数组（→ `()`）、
  以及可迭代对象；**去重且保持首次出现顺序**。

## 注册与触发流程

```python
from albuswall.ui.keymap import get_default_registry, load_shortcuts, shortcut_slot

@shortcut_slot("album.open", help_="打开相册", keys="Alt+1",
               args=(1,), kwargs={"mode": "fit"})
def open_album(index: int, *, mode: str = "grid") -> None: ...

load_shortcuts()                    # 整表重读（默认操作进程级注册表）
registry = get_default_registry()
registry.trigger("Alt+1")           # 「按一下键」：调该键位绑定的所有命令
registry.invoke("album.open")       # 按命令触发：配置实参 + 显式实参
registry.persist()                  # 把与默认不同的条目写回用户表
```

| 步 | 入口 | 行为 |
| --- | --- | --- |
| 1. 登记槽 | `shortcut_slot(slot_id, *, help_="", keys=(), args=(), kwargs=None, replace=False)` → `ShortcutRegistry.slot` → `register_slot` | 装饰器返回**原函数**不包装；重复 `id` 抛 `DuplicateShortcutSlot`，`replace=True` 才覆盖；给出 `command_registry` 时同一槽同时注册成 `albuswall.command` 的命令（同一 handler 幂等，被别的 handler 占用抛 `DuplicateShortcutSlot`） |
| 2. 装载 | `load_shortcuts(config=None, *, registry=None, strict=True)` → `ShortcutRegistry.load(config=None, *, strict=True)` | `clear_bindings()` → 读配置 → 给配置里缺席的槽补兜底绑定 → 逐条 `bind_command(..., replace=False)`；**每次重新读盘**，不缓存，导入期的临时绑定会被清掉 |
| 3. 绑定 | `bind(keys, *commands, args, kwargs, replace=False)` / `bind_command(command, keys, *, args, kwargs, replace=True)` | 前者一键多命令（追加去重 / `replace` 整体替换），后者是配置文件方向（默认先清掉该命令原键位）；另有 `unbind` / `unbind_command` / `clear_bindings` |
| 4. 调用 | `invoke(command, *args, **kwargs)` | `slot.handler(*配置位置参数, *显式位置参数, **{**配置关键字参数, **显式关键字参数})`；显式位置参数追加在后面，同名关键字参数覆盖；槽未注册抛 `KeyError` |
| 5. 按键 | `trigger(keys)` | 对该键位绑定的每条命令逐个 `invoke`，返回返回值元组；绑到未注册的槽时记 debug 日志并跳过；没有绑定则返回 `()` |

其他入口：`get_default_registry()`（进程级单例）、`registry_shortcut_registry(container)`
（只做 `container.register("shortcuts", get_default_registry, returns=ShortcutRegistry)`，
**不**自动 `load()`）。

诊断查询：`commands_for(键)` / `keys_for(命令)` / `binding_for(命令)` /
`args_for` / `kwargs_for` / `bindings()` / `commands()` /
`conflicts()`（一键多命令，是能力不是错误）/ `unbound_slots()`（没有任何键位
指过来的槽：随包表里 `= []` 的那 6 条，或者「新命令忘了在默认表里加一行」）。

## 界面接入：当前 UI 的槽登记

`src/albuswall/ui/presenter/shortcuts.py` 是界面与配置器之间**唯一**的接线点：
把当前界面的 18 条命令登记成槽（接口），再载入键位表（数据）。它**不**接管
真实按键，也不认识任何视图控件。

### 落点

| 命令 | 落点（presenter.方法） |
| --- | --- |
| `app.quit` | `window.quit`（= 菜单「退出」：`app.quit()` + 关窗口） |
| `app.settings` | `window.open_settings`（打开设置浮层；数据由 `settings` presenter 提供） |
| `window.fullscreen` | `window.toggle_fullscreen` → `Window.toggle_fullscreen()`（`showFullScreen` / `showNormal`） |
| `window.close` | `window.close_layer` → `Window.close_layer()`（先收浮层页，没有浮层页才关窗口） |
| `view.zoom_in` / `view.zoom_out` / `view.reset` | `viewer.zoom_in` / `zoom_out` / `reset_zoom` → `Detail.zoom_*()` → `ImageViewer` |
| `content.search` | `search.focus_search`（展开搜索栏 + 抢输入框焦点） |
| `album.new` / `album.rename` | `album.create_album` / `album.rename_current`（弹输入框，与菜单入口同一条路） |
| `window.album` / `window.source` | `window.toggle_album` / `window.toggle_source`（返回「现在是否在前台」） |
| `viewer.next` / `viewer.prev` | `viewer.next_asset` / `viewer.previous_asset`（详情页没开时返回 `False`） |
| `content.preview` | `content.preview_current`（对网格光标那一格并发 `asset_previewed`，与空格同一条出口） |
| `asset.favorite` | 详情页开着 → `viewer.toggle_favorite`；否则 `content.toggle_favorite`（网格光标那一格） |
| `asset.restore` | 详情页开着 → `viewer.restore_current`；否则 `content.restore_current` |
| `asset.delete` | 详情页开着 → `viewer.trash_current`；否则 `content.trash_current` |

### 约束

* **懒解析**：槽的处理函数捕获的是**容器**，触发时才
  `container.get("presenter")` 并按 `Presenter.name` 找子节点。登记顺序与
  presenter 构建顺序无关；没有界面（headless / 单测里只有容器）时触发是
  静默 no-op，返回 `False`（不抛 `KeyError`）；
* **只指向公开方法**：落点必须是 presenter 的公开接口（`_` 开头一律不许），
  `tests/test_ui_shortcuts.py` 用结构断言守着，防止改名后静默失效；
* **`asset.*` 的分流**看 `ViewerPresenter.is_detail_open`（`_ctx` 有值**且**
  详情页是浮层当前页），不是只看 `_ctx`：浮层被收起后 `_ctx` 还在；
* **登记时机**：`ui/bootstrap.setup_ui` 的 phase 5（presenter 树建好之后）
  调 `install_ui_shortcuts(container)` —— 内部 `replace=True` 幂等登记，然后
  一次 `load_shortcuts(strict=False)`；配置坏了只记日志，不拦界面启动；
* **缩放只属于详情页**：`view.zoom_*` 没有「网格缩放」语义，详情页没开时
  返回 `False`。

```python
from albuswall.ui.keymap import get_default_registry

get_default_registry().trigger("Ctrl+N")            # == album.create_album()
get_default_registry().trigger("F11")               # == window.toggle_fullscreen()
get_default_registry().invoke("content.search")     # 按命令触发，同上
```

## 真实按键：binder

`src/albuswall/ui/keymap/qt_binder.py` 是「按键 → 命令」的最后一段：挂在
`QApplication` 上的事件过滤器，把 `QKeyEvent` 规范化成 `KeySequence`，在绑定表里
查（按**身份**匹配，见下）再调 `trigger()`。界面 boot 的 phase 5 装它
（`container["shortcuts_binder"]`），owner = 主窗口。

不用 `QShortcut`：它只能挂单步按键，和弦要自己攒状态；而且没法表达「这个键
此刻归控件」。

### 判定顺序（就是 `eventFilter` 的顺序）

| # | 规则 | 为什么 |
| --- | --- | --- |
| 1 | 只处理 `KeyPress`；忽略自动重复、纯修饰键、**非自发事件** | 合成事件（`activation_keys` 把回车改写成空格）不能再触发一遍 |
| 2 | 只在**焦点控件那一遍**处理 | Qt 会把没被消费的按键向父控件冒泡，同一次按键会以「接收者 ≠ 焦点控件」再来一次 |
| 3 | 作用域 = owner 顶层窗口 | 弹窗 / 菜单里打字不该触发主窗口的快捷键 |
| 4 | **导航键永不接管**：`Tab` / `Shift+Tab` / 方向键 / `Home` / `End` / `PgUp` / `PgDn` / `Esc` | 焦点链与 Esc 层级归控件和窗口，绑了也不生效 |
| 5 | 文本输入里的**无修饰键**放行；`keymap_passthrough` 控件上的**空格 / 回车**放行 | 打字、按钮点击、网格快速预览都是控件自己的语义；修饰键组合照常触发 |
| 6 | **只吞能触发的键**（命中的绑定里至少一个已登记槽） | 占位绑定（命令还没实现 / 插件没加载）不改变现有键盘行为 |
| 7 | 和弦攒前缀 + 超时（`DEFAULT_CHORD_TIMEOUT_MS` = 1.2s） | `G, G`；前缀既是完整键位又是更长和弦开头时先等超时再退化 |
| 8 | 同一次按下的重复投递不重复触发 | 见下「重复投递」 |

### 按键身份：`KeyAlias`

Qt 对同一个物理键有多种写法，**配置里写的**与**事件里转出来的**不一致时不能
靠字符串比较：

| 物理按键 | 配置常见写法 | 事件转出的文本 |
| --- | --- | --- |
| `Shift+Tab` | `Shift+Tab` | `Shift+Backtab` |
| 主回车 | `Enter` | `Return` |
| 小键盘回车 | `Enter` | `Enter` |
| `Delete` | `Delete` / `Del` | `Del` |

所以匹配走 `keys.step_identity()`：`(修饰键集合, KeyAlias | 键名)`，一个成员的
`aliases` 里出现过的任何写法都算同一个键。**不做文本归一**——配置里写
`Delete`，`keys_for()` 就仍然回显 `Delete`。

### 重复投递

窗口状态刚变化（例如刚 `show()`）时，同一次按下可能被投递两遍（`QTest` 与部分
平台插件都会这样）。`KeyRelease` 之前同一个键再来一次按下的，视为重复投递，
直接忽略——真实键盘不可能在没松开的情况下第二次按下同一个键。

### 与其它键盘层的关系

| 层 | 归谁 | 关系 |
| --- | --- | --- |
| 焦点链（Tab / 方向键） | Qt | binder 永不接管（规则 4） |
| Esc 两级（取消焦点 → 收浮层） | `Window.keyPressEvent` | binder 不接管；和弦进行中按 Esc 只放弃前缀 |
| 空格 / 回车 = 激活 | `utils/activation_keys.py` | binder 让渡（规则 5）；它合成的空格是非自发事件，binder 不理 |
| 网格光标 / 快速预览 | `widgets/view/tile_view.py` | `keymap_passthrough` 属性 + 规则 5 |
| 搜索栏 / 标签树弹窗 | 各自控件 | 弹窗是独立顶层窗口，靠规则 3 排除 |

## 校验与错误



| 场景 | 表现 |
| --- | --- |
| 键位字符串非法：空串 / 未知键名（`Ctrl+Foo`）/ Qt 静默截断（`Ctrl+K, Foo`）/ 只有修饰键（`Ctrl+Shift`）/ 超过 4 步和弦 | `InvalidKeySequence`（`canonical_sequence()` 抛；Qt 对拼不过的名字返回空串而不报错，这里必须转成异常） |
| `KeySequence(...)` 直接传非规范文本（`"ctrl+p"`） | `InvalidKeySequence`（`__post_init__` 拒绝，请用 `KeySequence.parse()`） |
| 同名槽重复注册；命令层该路径已被别的 handler 占用 | `DuplicateShortcutSlot` |
| 默认表缺失 / 文件没有 `[shortcuts]` 段 / 读盘失败 / 写盘失败 / 记录不可 JSON 序列化 | `ShortcutConfigError` |
| 配置文件里单条记录非法 | `strict=True` → `ShortcutConfigError`（前缀 `文件: [段] 命令:`）；`strict=False` → 记 warning 跳过该条 |
| `bind` 没给命令、键位为空、命令名空串；`set_call_args` 位置参数不是可迭代 / 关键字参数不是 mapping / 关键字名不是字符串 | `ValueError` |
| `invoke` / `command_handler` 一个没注册的槽 | `KeyError` |
| `ShortcutSlot` / `ShortcutBinding` 结构非法（`id` 空、handler 不可调用、`kwargs` 不是 mapping 或键不是 str） | `ValueError` / `TypeError` |

`KeymapError` 是整层基类，一条 `except KeymapError` 能兜住；
`InvalidKeySequence` 同时继承 `ValueError`（键位错本质是值错）。

## 窗口内的「激活 / 确定」键（keymap 主动让渡）

窗口里「确定 / 打开 / 激活当前项」不再按控件分键位：**空格与回车等价**。
实现只有一处——`ui/utils/activation_keys.py` 的应用级事件过滤器，由
`ui/bootstrap.py` 的 phase 1 装到 `QApplication` 上（幂等）：

| 场景 | 以前 | 现在 |
| --- | --- | --- |
| `QAbstractButton`（按钮 / 复选框 / 工具按钮） | 只有空格触发 `clicked` | 回车 / 小键盘回车改写成一次空格（按下动画 + `clicked` + 带菜单的按钮弹出菜单） |
| 对话框里的按钮 | 回车 = 默认按钮 | **不动**：保持 Qt 的默认按钮语义 |
| `QAbstractItemView`（列表 / 网格 / 树） | 只有回车 / 双击发 `activated` | 空格也发 `activated(currentIndex())` |
| 带菜单的按钮 | 鼠标点才有菜单 | `Menu` 键 / `Shift+F10` 也打开菜单（与标签树、导入源卡片的约定一致） |

* 带 `Ctrl` / `Alt` / `Meta` 的组合与按住不放（自动重复）一律不接管；
* 文本框里的空格 / 回车属于输入内容，过滤器只认 `QAbstractButton` 与
  `QAbstractItemView`，碰不到它们。

binder 与这一层不打架：空格 / 回车在 binder 里属于 `KeyAlias.SPACE` /
`RETURN`（见「真实按键」第 5 条），焦点在文本输入或 `keymap_passthrough`
控件上时不接管；而本过滤器**改写出来**的空格是非自发事件，binder 直接忽略，
不会「回车 → 空格 → 又触发一遍快捷键」。

**唯一的例外是内容视图**（`Content` 的资产网格，`TileView`）：
空格是「快速预览」、回车是「详情」，两个键分开（见 `unit_previewed` /
`unit_activated`）；预览中再按空格退出预览（回到网格），按回车则升级成
完整详情页（`Detail.set_preview_mode`）。它虽然是 `QAbstractItemView`，
但类属性 `space_previews = True` 会让激活键过滤器跳过它 —— 空格由视图
自己的 `keyPressEvent` 处理，不会被改写成「激活」。

## Esc 的层级

实现在 `ui/window/window.py` 的 `Window.keyPressEvent` / `Window.cancel_focus`：

| 按键 | 效果 |
| --- | --- |
| `Esc`（还有焦点控件时） | **取消焦点**：`clearFocus()`，叠加层 / 当前页保持原样 |
| `Esc`（焦点已取消时） | **收起叠加层**，回到空白占位页（`close_overlay()`） |
| `Shift+Esc` | 不看焦点，**直接收起叠加层** |
| `Ctrl/Alt/Meta + Esc` | 不参与这套层级（留给其它消费者） |

* 「取消焦点」是 `clearFocus()`，不是把焦点挪到空白页：空白页是 `NoFocus`，
  挪过去等于焦点仍挂在某个控件上，「取消」不可观测；
* 取消焦点后按键由 Qt 交给窗口本身（没有焦点控件时的回退目标），所以第二次
  `Esc` 仍能到 `Window.keyPressEvent`；
* 按键先到焦点控件：搜索栏这类自己消费 `Esc` 的控件（先收建议列表、再收
  搜索栏）优先，没被消费的 `Esc` 才沿控件链冒泡到窗口；
* binder **从不接管** `Esc`（见「真实按键」第 4 条）：和弦进行中按 `Esc`
  只放弃前缀，按键照旧交给控件 / 窗口走上面这张表。

## 焦点链（Tab / 方向键遍历）

Qt 的 Tab 链只看焦点策略，**不看控件是否画了焦点指示器**；Qt 6 起方向键
也会沿同一条链移动焦点。于是「不可见但可聚焦」的容器会变成陷阱：焦点走到
上面，用户看到的就是「焦点不见了」。

约定：

* **滚动容器不进焦点链**：`Album.scroll_area` 与 `IngestSource.scroll_area`
  （含 viewport）都设成 `NoFocus`。它们只是容器，落在上面的控件滚动可见性由
  Qt（`ensureWidgetVisible`）负责，滚轮 / 列表自身的方向键照常工作；
* **表单顺序显式给出**：控件创建顺序 ≠ 视觉阅读顺序，所以导入源详情页的
  Tab 链由 `IngestSource._setup_tab_order` 串起来：
  `关闭 → 应用 → 元信息 → 导入源 → 触发器（定时 → 设备插入）`，表单内部
  顺序来自 `IngestSourceDetailWidget.tab_order()`；
* 只读列表（标签 / 文件类型）保留在链上（可 Tab 选中），只是禁掉编辑；
* **可勾选分组**（导入源页的「定时更新 / 设备插入触发器」）：Qt 的方向键
  焦点导航不处理 `QGroupBox`，走进去就出不来。表单在
  `IngestSourceDetailWidget._group_key_press` 里补上进出口语义 ——
  `↓ / →` 进第一格，`↑ / ←` 退回分组之前；分组没勾选（子控件禁用）时
  `↓ / →` 直接跳到分组之后的下一个焦点停点，不会卡住。分组的勾选本身
  空格与回车都能切换（回车由激活键过滤器改写成空格）。

## 尚未接入的部分

| 缺口 | 现状 |
| --- | --- |
| 真实按键 | **已接通**：`keymap/qt_binder.py` 的 `ShortcutBinder` 在界面 boot（phase 5）装到 `QApplication` 上，单键 / 修饰键组合 / 和弦（1.2s 超时）都能触发 |
| 视图 / presenter 接线 | **已接线**：`ui/presenter/shortcuts.py` 登记 18 个槽，`ui/bootstrap.py` 的 phase 5 调 `install_ui_shortcuts()` 并 `load_shortcuts()`；仍是按鸭子类型用容器里的注册表（`keymap` 不反向 import presenter） |
| 默认表与代码的对应 | **已对上**：18 条命令都有槽；`load()` 之后 `unbound_slots()` 恰好是随包表里 `= []` 的 6 条；`tests/test_ui_shortcuts.py` 断言「`UI_SLOTS` 的命令集合 == `shortcuts.ini` 的命令集合」且「只有那 6 条没有键位」 |
| 键位编辑 UI | 设置页已接回主窗口，并有一个可编辑的「快捷键」分组（命令 → JSON 绑定记录）；按键映射编辑页尚未实现：没有 Qt 行模型、没有录键控件、没有冲突提示、没有「恢复默认」按钮。`conflicts()` / `unbound_slots()` / `binding_for()` 是留给它的查询接口；**只读**的查看入口已经有了（`albusctl ui shortcuts`，见上文，底层是 `shortcut_config_info()`），缺的是编辑与落盘那一半（槽的 `help` 文案现在是英文短句，设置页出现时再走 i18n） |
| 提示未来的组件标记 | `ui/widgets/widget/collapsible_group_box.py` 与 `ui/widgets/view/tile_view.py` 设的 Qt 属性 `keymap_passthrough` 由 `keymap/qt_binder.py` 读取（规则 5：空格 / 回车让渡给控件） |
| 命令层接口 | `albuswall/command/registry.py` 的 docstring 有 `TODO(ui)`：快捷键绑定归界面层，命令注册表不提供接口 |
| 设置写回 | **已接入**：`shortcuts.ini`（默认表 + 用户表）由 `setup_config` 用 `reg()` 登记，值走 `Resolver`，写回走 `ShortcutRegistry.save_shortcut()` / `persist()`，恢复默认走 `Configue.reset()`。`[shortcuts]` 的 key 是运行期才知道的，所以**不**出现在 `setting.schema` 的自动树里（它没有 `@configclass` 字段）；只读视图是 `albusctl ui shortcuts` |
| 热重载 | **已接通（进程内）**：`ShortcutRegistry.bind_config(config)` 订阅配置变更，`set` / `reset` / 重新 `load()` 都会重建绑定表。**外部改文件**仍然要重启（`QFileSystemWatcher` 没做——仓库没有 watcher 基础设施，task 明确不新造） |
| 菜单里的键位提示 | `Menu` 的 `QAction` 没有 `setShortcut`：菜单不显示键位，也没有把键位变化同步回菜单（`binding_for()` 是留给它的查询接口） |
| 输入法 / 非拉丁布局 | 组合输入期间的 `KeyPress` 不额外过滤（依赖「焦点在文本输入」那条放行）；无修饰键的字母在非 QWERTY 布局下拿到的是别的键码，所以默认表只用修饰键组合与功能键 |
