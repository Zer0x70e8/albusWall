# 界面导航（nav）

> 状态：**受控执行层**。注册 + 决策 + 受控执行（`FocusBackend`）+ 域内 Tab +
> 窗口接线（boot phase 3/3c）+ 分发器（`NavPresenter`）+ CLI 内省命令。
> 代码在 `src/albuswall/ui/nav/`（含 `wiring.py`、`backend.py`、`qt.py`），
> 分发器在 `src/albuswall/ui/presenter/nav_presenter.py`。测试在
> `tests/ui/nav/`（含 offscreen 集成测试 `tests/ui/nav/qt/`）与
> `tests/test_handlers_nav.py`。
> **主窗口的域 / 节点已经注册进真实界面（§11），按键也已经接上了**：方向键 /
> Tab / Enter / Esc 由 `NavPresenter` 装的应用级 `eventFilter` 分派，落点由
> `focus_node` 执行并**验证**，失败会回滚逻辑光标（§12）。
> 与 keymap 层的关系见 [`docs/nav/keymap-review.md`](nav/keymap-review.md)。

## 一句话模型

**注册域与节点 → 根据 `(page_id, 按键语义)` 返回「光标该落在哪个 Node」→
`focus_node` 执行并验证它真的落上去了；验证不过就回滚。**

## 模块分层

| 文件 | 职责 |
| --- | --- |
| `nav/__init__.py` | 公开 API（`__all__`）+ 用法 / 契约 / 分发器架构 docstring + 模块级便捷入口 `register_domain` / `register_node` / `register_from_slot` |
| `nav/model.py` | `Direction` / `GridPlacement` / `Node` / `Domain` / `NavPlacement` / `DomainFrame` / `PageState`；纯数据，**不 import Qt** |
| `nav/backend.py` | `FocusBackend` 协议 + `NullFocusBackend`（无 Qt 的默认实现）；**不 import Qt** |
| `nav/registry.py` | `NavRegistry`：域与节点注册 + `move` / `enter` / `exit_back` / `esc` / `shift_esc` / `jump_to_page_root` / `tab_next` / `tab_prev` 决策 + `focus_node` 受控执行 + `snapshot` / `restore` / `suspend` / `resume` + 进程级单例 |
| `nav/wiring.py` | 把主窗口控件树翻译成域与节点（含三档叶域让渡集常量）；**不 import Qt** |
| `nav/qt.py` | `QtFocusBackend`：本包**唯一** import Qt 的模块，只由包外分发器注入 |
| `nav/errors.py` | `NavError` 及其 7 个子类（`NavGridCollision` 是 v2 预留） |
| `ui/presenter/nav_presenter.py` | `NavPresenter`：应用级 eventFilter + `focusChanged` 回灌 + 模态暂停（**包外**，可以 import Qt） |

文档与被实现分离：本文讲**为什么**和**坑在哪**，逐方法说明在 docstring 里。
`tests/ui/nav/` 是可执行规范：行为与边界以它为准。

---

## 1. 这个模块是什么、不是什么

**是**：一个纯数据的决策表。注册域/节点 → 根据 `(page_id, 按键语义)` 返回「光标该落在哪个 Node」。

**不是**：
- 不 import 任何 Qt 类型（连 `TYPE_CHECKING` 下都没有 Qt；唯一例外是同包的
  适配器 `nav/qt.py`，它不被任何核心模块 import）
- 不直接调 `setFocus`：执行外包给 `FocusBackend`，核心模块只写
  `backend.apply_focus(node)` 并读回焦点链验证
- 不装事件过滤器、不接 `QShortcut`、不画 focus ring（拦按键的是包外分发器）
- 不持有 widget 生命周期（Node 里的 `widget` 是裸引用，nav 不管它死没死）

这条边界是硬约束。**任何让它 import Qt 或主动设焦点的 PR 都应该被拒**。理由：nav 决策必须能在无 GUI 的 pytest 里全量跑，这是测试矩阵能覆盖到栈语义的前提。

---

## 2. 三层数据模型

```
PageState (page_id)
  └─ stack: [DomainFrame, DomainFrame, ...]
       每个 frame = (domain_id, current: Node.id | None)

Domain
  └─ nodes: {node_id: Node}       ← 注册顺序 = 线性导航顺序
```

**关键不变量**：

| 不变量 | 破了会怎样 |
|--------|-----------|
| `Node.id` 全局唯一 | `_nodes` 平铺表会互相覆盖 |
| 每个 `Node` 只属于一个 `Domain` | `replace=True` 会从旧域移除，不允许多归属 |
| `PageState.stack` 只由 Registry 方法修改 | 外部改栈会绕过 `allow_exit` / `esc_clears_current` 判定 |
| `current=None` 恒等于「光标在虚拟根」 | 把所有 `None` 都当「无选中」会错判 `has_root=False` 的域 |
| `focusable=False` 的节点**永远不作为落点**（但可出现在 current） | 见 §5.1 |

`ensure_page` 建出来的栈恒为两帧：`[{page_id}::page_focus(None), root_domain(None)]`。
`::page_focus` 是「页面整体选中」态——`shift_esc` / `jump_to_page_root` 的终点，
也是调用方判断「要不要关页」的依据（`len(stack) == 1` 才是页面焦点）。

---

## 3. 域的两类：有根 / 无根（最容易搞混）

这个区分是整个模块最反直觉的设计，新维护者必须理解。

| | `has_root=True`（默认） | `has_root=False` |
|---|---|---|
| 语义 | 域内有「虚拟根」状态，`current=None` 表示「整个域选中，尚未进入具体子项」 | 没有虚拟根，`current` 永远指向某个子项 |
| Tab 进入 | 落 `entry` / 第一个 focusable | 同左 |
| Enter 进入 | **落 `current=None`**（停在虚拟根，等用户再选） | 同 Tab |
| 典型 | `page`、`form` | `navbar`、`list_view`、`tab_bar` |

**为什么要有无根域**：避免「根连根」。一个 navbar 本身不承载任何数据，把它当有根域会让 Enter 进去后「停在 navbar 上但没选中任何按钮」这种无意义状态。无根域的语义是「进入即等于选中默认按钮」。

**推论（写代码时依赖这两条）**：

1. 无根域 `entry` 必填 → 注册时给 warning，不阻塞（v1 允许 fallback）
2. `esc_clears_current=True` **只对 `has_root=True` 有意义**。无根域设了它，Esc 会把 `current` 置 `None`，下一帧 `_resolve_entry_target` 又落回 entry，造成 Esc 卡死。

---

## 4. 决策流水线

### 4.1 `move(direction)` 的优先级

```
1. direction_aliases 命中？
   ├─ "enter" → 调 enter(how="enter")
   ├─ "next"/"prev" → 按注册顺序线性移动
   └─ 其它 → 视为未命中
2. 网格扫描
   ├─ current=None → 从该方向边缘找第一个 focusable
   └─ current 有值 → 逐格扫描；cur.grid=None 时直接失败
3. 扫描失败 → direction ∈ exit_directions ？ exit_back() : return None
```

**为什么 alias 优先于网格**：navbar 这种纯线性组件没有网格语义，但外层可能有网格。alias 是「本域内的按键重映射」，优先级必须高于物理方向。

`next` / `prev` 的起点：`current=None` 时 `next` 从第一个 focusable、`prev` 从最后一个开始；`focusable=False` 的节点被跳过，不占序列位置。

### 4.2 `enter(how)` 的分支

```
A. current 是节点 + 有 child_domain
   ├─ how="enter" + 子域 has_root=True → 推入虚拟根（current=None）
   └─ 其它 → 推入，落 entry / fallback / 容器踏板
B. current 是节点 + 无 child_domain + how="enter" → 调 action
C. current=None
   ├─ how="enter" + has_root + root_action → 调 action，返回 None
   └─ 其它 → 落点：entry → 第一个 focusable → 第一个节点（三级 fallback）
              落点是容器（focusable=False + child_domain）→ 踏板
D. 无落点 → None
```

**A 分支的 `how="enter"` 特判是核心**：有根子域 + Enter =「进入但停在根上」，无根子域 + Enter =「进入并选中第一个」。这就是有根/无根在 Enter 路径上的体现。

**父帧 `current` 在任何路径下都不更新**：进入子域只 append 新帧，退出时靠父帧恢复进入前的值。这就是「Shift+Tab 弹回来光标还在原处」的实现方式，改这里必须保住。

### 4.3 返回 None 的语义（重要）

`None` 在这个模块里**至少 5 种含义**，调用方必须结合 `current_frame` / `stack` 判断：

| 方法 | `None` 的含义 |
|------|------------|
| `move` | 未移动（边界 + 非 exit） |
| `enter` | 落到虚拟根，或触发了 action 后返回 |
| `exit_back` | 栈深 ≤ 1，或 `allow_exit=False` |
| `esc` | `esc_clears_current` 命中（取消选中），或栈深 ≤ 1 |
| `shift_esc` / `jump_to_page_root` | 停在虚拟根 |

调用方（页面栈管理层）不应仅凭 `None` 判断「该关页」，必须看 `len(stack)`。

---

## 5. 已知陷阱

### 5.1 `focusable=False` 的三重身份

容易搞混，务必分清：

| 场景 | 是否可进 `current` | 是否 move/enter 落点 |
|------|----------------|-------------------|
| 作为域代表格（`list_view_container`） | 可以（Tab 推入、Esc 弹回时） | 否（跳过） |
| 作为踏板容器（有 `child_domain`） | 不进（`_push_child` 直接下钻） | 否（作为踏板被穿透） |
| 无 `child_domain` 的 `focusable=False` | 不进 | 否（落点失败，返回 `None`） |

**维护时检查**：任何改动 `_push_child` 或 `_resolve_entry_target` 的 PR，都要验证焦点不会卡在 `focusable=False` 节点上（回归测试：`test_push_child_focusable_false_no_child_returns_none`）。

### 5.2 容器踏板递归

踏板 = 目标落点 `focusable=False` 且有 `child_domain` 时，不推当前帧，直接递归到子域。

**设计约束**：
- 深度上限 `_MAX_PEDAL_DEPTH = 4`（防循环 A→B→A）
- 达到上限返回 `None`，**栈不增长**（踏板中途失败不推帧）
- 两条路径都要走踏板：C 路径（虚拟根进入）+ A 路径（从节点进子域）

「栈不增长」是靠**顺序**保证的：所有「不可落点」分支都在 `stack.append` **之前**返回。
这不是随手写的，是这个函数唯一的结构性约束——把 append 提到前面，失败就会留下半截子栈帧，后续 `esc` 弹出一层空帧。

**历史 bug**：早期版本只有 C 路径踏板，A 路径会让 `focusable=False` 节点 fall-through 到 `current`，光标死锁。已在 v1 修复，回归测试是 `test_pedal_A_path_entry_is_container`（A 路径）与 `test_pedal_C_path_entry_is_container`（C 路径）。

### 5.3 `direction_aliases` 的 `"enter"` 与 `move` 的间接调用

navbar 设 `{RIGHT: "enter"}` 时，`move(RIGHT)` 走的是 `enter(how="enter")` 而不是 `enter(how="tab")`。这意味着：如果当前节点有 `child_domain` 且子域 `has_root=True`，`move(RIGHT)` 会**落到子域虚拟根而不是第一个按钮**。

设计上是对的（Enter 语义），但如果有人期望「右键 = 快速前进」，可能需要重新审视 alias 定义。

### 5.4 `ensure_page` 的 root_domain 只认第一次

二次调用 `ensure_page("p1", root_domain="另一个")` 静默返回旧 state，仅 debug 日志。不会重新绑定根域。**这是故意的**：页面根域是声明式配置，运行时不应变更。

顺带一个后果：`{page_id}::page_focus` 域是 `ensure_page` **首次**调用时创建的，同一个 `page_id` 只会有一个，`clear()` 之后重建也没问题。

### 5.5 span > 1 在 v1 不生效

`GridPlacement.rowspan/colspan` 会注册成功，但 `_grid_scan` / `_scan_from` 全按 1×1 处理，注册时只有 warning。**用户如果给大按钮设 span，视觉上占了多格但导航只认左上格**。这是 v2 待补项（见 §7）。

### 5.6 叶域、`visible_fn` 与键位让渡（v1 补件）

三件东西共同支撑「列表内部由控件自治」这条边界（详见包 docstring 的定位节）：

| 补件 | 作用 | 陷阱 |
|---|---|---|
| `Node.visible_fn` | 返 `False` 的节点不参与 entry / 第一个 focusable / 网格扫描 / next-prev | 抛异常视为隐藏并 warning；**不阻止** `set_current`（外部权威，仅 warning）。临时隐藏**不要**用 `unregister_node`——会丢 `current` |
| `Node.pass_through_keys` | 声明让渡给控件的键（`NAV_UP`…`NAV_TAB`） | `set()` 是显式空集，**压过** `widget.keymap_passthrough=True`；`None` 才回退读控件标记。**按控件实际能吞的键声明，禁止一刀切全集**（见下） |
| `NavRegistry.should_pass_through` | 调用方在 `move` / `enter` / `esc` 之前必问 | nav 的动作方法**不**检查让渡——不先问就会把叶域的方向键抢走，或在标签树里把 Delete 顺走 |

**`keymap_passthrough` 的两种打标方式都认**：真实控件用的是 Qt 动态属性
`setProperty("keymap_passthrough", True)`（`virtual_scroll`、`collapsible_group_box`），
而 PySide6 的动态属性**不会**变成同名 Python 属性。只写
`getattr(widget, "keymap_passthrough", False)` 会永远读到 `False`，
实现见 `registry.py:_widget_flag()`。

**让渡集按控件实际能力声明，禁止一刀切全集。** 声明多了等于把 nav 的方向键
入口糊死（光标卡在控件里出不来）。主窗口的现成模板（`wiring.py`）：

| 常量 | 键 | 用在 |
|---|---|---|
| `LEAF_PASS_THROUGH_KEYS` | ↑ ↓ ← → + Enter | 滚动网格 / 列表：内容层、相册封面、回收站、导入源卡片、详情大图、缩略导航条 |
| `LEAF_PASS_THROUGH_HORIZONTAL` | ← → | 单行输入：标签栏（`tag_bar`）、导入源表单（`detail_widget`）——↑/↓ 归 nav 跳区块、Enter 归 nav 走 `action` |
| `LEAF_PASS_THROUGH_VERTICAL` | ↑ ↓ | 可纵向滚动的键值表：全屏信息面板——←/→ 归 nav |

### 5.7 `GridPlacement` 为什么不是 `typing.NamedTuple`

构造期校验必须发生在 `__new__`，而 Python 3.14 起 `typing.NamedTuple` 的子类里禁止在自定义 `__new__` 中调 `super().__new__`
（`TypeError: uses of super() and __class__ are unsupported in methods of NamedTuple subclasses`）。
所以实现是 `collections.namedtuple(...)` 派生 + 裸调 `_GridPlacementBase.__new__`：
保持不可变元组 / 可哈希 / 可解包，代价是多两行样板。**别「顺手」改回 `typing.NamedTuple`**，那会让整个包 import 失败。

### 5.8 硬接管的边界（能保证什么、不能保证什么）

**能保证的**：

- 跨区域焦点该落在哪个节点，**决策归 nav**（域栈语义），Qt 的焦点链
  （Tab 遍历、`focusNextPrevChild`、`setTabOrder`、点击取焦）不参与决策；
- Tab / Shift+Tab / 方向键 / 回车 / Esc 在 nav 管辖范围内被 nav 消费，不会被
  Qt 的焦点链接手（`_handle_tab` 任何分支都不冒泡）；
- 程序化 `setFocus` 只发生在 nav 自己决定落点的地方（`focus_node` →
  `apply_focus` / `focus_fn`），以及 `wiring` 之后 boot 的「首焦点」。

**不能保证的**：Qt 的**内部焦点管理**会程序化 `setFocus`，而程序化
`setFocus` 无视 `focusPolicy`、也绕过 `QFocusEvent` 拦截。已知会这么做的有
`QStackedLayout` 换页（把焦点转给新页）、`QTabWidget` 切页、`QDockWidget` 停靠
等。nav 的 `NoFocus` + 事件消费挡得住 Tab / 点击这条焦点链，**挡不住**这些内部
行为。这不是缺陷，是 nav 与 Qt 的边界（包文档「硬接管的验证语义」一节是全文
口径）。

**兜底机制**：事后探针。`NavPresenter._report_stray_focus` 检测到焦点落在未注册
控件上时 `logger.warning`（控件类型 + `page` / `domain` / `nav.current` 快照），
**不拉回、不改 nav 状态**。「闭环」的判据是**探针未触发**；探针触发即意味着出现
了 nav 未预见的 Qt 内部行为，处理方式是 case-by-case（给控件加 `NoFocus`、切页
前主动交出焦点、或改落点策略）——**不允许调松探针**。

**已处理的案例**：

| 场景 | 处理 | 位置 |
|---|---|---|
| 关 overlay 页时 `QStackedLayout` 把焦点转给空白占位页 | 切页前 `clearFocus()` 交出焦点（占位页本身就是 `NoFocus`，策略挡不住程序化 `setFocus`） | `widgets/passthrough_stack_widget.py::PassthroughStack._release_focus` |
| 关页之后焦点该去哪 | 经 nav 弹回根帧 + 落确定的区域入口，不直接 `setFocus` | `nav_presenter.py::NavPresenter._on_overlay_page_changed` |
| 容器节点被 Qt 焦点链碰到 | 容器永远 `NoFocus`；「容器与叶共用控件」时让位给叶 | `nav/qt.py::QtFocusBackend.ensure_focusable` + `is_landing_widget` |

**事前禁止（v2）**：要在 `QApplication` 层拦 `QFocusEvent`、对未注册控件
`ignore`。当前不做：代价大（要复刻 Qt 的焦点转移语义）、易碎（Qt 版本差异）、
可能破坏 IME / 无障碍，且不一定挡得住程序化 `setFocus`。见 §7。

---

## 6. 修改这里的决策 checklist

碰到以下常见任务时，按清单自检。

### 任务 A：新增一个导航域

```python
reg.register_domain(Domain(
    id="my_domain",
    has_root=?,              # 这个域有「整体选中」状态吗？
    entry=?,                 # 无根域必填
    allow_exit=?,            # Shift+Tab 能弹出吗？（list/form 类通常 False）
    is_real_page=?,          # Shift+Esc 要停在这吗？
    esc_clears_current=?,    # 仅 has_root=True 有意义
    exit_directions=?,       # 哪些方向越界时弹栈？
    direction_aliases=?,     # 本域内方向重映射
))
```

自检：
- [ ] 无根域是否给了 `entry`？没有会在切进域时落点不确定
- [ ] `allow_exit=False` 的域有没有配 `esc_clears_current` 或其它退出方式？否则用户会被困住
- [ ] 如果域内节点有 `grid`，是否检查过坐标碰撞？（v1 不检测，见 §7）

### 任务 B：新增一个导航节点

- [ ] `id` 全局唯一？
- [ ] 属于哪个域？`register_node(..., domain_id=...)` 是否传对？
- [ ] 是容器（有子域）还是叶子？
  - 容器：`focusable=False` + `child_domain=子域id`
  - 叶子：`focusable=True`（默认）
- [ ] 需要 Enter 触发动作？→ `action=`
- [ ] 需要自定义焦点？→ `focus_fn=`（否则走 `backend.apply_focus`，会被验证；滚动到可见也写在这里，nav 没有 `scroll_fn`）
- [ ] 是会临时隐藏的节点（预览模式、折叠、按需显示的按钮）？→ `visible_fn=`
- [ ] 是按钮？→ `wiring.py` 的 `_button_node` 已自动联动 `isEnabled`（disabled
      按钮不是落点），手写节点记得自己加
- [ ] 是叶域容器（列表 / 树 / 网格本身）？→ `pass_through_keys=` 按控件**实际
      能吞的键**声明：滚动网格用 `LEAF_PASS_THROUGH_KEYS`，单行输入用
      `LEAF_PASS_THROUGH_HORIZONTAL`，纵向滚动表用 `LEAF_PASS_THROUGH_VERTICAL`。
      **不要**一律塞全集——那会把 ↑/↓ 从 nav 手里抢走
- [ ] 域内 Tab 顺序不等于 `(row, col)`（比如视觉顺序与网格不一致）？→
      `Domain(tab_order=[...])`

### 任务 C：修改栈语义相关代码

**任何触及以下符号的 PR，必须跑完整 `tests/ui/nav/test_actions.py`**：

`_push_child` / `exit_back` / `esc` / `shift_esc` / `jump_to_page_root` / `_resolve_entry_target`

并要求 PR 描述里回答：
1. 父帧 `current` 是否仍保证「进入子域不更新」？
2. `focusable=False` 的节点是否仍不参与落点？
3. 新增路径的 `None` 返回值在 §4.3 的哪一类？

---

## 7. v2 待办（写在代码 TODO 里，别丢）

| 项 | 位置 | 说明 |
|----|------|------|
| 网格 span 出口 | `_grid_scan` / `_scan_from` | `rowspan` / `colspan` 覆盖的格子参与扫描 |
| 网格碰撞检测 | `NavGridCollision` | 注册时检测重叠，v1 只保留异常类 |
| 跨域 Tab 跳跃 | `tab_next` / `tab_prev` | v1 到域末尾返回 `None`（不跨域），分发器只消费不移动 |
| 跨域跳跃 | `move` | 网格边缘跨域而非只能 `exit_back` |
| 非网格场景 | 新协议 `NavScenario` | 线性流 / 树 / 时间轴接入路径未定 |
| `switch_page` 页面列表 | `switch_page` | v1 返回 `(page_id, page_id)`，实际切换未实现 |
| `jump_to_page_root` 键位 | 调用方 | 方法已实现，未绑快捷键 |
| `unregister_*` 级联 | 注册表 | v1 不级联清理 `child_domain` / `PageState` 引用 |
| 硬接管的「事前禁止」 | 未来：`QApplication` 层 | 拦 `QFocusEvent`、对未注册控件 `ignore`。代价大 / 易碎 / 可能破坏 IME 与无障碍，且不一定挡得住程序化 `setFocus`；当前用事后探针（§5.8） |

**已实现（从 v2 待办移出）**：叶域类型仍留待 v2；`FocusBackend` 受控执行（B）、
模态 `suspend` / `resume`（C3）、域内 Tab 语义（D）、焦点执行验证与回滚、
`snapshot` / `restore` 见 §12。

---

## 8. 测试策略

三层结构，加新功能时按「测试金字塔」往下补：

**`test_model.py`**（数据结构）
- 构造 / 默认值 / 校验异常。改动 `Node` / `Domain` / `GridPlacement` 字段时补这里。

**`test_registry.py`**（注册/查询，无状态机）
- 增删查、重复检测、`replace` 语义、warning 触发（`caplog`）。
- 每个 warning 都要有对应测试，否则 warning 会悄悄消失。
- 容器登记（`registry_nav_registry`）也在这里：只验证「登记了单例」，不验证加载时机。

**`test_actions.py`**（决策语义，核心）
- 按 §4 的决策流水线逐条覆盖。
- **每个已知陷阱（§5）必须有至少一个回归测试**，PR 里的 bug fix 必须带对应新测试。
- 场景 fixture（`page → list_view → form`）是公共基线，新功能应复用它而非重建。
- 进 navbar 这类「子域由容器节点推入」的用例，用 `_setup_navbar(reg, host=...)` 指定容器节点挂哪个域——挂错域会在 `set_current` 阶段就以 `UnknownNavNode` 炸掉。

**`test_backend.py`**（受控执行，无 Qt）
- 假后端覆盖「验证成功 / 失败 + warning / 观测不到焦点 → 乐观成功 /
  `focus_fn` 直通」；`last_action_ok` 的四种情形；`suspend` 对六个动作的
  惰性；`snapshot` / `restore` 三类（含重建被弹掉的帧）；
- 另有一条**结构**用例：用 AST 静态断言 `model` / `registry` / `errors` /
  `backend` / `wiring` 里没有任何 Qt / shiboken import。这条红了就说明
  「nav 核心不 import Qt」的硬约束被破了。

**`test_tab.py`**（域内 Tab）
- 网格排序 vs `tab_order` 严格覆盖、跳过隐藏 / 不可聚焦、虚拟根落入口、
  隐藏 current 回退、`suspend` 惰性、不跨域；外加两条主窗口接线验收
  （`album` / `detail_hud` 的完整 Tab 链）。

**`qt/`**（offscreen 集成，需要 `QApplication`）
- `test_qt_backend.py`：后端四方法的真实行为（真焦点 / 空链乐观 / NoFocus
  提升 / 解析器与销毁容错）+ 注册表端到端验证；
- `test_dispatcher.py`：后端注入与 `stop` 还原、方向键落点与真实焦点一致、
  `focus_node` 失败回滚且不消费、Tab 进域 / 域内遍历 / 末尾消费、Shift+Tab
  回退再出域、焦点跑到地盘外只告警不拉回、挂起时 nav 动作返回 `None`；
- `test_blur_update_keeps_focus.py`：叠层自隐藏刷新（`BlurLabel._update_blur`）
  之后焦点仍在页内、`nav.current` 仍在同一域；
- `test_root_shift_tab_consumed.py`：根帧 Shift+Tab 被消费（焦点头不动、无
  未注册控件拿到焦点），页内 Shift+Tab 正常弹回父帧；
- `test_container_focus_policy.py`：容器节点永远 `NoFocus`（唯一例外是
  容器与叶共用控件的 `window.content`），区域入口在 nav 侧仍可落点；
- `test_focus_changed_no_pullback.py`：杂散焦点只 warning、不拉回、不改 nav；
- `test_leaf_focus_fn_not_installed.py`：普通叶节点不装 `focus_fn`（走
  `apply_focus` + 验证），复合叶节点仍装；
- `test_overlay_gates_content.py`：叠层可见时被盖住的内容层不可落点；
- `test_focus_lands_within_nav.py`：结构性断言——`focus_node` 成功时焦点必须
  落在 nav 认知内的控件上；
- `test_passthrough_page_no_focus.py`：关 overlay 页后焦点由 nav 决定落点
  （不在 `_PassthroughPage` 等占位页、落在注册节点上、光标离开被关的页域），
  且不触发 `_report_stray_focus` 的 warning；
- `test_is_landing_widget_isolation.py`：`is_landing_widget` 判定纯注册表
  （`ast` 审查，零 Qt 名字）+ 注入链路（presenter → `QtFocusBackend`）。

**覆盖率底线**：`registry.py` 的方法级覆盖 ≥ 95%。低于这个数意味着某条决策分支没人走过。

---

## 9. 与其它模块的契约

**向上（调用方 / 分发器）**：
- 启动时调 `ensure_page(page_id, root_domain=...)` 注册页面（主窗口由
  `register_window_nav` 代劳，boot 随后落首焦点）
- 按键事件 → 调 `move` / `enter` / `exit_back` / `esc` / `shift_esc` /
  `jump_to_page_root` / `tab_next` / `tab_prev`
- 拿到返回的 `Node` 后调 `focus_node(node)`（返回 `bool`）；失败时用
  `snapshot()` / `restore()` 回滚。模态期间用 `suspend()` / `resume()`
  （分发器另有 `_is_blocked()` 兜底：挂起 / 模态 / 弹窗时直接放行；统一
  suspend 留 `TODO(v2)`，见包文档契约 1），或用 `last_action_ok` 判断这一下
  有没有进展
- 根据 `restore_page(page_id).stack` 长度判断是否关页
- **overlay 关页后的焦点落点由分发器经 nav 决定**：`PassthroughStack` 切页前
  先把焦点从旧页交出去（`_release_focus`，否则 Qt 的 `QStackedLayout` 会把焦点
  显式转给新页——占位页也会被程序化 `setFocus` 命中，`NoFocus` 策略挡不住），
  分发器订阅 `overlay.currentChanged`，收到空白层时 `exit_back` 回根帧并把焦点
  交给确定的区域入口（内容层 → 标题栏兜底），全程不直接 `setFocus`

**横向（keymap 层）**：
- 单向依赖：nav → keymap 只在 `TYPE_CHECKING` 下 import `ShortcutSlot`；运行时零 import
- `register_from_slot(slot)` 是**鸭子类型**，按属性取、缺了就当没有：
  - 节点 id：先 `slot.slot_id`，再 `slot.id`（`keymap.ShortcutSlot` 目前用的是后者）
  - 导航声明：`getattr(slot, "nav", None)`；没有 `nav` 字段 = 该槽不参与导航，返回 `None`
- `NavPlacement` 是声明载体：`@shortcut_slot(nav=NavPlacement(...))` 之后由调用方逐个
  `register_from_slot`（nav 不会自己去遍历 keymap 注册表）
- **契约测试建议**：keymap 侧改 `ShortcutSlot` 字段名的 PR 应触发 nav 侧的集成测试
  （`test_register_from_slot_accepts_slot_id_attribute` / `test_register_from_slot_without_nav_attribute`）

**向下（Qt）**：无。任何引入都是越界。

**与 keymap 的协作**：`docs/nav/keymap-review.md` 是这一侧的完整审查（事件分发
链路、`keymap_passthrough` 归属、模态 suspend 建议）。一句话：keymap 只吃修饰键
组合，nav 只吃导航键，两者都在「控件自治」处短路让渡。

---

## 10. 一页速查

```
类 Domain 的开关，按直觉填：
  有「整体选中」状态？        → has_root=True
  无根且怕落空？            → 必填 entry
  Shift+Tab 不让弹？         → allow_exit=False
  Shift+Esc 要停在这？       → is_real_page=True
  Esc 先取消选中再弹？       → esc_clears_current=True（仅 has_root=True）
  左右键要换成线性？         → direction_aliases
  到边界要弹栈？             → exit_directions

方法调用的兜底规则：
  move 返回 None    → 没动（看是不是边界）
  enter 返回 None   → 触发了 action 或落在虚拟根
  exit_back 返回 None → 被 allow_exit 卡住或栈到底
  esc 返回 None     → 取消了选中或栈到底

改代码前问自己：
  这条路径有没有触及 focusable=False 节点？
  父帧 current 有没有被意外改写？
  新的 None 返回值属于哪一类？
```

---

## 11. 窗口接线：`nav/wiring.py`（boot phase 3）

`register_window_nav(window, registry=...)` 把主窗口的控件树翻译成域 / 节点。
三条自我约束：**不 import Qt**（只按属性名访问控件）、**不改 `ui/window/` 一行
代码**、**可重复调用**（域名 / 节点 id 固定 + `replace=True` + 页面栈复位）。

接线点是 `albuswall/ui/bootstrap.py::setup_ui` 的 **phase 3**（3a 容器登记 →
3b 窗口结构），它由 `plugins/builtin_ui` 的 `init` hook 在 `init` 头部调用：

```
select_loop → 候选工厂只造被动主循环（ui_app）
init        → setup_ui：语言 → 主窗口 → [3a registry_nav_registry,
                                          3b register_window_nav] → presenter
loop        → show_ui：显示容器里的 main_window
```

| 域 | 内容 | 关键声明 |
|---|---|---|
| `window`（根，`is_real_page`） | 三个区域入口：标题栏 / 内容层 / 当前 overlay 页 | overlay 三页**共用格 (2,0)**，靠 `visible_fn` 过滤 |
| `titlebar` | 6 个按钮（工具 3 + 窗口动作 3） | ←/→ = prev/next，↓ 越界弹栈 |
| `content` | 内容层虚拟滚动网格（叶） | 自管键盘 |
| `album` / `source` / `detail` | 三个页各自的按钮 + 叶控件 | ↓ 越界弹栈 |
| `detail_hud` | 详情页 5 个操作按钮 | — |
| `detail_panel` | 信息面板：关闭 + 键值列表 | `visible_fn = panel.isVisible()` |

两条叶域约定（§5.6 的实际用法）：

1. **列表 / 长内容自己做键盘**：内容层、相册网格、回收站、导入源卡片、详情大图、
   缩略导航条、信息面板键值表 —— 一个节点代表整个控件，声明
   `LEAF_PASS_THROUGH_KEYS`（方向键 + 回车让渡；**Esc / Tab 不让渡**，那两下归 nav）；
2. **复合控件用 `focus_fn` 指派内部焦点**：标签栏 → `tag_line_edit`，导入源表单
   → `title_line_edit`。

按钮节点的 `action` 一律是 `widget.click` —— 与「焦点在按钮上按回车」的 Qt 原生
行为一致，也顺带满足契约 5：`detail.trash_button` 那种「一控件两语义」的按钮，
点下去由控件自己的槽按状态分流，nav 不需要知道状态。

### overlay 关页落点（默认策略，非唯一正确落点）

overlay 页关闭时，分发器经 nav 决定焦点落点（`_on_overlay_page_changed`）：
先 `exit_back` 一路弹回页面根帧，再按下面的顺序挑落点 ——

1. 根帧上残留的光标仍然可见 → 就落它；
2. **内容层入口**：`window.content` 可见时进内容域、落 `content.body`（叠层收起
   后被揭开的那一层，也是用户视觉上「回到的地方」）；
3. **标题栏入口**：`window.titlebar`（内容层不可见时兜底，例如未来出现「关页后
   内容层仍被遮住」的场景）。

**这是默认策略，不是唯一正确落点。** 要改就改
`NavPresenter._on_overlay_page_changed` 里的落点顺序；`tests/ui/nav/qt/`
里锁 `content.body` 的断言是**策略锁定**，策略一变要跟着改断言。

为什么不直接 `setFocus`：落点必须由 nav 决定（`set_current` / `entry` /
`focus_node`），直接 `setFocus` 会绕过栈语义与验证，制造「逻辑光标与真实焦点
各走各的」——正是硬接管要消灭的状态。

实测（真实 `Window`）：**8 个域 / 35 个节点**（`nav domains` 里还会多出
`main::page_focus` —— 那是 `ensure_page` 自动建的页面焦点域），`Tab` 先落
`window.titlebar`，
再 `Tab` 进 `titlebar.album`，`↓` 落到第二行窗口动作按钮，`↑` 回到工具按钮。

## 12. 受控执行与分发器

三件东西各管一段，别让它们互相越界：

| 部件 | 只做这件事 | 在哪 |
|---|---|---|
| `NavRegistry` | 决策 + 状态：算落点、维护域栈与 `current`、判断落点是否可见、记录动作有没有进展（`last_action_ok`）、提供 `snapshot` / `restore` / `suspend` / `resume` | `nav/registry.py`（不 import Qt） |
| `FocusBackend` | 执行 + 观测：`apply_focus` / `ensure_focusable` 动焦点，`current_focus_chain` 把结果读回来，`widget_to_node_id` 支持回灌 | 协议在 `nav/backend.py`（不 import Qt），Qt 实现在 `nav/qt.py` |
| 分发器 | 编排 + 收口：拦按键、决定消费还是冒泡、失败时回滚、`focusChanged` 回灌、模态暂停 | `ui/presenter/nav_presenter.py::NavPresenter`（包外，可以 import Qt） |

**`focus_node` 的三条验证规则**（这就是「受控」的含义）：

1. 有 `focus_fn` → 调用它，**视为成功**（自定义执行可能落在控件后代上，
   nav 猜不出来；契约 4）；
2. 没 `focus_fn` → `backend.apply_focus`，再读 `current_focus_chain`，
   `node.widget` 在链上才算成功；
3. 链**为空**（无 WM / offscreen / 未激活）→ 乐观成功。观测不到不等于失败，
   否则无头环境里整条导航会退化成一直回滚。

**失败之后**：分发器用 `nav.snapshot()` / `nav.restore()` 把逻辑光标回滚到
动作前，打 `warning`，并**不消费**这次按键（冒泡，控件原生行为兜底）。
`restore` 能重建被弹掉的栈帧，只在「快照里的域已被反注册」时拒绝。

**为什么分发器不新增一个 `NavDispatcher` 类**：同一个 `QApplication` 上装两个
应用级 `eventFilter` 会让一次按键被分派两遍，`focusChanged` 也会被回灌两次。
本仓库里 `NavPresenter` 已经是这个角色，所以是加固它而不是复制它。

**为什么不能用 `QShortcut` 分方向键**：`QShortcut` 只在有窗口上下文时触发，
一旦启用就吃掉按键，没有「算完发现没落点再冒泡」这条路径；而叶域里滚动网格
必须能自己翻页。应用级 eventFilter 才能做到「先决策、有进展才消费」。

**Tab 的语义分工**（Phase D 是 Phase C 的前置）：

| 光标位置 | Tab | Shift+Tab |
|---|---|---|
| 虚拟根 / 容器节点（有 `child_domain`，含区域入口） | `enter(how="tab")` 进域 | `exit_back()` 出域（根帧上放行给调用方） |
| 域内的叶 / 按钮 | `tab_next()` 域内前进 | `tab_prev()` 域内后退，到头再 `exit_back()` |

域内顺序：`Domain.tab_order` 给出时严格按它，否则按 `GridPlacement(row, col)`
排序（无 grid 的节点排最后）。走到域末尾仍**消费**这次 Tab（Qt 焦点链已被
nav 接管），但 v1 **不跨域**。

**本批改造逐条对应**（便于 review）：

| 项 | 内容 | 位置 |
|---|---|---|
| A1 | 叶域让渡集按控件能力分三档（水平 / 垂直 / 全集） | `wiring.py` |
| A2 | 隐藏的 `current` 上 `enter` 不执行（不点隐藏按钮、不推隐藏容器） | `registry.py::enter` |
| A3 | `_sequential` 的 `current` 不在列表时按注册顺序回退，不再 `None` 卡死 | `registry.py::_sequential` |
| A4 | `titlebar` 去掉 `direction_aliases`（改用网格扫描）+ 按钮默认联动 `isEnabled` | `wiring.py` |
| A5 | 首焦点由 boot 决定：`setup_ui` phase 3c 落根域第一个可见入口 | `bootstrap.py` |
| B1 | `FocusBackend` 协议 + `NullFocusBackend` | `nav/backend.py` |
| B2 | `NavRegistry(backend=...)` / `set_backend` / `set_default_backend` | `registry.py` |
| B3 | `focus_node` 受控执行（返回 `bool`，验证失败 warning） | `registry.py` |
| B4 | `last_action_ok` + `snapshot` / `restore` + `suspend` / `resume` | `registry.py` |
| C1 | `QtFocusBackend`（`nav/qt.py`）与分发器侧执行语义 | `nav/qt.py` / `nav_presenter.py` |
| C2 | 焦点回灌：命中对齐、不可见跳过、不命中只 warning（不拉回） | `nav_presenter.py` |
| C3 | 挂起 / 恢复（presenter 计数 + 注册表页面级挂起）；模态统一 suspend 见 `TODO(v2)` | 两处 |
| C4 | overlay 关页后的焦点落点（视图侧交还焦点 + 分发器经 nav 重新落点） | `passthrough_stack_widget.py` / `nav_presenter.py` |
| D1 | `Domain.tab_order` | `model.py` |
| D2 | `tab_next` / `tab_prev`（v1 不跨域） | `registry.py` |
| D3 | `enter(how="tab")` 语义不变（仅进域入口） | `registry.py` |
| E | 包 docstring 契约 2 / 6 改写 + 受控执行与分发器架构章节 | `nav/__init__.py` |

## 13. 运行期核对：CLI 内省命令

跑起来的实例里核对注册数据的手段是命令层的只读命令（服务端命令，`albusctl`
转发过去执行）：

| 命令 | 作用 |
|---|---|
| `albusctl nav domains` | 所有域：`id / has_root / entry / allow_exit / is_real_page / is_page_focus / node_count` |
| `albusctl nav nodes` | 所有节点，按「域注册顺序 → 域内注册顺序」；`--domain=<id>` 过滤 |
| `albusctl nav pages` | 所有页面：栈深 / 栈顶域 / 栈顶 current（悬空标 `!dangling:`） |
| `albusctl nav page <page_id>` | 单页完整栈，逐帧标 `!missing_domain` / `!dangling_node` / `hidden` |
| `albusctl keymap slots` | 快捷键槽 + 一键多命令冲突 |

- 加 `--format=json`（或 `--json`）给脚本用；`--format json` 的空格写法也认。
- **注册表为空不是错误**：输出 `no domains registered` 之类，退出码 0。
- **容器里没有注册表**同样不是错误：输出「本进程没有注册表」提示。
  `registry_nav_registry(container)` 已由界面启动流程
  （`albuswall.ui.bootstrap.setup_ui`）在 UI boot 时登记，所以带 GUI 跑起来的
  实例里能直接查到；headless（没跑 UI boot）或 `registry_shortcut_registry`
  （keymap 侧仍未接线）仍然会看到这句提示。
- 命令只读：没有 `nav.set-current` 这类写入口。三态语义（`None` / `[]` / 集合）
  在 `nav nodes` 的 `pass_through_keys` 列里原样可见。

**依赖容器**：`registry_nav_registry(container)` 只做
`container.register("nav_registry", get_default_registry, returns=NavRegistry)`，
**不**建页面、不加载声明——注册时机属于界面启动流程。无 `.register` 的容器只 warning。

---

---

## 尚未接入的部分

| 缺口 | 现状 |
| --- | --- |
| 真实按键 | **已接入**：`ui/presenter/nav_presenter.py::NavPresenter` 装应用级 `eventFilter` 分派方向键 / Tab / Enter / Esc，落点经 `focus_node` 执行并验证（§12） |
| 视图 / presenter 接线 | **已接入**：`bootstrap.setup_ui` 的 phase 3 调 `register_window_nav`、phase 3c 落首焦点；presenter 树里的 `nav` 负责分派 |
| 页面栈管理层 | 判断关页、管理多页面生命周期的那一层不存在，`switch_page` 因此只能返回 `(page_id, page_id)` |
| 槽声明通道 | `keymap.ShortcutSlot` 目前**没有** `nav` 字段，也没有 `@shortcut_slot(nav=...)` 之类入口，所以 `register_from_slot` 在真实链路上恒返回 `None`；两边都还没改 |
| focus ring / 视觉反馈 | 不存在：`focus_node` 只调 `focus_fn` 或 `backend.apply_focus`，选中态渲染是调用方的事 |
| 依赖容器接线 | nav 侧已接线：`albuswall.ui.bootstrap.setup_ui` 在 UI boot（`init` hook）里调 `registry_nav_registry(container)`，所以 `albusctl nav ...` 在 GUI 实例里能查到注册表；keymap 的 `registry_shortcut_registry` 仍未接线（要接就是同一行），headless 进程仍然输出「本进程没有注册表」（退出码 0），见 §11 |
| 分发器 | **已落地**：`ui/presenter/nav_presenter.py::NavPresenter`（presenter 树里的 `nav`）装应用级 `eventFilter` + `focusChanged`，见 §12 |
| 页面生命周期 | 页面只有 `main` 一个；`unregister_*` 仍不级联，窗口销毁后注册表里会留下悬空 widget 引用（单窗口进程无实际影响）|

---

**放行标准**：如果你的 PR 让 §6 的 checklist 里任一勾变成问号，或让 §5 的任一陷阱失去回归测试，就该打回。
