# 快捷键系统（keymap）

> 状态：**只实现了注册层**。代码在 `src/albuswall/ui/keymap/`，默认键位表在
> `src/albuswall/resources/config/shortcuts.ini`，用户覆盖在
> `<user config dir>/shortcuts.ini`。**还没接到真实视图上，也没有设置页。**

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
| `keymap/sequence.py` | `KeySequence` / `parse_sequence(s)` / `canonical_sequence` / `format_sequences` / `ShortcutKind` / `MODIFIERS` / `CHORD_SEPARATOR` / `MAX_CHORDS`；本包**唯一** import Qt 的模块（`PySide6.QtGui.QKeySequence`，不碰 `QtWidgets`） |
| `keymap/model.py` | `ShortcutBinding`（键位 + 位置参数 + 关键字参数）、`ShortcutSlot`（`id` + `handler` + `help` + 兜底键位/实参）；两个 frozen dataclass，纯数据，不 import Qt |
| `keymap/registry.py` | `ShortcutRegistry`：槽注册、双向一对多绑定表、`load` / `persist`、与 `albuswall.command` 对接 |
| `keymap/config.py` | `ShortcutConfig`：默认表 + 用户表读 / 写、记录格式、原子写盘；`default_config_path()` / `user_config_path()` / `DEFAULT_FILE_NAME` / `DEFAULT_SECTION` |
| `keymap/errors.py` | `KeymapError` / `InvalidKeySequence` / `DuplicateShortcutSlot` / `ShortcutConfigError` |

`tests/test_ui_keymap.py` 是可执行规范：行为与边界以它为准。

`model.py` **不是** Qt 视图模型，只是两个 dataclass；仓库里没有
`QAbstractItemModel`、没有设置页需要的行模型。

## 默认键位（`src/albuswall/resources/config/shortcuts.ini`）

`[shortcuts]` 段，一条命令一行；13 条命令 / 15 个键位。分组是文件里的注释分组。

| 分组 | 命令 | 键位 |
| --- | --- | --- |
| application | `app.quit` | `Ctrl+Q` |
| application | `app.settings` | `Ctrl+,` |
| window / view | `window.fullscreen` | `F11`, `Ctrl+Shift+F` |
| window / view | `window.close` | `Ctrl+W` |
| window / view | `view.zoom_in` | `Ctrl++`, `Ctrl+=` |
| window / view | `view.zoom_out` | `Ctrl+-` |
| window / view | `view.reset` | `Ctrl+0` |
| navigation | `navigation.grid` | `G, G`（和弦，不是两个键） |
| navigation | `content.search` | `Ctrl+F` |
| albums / assets | `album.new` | `Ctrl+N` |
| albums / assets | `album.rename` | `F2` |
| albums / assets | `asset.favorite` | `Space` |
| albums / assets | `asset.delete` | `Delete`（规范化后是 `Del`） |

这些命令只是**数据**：仓库里没有任何代码把 `app.quit` 之类注册成槽，所以当前
`ShortcutRegistry().load()` 之后 `slots() == ()`，`trigger("Ctrl+Q")` 返回 `()`。

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
* 命令行内的键位书写要用 JSON 数组，不用逗号分隔——逗号是**和弦分隔符**。

### 两份文件与优先级

| 优先级 | 文件 | 计算函数 | 说明 |
| --- | --- | --- | --- |
| 1（高） | `<user config dir>/shortcuts.ini` | `user_config_path(file_name="shortcuts.ini")` = `get_user_config_dir(albuswall.__name__, albuswall.__author__) / file_name` | 可写，只存与默认表**不同**的条目（Linux 实测 `~/.config/albuswall/shortcuts.ini`） |
| 2 | `src/albuswall/resources/config/shortcuts.ini` | `default_config_path()` = `albuswall.resources.get("shortcuts")` | 只读，随包分发 |
| 3（低） | 槽装饰器的 `keys= / args= / kwargs=` | `ShortcutSlot.default_binding` | 只给配置里没有的命令补位 |

* 覆盖粒度是**命令**：`ShortcutConfig.load()` = `load_defaults()` 之后被
  `load_user()` 逐条 `update`，所以用户写 `album.new = []` 只清掉这一条；
* 用户文件缺失 = 全部走默认；
* `ShortcutRegistry.persist()` 只写差异：基线 = 随包默认表优先、槽兜底绑定兜底，
  与基线一致的条目从用户表里消失；没有 `user_path` 时返回 `None`；
* 还没 `load()` 就 `persist()` 只写本进程认识的命令，不会把默认表整片解绑；
* `ShortcutConfig.reset()` 删掉用户文件即恢复全默认；写盘是原子的（同目录临时
  文件 + `os.replace`），用户文件里的注释不保留（状态文件）。

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
指过来的槽，通常是「忘了在默认表里加一行」）。

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

## 尚未接入的部分

| 缺口 | 现状 |
| --- | --- |
| 真实按键 | 没有任何 `QShortcut` / 事件过滤器：`keymap/__init__.py` 明确「本包只做注册，不接管真实按键」。`sequence.py` 是唯一 import Qt 的模块，所以即使加载成功，按键也不会自己发生 |
| 视图 / presenter 接线 | `src/albuswall/ui/keymap/` 之外没有任何代码 import 它（只有 `tests/test_ui_keymap.py`，以及 `shortcuts.ini` 头部的注释提到它）；启动流程里也没有 `load_shortcuts()` |
| 默认表与代码的对应 | `shortcuts.ini` 里的 13 条命令都没有对应的槽登记，`load()` 之后 `unbound_slots()` 为空只是因为没有槽，`trigger` 对每条命令都跳过 |
| 设置页 / 键位编辑 UI | 不存在：没有 Qt 行模型、没有录键控件、没有冲突提示、没有「恢复默认」按钮。`conflicts()` / `unbound_slots()` / `binding_for()` 是留给它的查询接口 |
| 提示未来的组件标记 | `ui/widgets/collapsible_group_box.py:30` 与 `ui/widgets/virtual_scroll/basic.py:56` 设了 Qt 属性 `keymap_passthrough`，当前**没有任何代码读它** |
| 命令层接口 | `albuswall/command/registry.py` 的 docstring 有 `TODO(ui)`：快捷键绑定归界面层，命令注册表不提供接口 |
| 设置写回 | 应用处于重构阶段、设置 GUI 未恢复。`albuswall.service.command.handlers.setting`（`setting.schema` / `setting.set` / `setting.save`）只对 `configue` 管的配置（如 `[ui]`）做写回，**不**管 `shortcuts.ini`——快捷键文件由 `ShortcutConfig` 自己读写，不进 `configue` schema |
