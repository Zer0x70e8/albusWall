# keymap 层审查：nav 集成的可行性与冲突点

> 面向：同时维护 `albuswall.ui.keymap` 与 `albuswall.ui.nav` 的人。
> 结论先给：**keymap 是一个纯查询表，不参与任何按键事件**——所以它和 nav 之间
> 既没有冲突，也没有现成的接线点；两者之间的那层**分发器**现在已经落地
> （`ui/presenter/nav_presenter.py::NavPresenter`，见 `docs/nav.md` §12），
> `qt_keys.py` 就是它用的第一块砖。

审查范围：`src/albuswall/ui/keymap/{__init__,model,registry,config,sequence,errors}.py`
（+ 本仓库第一次读它的 `keymap_passthrough` 相关方）。证据均为 `文件:行号`。

---

## 1. `ShortcutSlot` 字段契约

**结论：不能直接用 `register_from_slot`——字段名不符且缺 `nav` 字段。**

实际字段（`keymap/model.py:88-93`）：

```python
class ShortcutSlot:            # frozen dataclass，model.py:73
    id: str
    handler: Handler
    help: str = ""
    default_keys: tuple[KeySequence, ...] = ()
    default_args: tuple[Any, ...] = ()
    default_kwargs: Mapping[str, Any] = field(default_factory=dict)
```

- 槽 id 的字段名是 **`id`**，不是 `slot_id`。
  `slot_id` 是 `ShortcutRegistry` 的**方法形参名**（`registry.py:140`
  `register_slot(..., slot_id: str, ...)`、`registry.py:186` `get_slot(slot_id)`），
  容易被误当成字段名——nav 早期设计稿正是这么假设的。
- **没有 `nav` 字段**，也没有 `NavPlacement` 类型的导入（`keymap/model.py` 全文
  不含 `nav`）。

nav 侧的实际处理（已落地）：`registry.py:_slot_node_id()` 按
`slot_id → id` 顺序取，`register_from_slot()` 用 `getattr(slot, "nav", None)`
取声明，没有该字段即视为「该槽不参与导航」。

**与 nav 的关系：互补（nav 已适配）。** 建议（不落地）：keymap 侧将来若要
「一条快捷键声明自带落点」，加 `nav: NavPlacement | None = None`，`NavPlacement`
类型只在 `TYPE_CHECKING` 下引用，保持 keymap 不反向依赖 nav 的运行时。

---

## 2. 与 `keymap_passthrough` 的关系

**结论：全仓此前无人读它；nav 的 `should_pass_through` 是第一个读者。**

写入点（两处，都用 **Qt 动态属性**，不是 Python 属性）：

| 位置 | 代码 |
|---|---|
| `widgets/virtual_scroll/basic.py:52-58` | `setFocusPolicy(StrongFocus)`；`self.setProperty("keymap_passthrough", True)`；注释：「网格自己消费方向键 / Home / End / 回车：外部若接入全局快捷键处理器，看到这个属性就不应再抢走无修饰键」 |
| `widgets/collapsible_group_box.py:28-30` | `self.setFocusPolicy(StrongFocus)`；`self.setProperty("keymap_passthrough", True)`；注释：「Enter / Space 由本控件处理，不被全局 QShortcut 抢走」 |

读取点：**审查前为零**。`grep -rn keymap_passthrough src tests` 在审查前只命中
这两个写入点 + `docs/keymap.md` 的一句「当前没有任何代码读它」。
keymap 层内**没有任何** `QShortcut` / `QKeyEvent` / `eventFilter` /
`keyPressEvent`（`grep -rn "QShortcut\|QKeyEvent\|installEventFilter\|keyPressEvent" src/albuswall/ui/keymap` 为空）。

**⚠ 审查中发现的实际缺陷（已在本轮修在 nav 侧）**：PySide6 的
`setProperty("k", True)` **不会**让 `widget.k` 可读——

```
widget.setProperty('keymap_passthrough', True)
widget.property('keymap_passthrough')  → True
getattr(widget, 'keymap_passthrough', False) → False（属性不存在）
```

所以只写 `getattr(widget, "keymap_passthrough", False)` 的实现，面对**真实控件**
永远读到 `False`，叶域让渡整套静默失效（`virtual_scroll` 与
`collapsible_group_box` 会重新被 nav 抢走方向键）。
nav 现在用 `registry.py:_widget_flag()`：先 `widget.property(name)`，再退回
`getattr`，两种打标方式都认；测试
`tests/ui/nav/test_actions.py::test_pass_through_reads_qt_dynamic_property`
锁住这条。

**与 nav 的关系：互补，语义一致。** 两处注释的意思都是「这个控件吃无修饰键」，
与 nav `DEFAULT_PASS_THROUGH_KEYS`（up/down/left/right/enter/esc/tab）一致。
建议：`keymap_passthrough` 的**唯一读者保持为 nav**（keymap 不读），因为只有
nav 知道「当前是哪个控件拿到了焦点」；keymap 是静态绑定表，没有 `current`
概念。若将来 keymap 也要让渡，应复用同一个属性名与同一套判定，不要各写一份。

---

## 3. 无修饰键 vs 修饰键

**结论：keymap 没有「是否抢无修饰键」的策略——它根本看不到按键。**

- keymap 的输入是**规范化字符串**（`KeySequence`，PortableText），不是
  `QKeyEvent`；唯一的触发入口 `trigger(keys: str | KeySequence)`
  （`registry.py:450`）要求调用方**已经**把按键拼成了 `"Ctrl+F"` 这样的文本。
- 它区分形态的能力只到内省层面：`KeySequence.modifiers`
  （`sequence.py:166-173`）、`kind` / `is_single_key`（`sequence.py:176-187`），
  形态枚举的文档明确「只用于内省 / 文档 / 冲突提示，不参与匹配」（`sequence.py:62-63`）。
- 因此「注册了无修饰键的快捷键就抢」这个问题在 keymap 层不存在；它会不会被
  无修饰键触发，取决于**将来的分发器**把哪些键喂进 `trigger()`。

`resources/config/shortcuts.ini` 里确实有单键绑定（如 `navigation.grid = ["G, G"]`、
`content.search = ["Ctrl+F"]` 的和弦写法），但同样只是数据。

**与 nav 的关系：无冲突，但需要分布规则。** 建议（下一轮）：
分发器里按
`modifiers 非空 → keymap.trigger(pkey文本)`；
`无修饰键（或仅 Shift）→ 先问 nav.should_pass_through → 否则 nav 动作 → 否则冒泡`。
`KeySequence.is_single_key` 可作为这条判断的现成工具。

---

## 4. 模态嵌套事件循环

**结论：keymap 没有 suspend/resume，也不需要——因为它没有安装任何
`QShortcut` / 事件过滤器，模态期间它天然「不在链路上」。**

证据：

- `keymap/__init__.py:56-62`：「本包**只做注册**：不 import `QtWidgets`、不安装
  事件过滤器、不接管真实按键——把键位接到 `QShortcut` / 事件过滤器上是另一个
  独立层的事。」
- 全仓 `QShortcut` 只出现在 `window/utils.py:58-60`（`install_close_button` 的
  可选 `shortcut` 参数，且**内置界面一律不传**，见 `window/utils.py:8` 的注释
  「以免和全局键位系统抢 Esc」）。
- 因此 `QDialog.exec` / `QMenu.exec` / Popup / `QFileDialog` 期间，keymap
  **不会**响应任何东西——没有「主窗口快捷键仍然活着」的问题。

**与 nav 的关系：互补，nav 是唯一需要 suspend 的一方。** 建议：**不要**为
keymap 造 suspend API，只为「将来的分发器」造一个，由分发器同时尊重 nav 的
契约 1。理由：keymap 是查表，suspend 一个查表没有意义；真正会误触发的是
「分发器 + nav 动作」这条链路。

---

## 5. 快捷键与 nav 动作的编排（当前分发链路）

**结论（审查当时）：分发链路是空的——没有 `keyPressEvent` 总入口，也没有
`QShortcut` 注册，keymap 与 nav 都没有被接线到真实按键。**
（**现已补齐**：`ui/presenter/nav_presenter.py::NavPresenter` 就是下面建议的
那个分发器，落点与顺序按本节实现，见 `docs/nav.md` §12。）

实测：

| 检查项 | 结果 |
|---|---|
| `src/albuswall/ui/` 里的 `keyPressEvent` | 只有 3 处控件自治实现：`widgets/virtual_scroll/basic.py:664`、`widgets/tag_chip_view.py:80`、`widgets/collapsible_group_box.py:100` |
| 窗口 / `QApplication` 级按键入口 | 无（`ui/application.py` 不含 `eventFilter` / `KeyPress`；`ui/window/window.py` 无 `keyPressEvent`） |
| `QShortcut` 注册 | 无（除 `window/utils.py` 里从未被启用的可选参数） |
| keymap 的 `trigger` / `invoke` / `load_shortcuts` 调用方 | 仓库内**只有** `tests/test_ui_keymap.py` |
| nav 的调用方 | 仓库内**只有** `tests/ui/nav/` |

现有的 `installEventFilter` 全是**别的用途**，容易被误认成按键分发，逐条记清：

- `presenter/main.py:400` —— 收 `LanguageChange` 重算文案（`main.py:411-415`）；
- `widgets/action_buttons.py:70`、`widgets/blur_overlay_label.py:66,108` —— 跟随窗口几何 / 重绘；
- `widgets/floating_search_bar.py:209-210` —— 给 line edit 做 Esc / 上下键 / 回车（**控件自治**，见 `floating_search_bar.py:445-468`）；
- `window/titlebar.py:383` —— 把滚轮事件透传给 `Content`；
- `window/source/core.py:119`、`window/tagbar.py:191` —— 各自处理 Menu 键 / Shift+F10 / F2 / Delete / Insert。

也就是说：**键盘输入今天完全落在 Qt 的默认行为 + 三个控件自己的
`keyPressEvent` + 若干控件内的 eventFilter 上**，与 keymap / nav 都无关。

**nav 当时应插入的位置（建议，已按此实现）：**

```
QKeyEvent
  └─ 未来的分发器（安装点建议：Window.keyPressEvent 或 QApplication 级 eventFilter）
       ├─ 1. 焦点控件带 keymap_passthrough → 直接冒泡（两者都不抢）
       ├─ 2. modifiers 非空 → keymap.trigger(portableText)  命中则消费
       ├─ 3. key = qt_keys.qt_event_to_nav(event)
       │     └─ nav.should_pass_through(page, key) → 命中则 event.ignore() 冒泡
       ├─ 4. 方向键 → nav.move(page, Direction(key))        命中则消费
       │     Enter → nav.enter(page, how="enter"); Tab → how="tab"
       │     Esc → nav.esc(page); Shift+Tab → nav.exit_back(page)
       └─ 5. 都不处理 → super().keyPressEvent(event)（控件自治）
```

注意第 3 步必须在第 4 步之前：`should_pass_through` 是 nav 的**显式契约**，
不先问就会出「叶域里方向键被抢走、光标卡死」与「标签树 Delete 被 esc/exit 顺走」
两类事故（见 nav 包文档契约 6）。

---

## 6. 是否已有 Qt 键值 → 字符串转换工具

**结论：没有，需新增；已落 `keymap/qt_keys.py`。**

- `keymap/sequence.py` 做的是**字符串 ↔ 规范文本**（`canonical_sequence()`，
  `sequence.py:95`，内部用 `QKeySequence`，`sequence.py:112`），不处理
  `Qt.Key` 枚举；`keymap/` 下不含任何 `Qt.Key_` 字面量。
- 全仓 `Qt.Key_*` 的使用都是**就地比较**，没有映射表：
  `widgets/virtual_scroll/basic.py:676-697`、`widgets/floating_search_bar.py:461`、
  `widgets/tag_chip_view.py:81`、`widgets/collapsible_group_box.py:101-105`、
  `window/tagbar.py:267-288`、`window/source/core.py:277-279`。
- 新增：`src/albuswall/ui/keymap/qt_keys.py` —— `qt_key_to_nav()` /
  `qt_event_to_nav()`，只映射 nav 认识的 7 个键（+ 小键盘 Enter、Shift+Tab 的
  `Key_Backtab`），不看 modifier，其余返回 `None`。测试
  `tests/ui/keymap/test_qt_keys.py`（18 项），其中一条断言映射字面量与
  `albuswall.ui.nav.model.NAV_*` 逐一对齐，防止两边悄悄漂移。

---

## nav + keymap 协作建议

### 1) 事件循环里的调用顺序

**`keymap（仅修饰键）→ nav（仅导航键）→ 控件冒泡**，并且两条前置短路：
「焦点控件带 `keymap_passthrough`」和「`nav.should_pass_through` 命中」都直接
冒泡，不进入任何一侧。

理由：keymap 管的是**用户显式配置过的组合键**（`Ctrl+Q`、`F11`、`G, G`），
nav 管的是**焦点位置的可预测移动**。前者是「显式意图」，后者是「默认行为」，
所以显式优先；而两者都必须给「控件自己吃键」让路，否则 `virtual_scroll`
的方向键和 `collapsible_group_box` 的空格就没了。

### 2) `keymap_passthrough` 的读取归属

**约定：唯一读者是 nav 的 `should_pass_through`（+ 将来分发器的第 1 步短路）。**
keymap 不读它。理由见 §2：keymap 没有 `current`，无法判断「现在焦点在哪」；
而分发器的第 1 步只是为了省一次 `should_pass_through` 调用，判定逻辑仍以 nav
为唯一实现。

同时必须写死一条：属性**两种打标方式都认**（Qt 动态属性 + 同名 Python 属性），
见 §2 的 PySide6 陷阱。

### 3) 模态 suspend 是否需要统一 API（已按建议实现）

**已做成「分发器级」的一个**：`NavPresenter.suspend()/resume()/suspended()`
（可重入），并自动识别 `QApplication.activeModalWidget()` /
`activePopupWidget()` —— `QDialog.exec` / `QMenu.exec` / `Popup` 期间整层不分派。
keymap 侧仍然没有、也不需要 suspend（它是查表，没有事件入口）。

- keymap 无状态、无事件入口，suspend 无从谈起；
- nav 需要的是「模态期间别再路由」，这可以由分发器在 `QDialog.exec` 前
  `install` 一个标志位承担，nav 契约 1 已经写明「调用方不得调动作方法」；
- 如果将来两侧真的都需要冻结，建议由**同一个对象**（分发器）同时持有
  `keymap` 与 `nav` 的引用，`suspend()/resume()` 只改这个对象的内部标志，
  谁也不要各自维护一份。

---

## 待办去向

| 事项 | 去向 |
|---|---|
| `ShortcutSlot` 加 `nav: NavPlacement \| None`（`TYPE_CHECKING` 引用） | keymap 下一轮 PR，需先定「槽自带落点」是否值得 |
| 分发器（`Window.keyPressEvent` 或 app 级 eventFilter） | 新增层，落地时按 §5 的 5 步顺序 |
| 无修饰键的分布规则（keymap 只吃修饰键组合） | 分发器 PR 内实现，用 `KeySequence.is_single_key` |
| 模态 suspend | 分发器 PR 内实现，单一标志位 |
| keymap 读 `keymap_passthrough` | **不做**（见 §2 归属约定） |

---

## 7. CLI 查看命令（v1 收尾）

两个注册表都没有分发器可依赖，所以先给命令层加了只读内省命令。落点：
`src/albuswall/service/command/handlers/nav.py` 与 `keymap.py`（+ 共用渲染层
`_render.py`），在 `handlers/__init__.py::MODULES` 里排在 `builtin` 之后。

### 命令清单

| 命令 | 参数 | 输出 |
|---|---|---|
| `nav.domains`（`nav domains`） | `--format=json` | `id / has_root / entry / allow_exit / is_real_page / is_page_focus / node_count`；JSON 另含 `esc_clears_current` / `direction_aliases` / `exit_directions` / `parent` |
| `nav.nodes`（`nav nodes`） | `--domain=<id>`、`--format=json` | `id / domain / focusable / grid / child_domain / has_action / has_focus_fn / visible_fn_set / pass_through_keys`；顺序 = 域注册顺序 → 域内注册顺序 |
| `nav.pages`（`nav pages`） | `--format=json` | `page_id / stack_depth / current_domain / current_node`（悬空显示 `!dangling: x`） |
| `nav.page <page_id>`（`nav page p1`） | `--format=json` | 逐帧 `depth / domain_id / current / note`，note ∈ `!missing_domain` / `!dangling_node` / `!visible_fn_raised` / `hidden` |
| `keymap.slots`（`keymap slots`） | `--format=json`、`--verbose` | `id / help / default_keys / has_nav` + `conflicts` 段（一键多命令） |

命名形态：**点分命令路径 + 命名空间转发**，与仓库现有命令一致
（`asset.add-to-album` / `tag.alias-list` 就是这一套），所以是 `nav domains`
而不是 `nav-domains`；`register_namespace` 让 `nav domains` 与 `nav.domains`
走同一条绑定/翻译链路。

### 初始化契约结论（本仓库的实际情况）

1. **CLI 属于「独立子进程」**：`src/albuswall/cli.py` 是纯 IPC 客户端——只硬编码
   `version` / `help` / `-h` / `--version`，其余 argv 一律解析成
   `method + args + kwargs` 发给运行实例（`run_cli` → `_run_call` → `IpcClient`）。
   它**不 import 任何业务 / UI 代码**，因此客户端进程里不存在 nav / keymap 注册表。
2. **注册表在服务端进程**：内省命令注册在 `CommandRegistry`（运行实例这一侧），
   所以真正读表的是 GUI 进程。
3. **nav 已有初始化钩子，keymap 还没有**：`registry_nav_registry(container)` 由
   界面 boot 的 phase 3a 调用（`albuswall/ui/bootstrap.py::setup_ui`），随后
   phase 3b 的 `register_window_nav` 把主窗口的域 / 节点写进去 —— 所以正常启动的
   GUI 实例里，`albusctl nav domains` 能看到 §11 那 8 个域。
   `registry_shortcut_registry(container)` 至今**仍无调用方**（`docs/keymap.md`
   的「尚未接入」表里也这么写），于是 `keymap slots` 在同样条件下会提示未接线。
   命令体仍然懒取容器键：
   - 键缺失 → 返回一句「本进程没有注册表，先调用 registry_*_registry」，
     **退出码 0**（不是错误，无 UI 的进程本来没有导航层）；
   - 键存在但表为空 → `no domains registered` 等，**退出码 0**。
4. **没有硬塞假注册**：命令不构造任何 `Domain` / `Node` / `ShortcutSlot`，只读。

nav 侧的接线已经补上（boot phase 3，见 `docs/nav.md` §11）；keymap 侧还缺两步：
启动流程调 `registry_shortcut_registry(container)`，再由 UI 用
`@shortcut_slot(...)` 登记真实槽（当前一个都没有，默认表里的 13 条命令都没有对应
槽）。在此之前 `keymap slots` 输出「本进程没有注册表」是**预期**行为。

### 与 §3 分发顺序的关系

命令层内省**不改变**分发链路：事件循环里仍然是「keymap（仅修饰键）→ nav（仅
导航键）→ 控件冒泡」，而这条链路目前**还没有实现**（§5）。内省命令只是让
「注册了什么」可视，不提供任何触发能力（无 `nav-move` / `keymap-bind` 之类）。
