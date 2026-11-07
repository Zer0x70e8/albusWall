# `albuswall.ui.setting` — 设置页被动视图

这份 README 只描述**契约**与**演示入口**；实现细节在各模块 docstring 里。

模块分工（按"谁改它"切）：

| 模块 | 职责 |
| --- | --- |
| `model.py` | 不可变设置树 + 索引 + 派生字段（唯一实现） |
| `contract.py` | 版本校验 / 归一化入口 / 展示判定（Scope / DisabledReason / SubmitMode） |
| `content.py` | 可见性 / 排序 / 合成分组（纯函数，不碰 Qt） |
| `strings.py` | 全部用户可见文案（i18n 只要包这一层） |
| `binder.py` | 一个叶子的初值 / 当前值 / dirty / 错误（纯 Python） |
| `editors.py` | 值类别 → 编辑器注册表（加控件只改这里） |
| `leaf_row.py` | 叶子行：标签 + 编辑器 + 重置 + 错误图标 |
| `group_box.py` | 可折叠分组（标题带计数） |
| `preview.py` | `dry_run` 变更预览表 |
| `nav.py` | 左侧锚点导航 + 两步焦点语义（`NavHost` 由视图实现） |
| `result.py` | 写回报告形状（demo / presenter / 真实写回共用） |
| `view.py` | 整页被动视图：外壳 / 范围 / 锚点 / 提交 / 回填 |
| `layout.py` | 展示层再处理（移动 / 自定义组件） |
| `dialogs.py` | 「移动到分组…」「＋ 组件」的对话框 |

## 1. 它是什么

一个只负责「画」的设置页：吃一份 dump、接变更通知、吐全量可编辑值。
它**不**读配置器、**不**写文件、**不**做类型推断、**不**求值 i18n、**不**
调度线程。写回层 / presenter 负责把数据递进来、把提交结果递回来。

```
presenter ──dump(schema_version=2)──▶ SettingView ──{key: typed_value}──▶ presenter
             on_node_changed(node,value,source) ──▶          ◀── show_submit_result(result)
```

## 2. 输入：dump → SettingTree

视图吃的是**不可变模型** `SettingTree`（`model.py`），不是裸 dict。两种喂法：

```python
from albuswall.configue.schema import dump_schema_dict
from albuswall.service.command.handlers._present import apply_present
from albuswall.ui.setting import normalize_dump

dump = apply_present(
    dump_schema_dict(config=container.get("config")),
    show_advanced=True,          # 交给视图的 show_advanced 决定折叠，别在 dump 里预先过滤
)
view.set_dump_schema(dump)       # 视图内部 normalize_dump(dump)
# 或者自己先归一化，避免重复解析：
view.set_tree(normalize_dump(dump))
```

`schema_version` 不认识 → 抛 `UnsupportedSchemaVersionError`。
归一化 / 派生字段（`by_file` / `unbound` / `summary` / `item_count`）**只有一份
实现**：`model.SettingTree`。视图与 presenter 都从树上的索引取东西：
`tree.leaves` / `tree.group(key)` / `tree.group_of(key)` / `tree.parent_of(key)` /
`tree.by_file` / `tree.unbound` / `tree.uncovered` / `tree.summary`。

视图读取的叶子字段：`key / path / label / description / value / raw /
interpolated / default / widget / constraints / choice_labels / advanced /
present / dirty / editable / readonly / read_error / type / choices`。
分组字段：`key / title / order / items / groups / item_count / editable`。

- `widget` 缺失时才用 `type + choices` 兜底（`common.setting_widget.infer_widget`，展示策略与契约共用同一份实现）；
- `interpolated=True` → 只读展示 `raw`；
- `editable=False` → 固定项；`readonly=True` → 只读文件；`read_error` → 读失败；
- `target_file is None` → 视图额外标记「未绑定」并禁用（写回层本来也写不了）。

## 3. 通知

```python
view.on_node_changed(("thumbnail", "batch_size"), 128, "set")   # set / remove / reset
view.on_node_changed(("ui", "theme"), "default", "remove")
view.on_reload()                                               # 整页重建（不自己 dump）
```

`source` 只接受 `set / remove / reset`（`reload` 等价于 `on_reload()`）。
**这两个方法可能在工作线程被调用**：视图内部用 Qt 信号排队到自己的线程，
调用方不需要自己切主线程。外部更新会刷新初值快照并清 dirty / 错误。

`on_reload()` 只按视图**当前持有**的 dump 重建；文件内容也变了时，presenter
应先调用 `view.set_dump_schema(new_dump)`。

## 4. 提交

```python
view.submit_requested.connect(handle_submit)   # 信号参数：(mode, {key: typed_value})
```

`mode ∈ {"dry_run", "collect_errors", "draft"}`，对应 footer 三个按钮。
`collect_values()` 返回**全量可编辑 binder**（与当前范围 / 高级开关无关）。

提交结果用 `view.show_submit_result(result)` 回填，`result` 是
`result.write_back_result(...)` 造出来的报告（唯一形状定义，真实写回 /
presenter 的错误分支 / 演示的假后端共用）：

```python
{
    "written": [...], "by_file": {...}, "applied": [...],
    "nodes": [["section", "key"], ...], "pending": ["[section]key", ...],
    "ok": True, "error": None,
    "validation_errors": [{"key": "group.item", "path": "[section]key", "message": "..."}],
    "changes": [{"key": ..., "old": ..., "new": ..., "text": ..., "file": ..., "changed": True}],
    "dry_run": False, "deferred": False,
}
```

- `validation_errors[].key` → 找到 binder → 行内 `⚠` + tooltip（不撑行高）；
- `pending` 非空 → footer「N 项未落盘」+ 重试按钮；
- `ok=True` → footer「已保存」；
- `dry_run` + `changes` → 视图自己渲染变更预览表。

## 5. 范围 / 高级 = 只切可见性

整棵树在 `set_tree()` 时建一次控件；`set_scope()` / `set_show_advanced()` **不
重建**，只切 `setVisible` + 重算侧栏锚点和分组标题计数。于是切范围从 ~74 ms
降到 ~2 ms，且不丢滚动位置 / 焦点（锚点选择仍按旧语义清空）。

## 6. 视图私有状态

`current_scope` / `current_anchor` / `show_advanced` / `binders` / `anchor_map`
（`anchor_map` 同时包含分组与叶子，外加 `__unbound__` / `__uncovered__`
合成分组；`uncovered` 的条目 key 形如 `__uncovered__:[section]key`）。
锚点按钮、跳转焦点框与两步焦点语义在 `nav.SettingsNav` 里，视图只实现
`NavHost` 的三个回调（`expand_chain` / `reveal` / `focus_target`）。

这些只通过 `set_dump_schema` / `on_node_changed` / `on_reload` /
`set_scope` / `set_show_advanced` / `select_anchor` 改变；外部不要直接摸
widget / binder。

### 跳转焦点框的两步语义

`select_anchor(key)` 不在第一次跳转时抢键盘焦点：

1. 第一次跳到某个锚点：在对应组件（叶子的编辑控件 / 分组框）上画一圈
   虚线焦点框，**不给键盘焦点**；
2. 再次跳到**同一个**锚点：如果焦点框在跳转前已经显示，就把真正的键盘
   焦点交给该组件，并隐藏虚线框（改用控件自己的 `:focus` 样式）；
3. 跳到别的锚点：重新从第 1 步开始。

目标不可聚焦时（只读 / 未绑定 / 未声明节点）保持虚线框，不强行
`setFocus()`。`jump_frame_target` / `jump_frame_visible` / `jump_focus_given`
是给测试 / presenter 的只读观测属性。

## 7. 演示

```bash
# 有桌面
python -m albuswall.ui.setting.demo

# 无桌面 / CI
QT_QPA_PLATFORM=offscreen python -m albuswall.ui.setting.demo --smoke
```

演示**不使用**应用现有框架（没有 `Presenter` 基类 / bootstrap / 容器），
只用 `demo.DemoSettingsPresenter` 冒充写回层与通知源：

- 顶部按钮：重载 dump / 外部 set / 外部 remove / 外部 reset / **子线程** set；
- 勾选「模拟写盘失败」→ 看 footer 的 pending + 重试；
- 把「强调色」改成 `abc`、或把「批大小」改成 `65`，再点「保存」→ 看行内
  `⚠` 与 tooltip；
- 点「预览」→ 看 `dry_run` 的变更表（视图自己的表格，不弹写回层对话框）。

演示数据在 `demo_data.py`，覆盖所有控件：text / int / float / bool / choice /
path / list / dict，以及高级项、只读、读失败、`${...}` 插值、未绑定、未覆盖。

## 8. 主窗口接入

设置页已经接回主窗口：

- 视图容器：`albuswall.ui.window.settings_page.SettingsPage`（`Window.settings_page`，
  放在主窗口的 `overlay` 浮层里，带与其它页一致的关闭按钮）；
- 数据 / 提交：`albuswall.ui.presenter.settings_presenter.SettingsPresenter`；
- 入口：标题栏「更多 → 设置」与 `app.settings`（`Ctrl+,`）都走
  `WindowPresenter.open_settings()` → `settings_requested` →
  `SettingsPresenter.open()`；
- 保存链路：`SettingView.submit_requested` → `MainPresenter.update_settings`
  （`collect_errors=True`；草稿 `flush=False`；预览 `dry_run=True`）→
  `SettingView.show_submit_result`；
- 语言变化后由 `SettingsPresenter.retranslate()` 重新 dump 设置树。

## 9. 再处理（layout）

设置页在 dump 与视图之间多了一层 **layout**（`layout.py`，展示层，不写回配置）。

它**不是配置文件**、也不落盘：布局是**容器注入的运行时对象**，由装配根提供：

```python
from albuswall.ui.setting import SettingLayout
container.set("setting_layout", SettingLayout())   # 装配根（presenter 工厂）里做一次
```

`SettingsPresenter` 通过 `self.service("setting_layout")` 拿到它（拿不到就退化为
空布局，headless / 单测照样能渲染）。生命周期跟着装配根走，和主题、窗口状态同一类
"注入进来就用"的东西；用户不需要编辑任何文件。

对象形态（`SettingLayout.to_dict()`，只作为纯数据的序列化视图）：

```json
{
  "version": 1,
  "moves": [{"key": "ui.theme", "group": "appearance", "order": 0}],
  "groups": [{"key": "appearance", "title": "外观", "components": []}],
  "claims": [{"section": "ui", "key": "custom_note", "group": "ui", "...": "..."}]
}
```

### 9.1 未覆盖节点（不暴露 UI 认领）

「认领」= 往**进程级 schema 登记表**里补一条声明，让写回层认识这个节点：

1. `register_setting_item(group, setting_item(...), replace=True)` 补声明；
2. `register_present_override(section, key, ...)` 补展示策略（控件 / 高级）；
3. `invalidate_schema()` 让下一次 `dump_schema_dict()` 重新生成设置树。

快捷键走的就是这条路（见 9.6，批量自动认领），未覆盖节点的手动认领入口已从
界面移除，`claim_node()` 只保留为内部能力。

### 9.2 修改位置

叶子行**右键任意位置 → 移动到分组…**（行内文本 / 徽标等非输入控件也接管了
这条菜单；输入控件保留自己的复制粘贴菜单）：

- 只改 dump 的树结构，叶子的 `key`（qualname）保持不变，因此写回键不变；
- 可移动到已有分组，或新建一个展示分组；
- 对**已声明**的普通配置项同样有效（不限「未声明 / 未覆盖」的节点）；
- **恢复默认位置** 撤销单条 move，落回**内置默认布局**的位置（见 9.3）。

### 9.3 内置默认布局（「主题」分组）

`layout.builtin_layout()` 给出一份默认摆放，presenter 用
`merge_layouts(builtin_layout(), 用户布局)` 应用：内置默认先摆，用户移动按
`key` 覆盖它。

当前内置默认：把 `[ui] theme`（主题名）与 `[path] theme`（主题目录）收进
「主题」分组（`order=50`，排在常识分组之后、快捷键/关于之前）。它是**展示层**
默认，不写回配置；用户可以用右键把它挪走，「恢复默认位置」后再落回这里。

### 9.5 添加自定义组件

- 页面右上角 **＋ 组件**：新增一个只含 `label` 的展示分组；
- 内置「关于」分组（版本 / 作者 / 配置目录）也走
  `groups[].components` 渲染，不绑定配置节点；
- 视图侧 `SettingView._build_component()` 目前支持 `type="label"`。

布局是**展示层**：移动 / 组件不改配置文件；只有「认领」会触碰配置器（因为它
必须让写回层认识那个节点）。

### 9.6 快捷键 groupbox（可编辑）

`SettingsPresenter._register_shortcut_settings()` 把 `[shortcuts]` 的每个命令
自动登记成一个普通设置项（`ShortcutRecord` 类型），挂进「快捷键」分组：

- 和普通分组一样是**可编辑行**，值是 JSON 记录文本，例如
  `["Ctrl+Q"]` / `["F11", "Ctrl+Shift+F"]` / `[]`（显式解绑）；
- 保存走统一写回链路，写入用户的 `shortcuts.ini`，快捷键注册表热更新；
- 默认值来自随包默认表，行内「重置」恢复默认键位；
- 不再作为 components label 展示，也不再出现在「未覆盖」清单里。

