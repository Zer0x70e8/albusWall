# 快捷键系统（keymap2）

> 代码在 `../src/albuswall/ui/builtin/keymap`；配置只落在主 INI 的
> `[shortcuts]` 段；设置页入口是 `Settings → Key bindings`。

## 一句话模型

**一个键 → 一个或多个操作；操作按动作登记顺序（`seq`）执行。**
默认键位由代码里的 `@shortcut(keys=...)` 声明，默认值的唯一真相源就是代码。

## 分层

```text
内核（Qt-free）   model / components / bindings / config / dispatch / viewmodel / policy
    ↓ 只读快照
ViewModel         KeymapViewModel.snapshot() -> tuple[KeyRow, ...]
    ↓ dataclass
Qt Model（薄）    BindingModel + BindingProxy（QSortFilterProxyModel）
    ↓ delegate
View              QTreeView + KeyColumnDelegate + 对话框 + Help 面板
```

`import albuswall.ui.builtin.keymap` 在配置阶段、没有 PySide6 的环境里也
可以成功；Qt 名字（`attach` / `install_text_input_guard`）延迟导入。

## 默认键位（按主题）

动作声明分成两处，导入即登记：

* `ui/builtin/shortcuts.py` —— 窗口 / 焦点 / 搜索 / 详情页的既有动作；
* `ui/builtin/actions/` —— 按主题拆分的库级动作，`actions/__init__.py`
  被 `factory.load()` 导入。

### 移动 / 删除

| 按键 | 动作 | 说明 |
| --- | --- | --- |
| `Delete` | `asset.trash` | 移到系统相册《回收站》（软删，不是硬删） |
| `Ctrl+Shift+Delete` | `asset.empty_trash` | 清空回收站，不可逆，弹确认 |
| `Shift+1..9` | `asset.move_to_slot` | 移到快捷槽位相册（带 `slot` 参数） |
| `M` | `asset.move_target` | 移到当前目标相册；未设置时打开选择器 |
| `Ctrl+Shift+M` | `asset.move_to_album_picker` | 移到相册（总是弹选择器） |
| `Ctrl+Z` / `Ctrl+Shift+Z` | `asset.undo` / `asset.redo` | 撤销 / 重做移动、删除、恢复 |
| `Ctrl+C` / `Ctrl+X` / `Ctrl+V` | `asset.copy` / `asset.cut` / `asset.paste` | 内部剪贴板 |
| `F2` | `asset.rename` | TODO：无 rename 接口 |
| `Ctrl+E` | `asset.export` | TODO：无导出对话框 |
| `Ctrl+Alt+R` / `Ctrl+Alt+C` | `asset.reveal` / `asset.copy_path` | 在文件夹中显示 / 复制路径 |
| `Ctrl+Shift+N` | `asset.new_album` | TODO：相册写入接口未实现 |

“移动到相册”走 `album_assets` 成员关系，不复制磁盘文件；撤销会从目标相册
移除并把资产放回来源相册（来源是虚拟相册时无需回填）。

回收站是系统相册：**不可重命名 / 不可删除 / 不可作为快捷槽位目标**
（`library.rename_album` / `delete_album` / `can_be_slot_target` 统一拒绝）。
软删只写 `is_deleted + deleted_at`，保留 `album_assets` 成员关系，所以
“恢复 / `Ctrl+Z`”天然回到原相册；在回收站里再按 `Delete` 只提示
“Already in Trash”，不重复改语义。

### 快速预览 / 幻灯片（显式模式，不是宏）

| 上下文 | `Space` | 说明 |
| --- | --- | --- |
| 网格 / 列表 | 打开快速预览 | `QuickPreview.open(current)` |
| 快速预览模式 | 退出快速预览 | `QuickPreview.close()` |
| 普通详情页 | 播放 / 暂停 | 可播放资产才生效；否则无操作 |
| 幻灯片 | 暂停 / 继续 | 不退出查看层 |

| 按键 | 动作 | 说明 |
| --- | --- | --- |
| `K` | `preview.play_pause` | 播放 / 暂停，不清除快速预览模式 |
| `S` / `F5` | `preview.slideshow` | 显式进入 / 暂停幻灯片 |

快速预览复用详情页：进入时记录来源浮层、焦点控件与 HUD 可见性，隐藏
HUD（`Detail.overlay`：导航条 + 标签栏 + 操作按钮，以及顶部关闭按钮），
底部保留一条短暂提示（几秒后自动隐藏）。
退出时 HUD **按之前记录的可见性**恢复，焦点回到来源区域。

`Esc` 的显式模式层（`PresenterManager.escape_layer()`）：
**幻灯片 → 快速预览 → 详情页 → 信息面板 → 浮层**。

### 详情页补充

| 按键 | 动作 | 说明 |
| --- | --- | --- |
| `F` / `I` | `viewer.favorite` / `viewer.information` | 收藏 / 信息面板 |
| `E` | `viewer.edit` | 编辑子页；先清除快速预览模式 |
| `Delete` | `asset.trash` | 与全局一致 |
| `Shift+S` / `Ctrl+Shift+S` | `viewer.share` | TODO：无共享目标 |
| `Left` / `Right` | `viewer.previous` / `viewer.next` | 焦点在导航条 / 标签栏时不抢 |
| `T` | `viewer.tag_focus` | 聚焦标签输入；输入框聚焦时不抢 |
| `Ctrl+Shift+T` | `focus.tags` | 全局聚焦标签输入并全选 |
| `Ctrl+0` / `Ctrl+1` / `+` / `-` | `viewer.zoom_*` | 适应窗口 / 100% / 缩放 |

### 其余

| 按键 | 动作 | 说明 |
| --- | --- | --- |
| `F11` | `window.fullscreen` | 全屏切换 |
| `0..5` | `asset.rating` | TODO：assets 表还没有 rating 列 |
| `P` / `X` / `U` | `asset.flag` / `asset.reject` / `asset.unflag` | TODO：还没有 flag 列 |
| `?` / `Ctrl+/` | `window.help` | 从注册中心现读的快捷键帮助 |
| `F3` / `Shift+F3` | `search.next` / `search.previous` | 仅在有过滤 / 搜索结果时移动光标 |
| `Ctrl+Shift+F` | `filter.advanced` | TODO：高级筛选面板 |
| `Ctrl+Shift+L` | `filter.clear` | 清除标签过滤 |
| `Ctrl+A` | `selection.select_all` | 仅在有过滤时出现；多选模型 TODO |
| `Shift+方向键` | `selection.extend` | 多选模型 TODO |
| `Home` / `End` / `PageUp` / `PageDown` | 网格原生 | 不注册快捷键，避免和网格抢键 |

> `Ctrl+T` 不使用（避免和标签页 / 新标签混淆）；标签统一走 `T` 与
> `Ctrl+Shift+T`。

## 绑定策略

`keymap/policy.py` 是三条规则的唯一实现，设置页与 ViewModel 都调用它：

1. **系统保留键**：`delete` 只能挂 `asset.trash`，`ctrl+shift+delete`
   只能挂 `asset.empty_trash`；改绑到别的动作会抛 `ValueError`。
2. **修饰键要求**：`asset.move_to_album`（带相册参数）手工绑定时必须含
   `Ctrl` / `Alt` / `Meta`；`Shift` 不算。快捷槽位与 `M` 是默认动作，不受此限。
3. **冲突检测**：一个键可以挂多个操作（既定模型，按 `seq` 依次执行），
   设置页在录键与提交时都会提示“这个键已经运行哪些操作”。

“恢复默认”沿用既有入口：`Reset all`（页面按钮）与
`Registry.reset_action()` / `reset_key()`。

## 默认键可以携带不同参数

`@shortcut(keys=...)` 支持三种写法，最后一种让同一动作的默认键各带参数：

```python
@shortcut("demo.rating", description="rate",
          keys={str(v): {"rating": v} for v in range(6)},
          arguments={"rating": 0})
def rate(ctx, rating: int = 0) -> bool: ...
```

`Action.arguments_for(key)` 返回「动作参数 + 该键参数」；默认表、写盘比较
和设置页都走它，所以 `Shift+1..9`、`0..5` 这类键位在配置里是一条键一个值。

## 未落地能力（空接口 + TODO）

这些动作**已经注册、可以绑定、可以出现在设置页**，handler 只 toast
“not implemented（TODO）” 并返回 `False`，不会抛异常：

| 动作 | 缺口 |
| --- | --- |
| `asset.rename` | AssetRepository 没有改文件名 / 改库的写接口 |
| `asset.export` | 没有导出对话框与复制策略 |
| `asset.rating` / `asset.flag` / `asset.reject` / `asset.unflag` | `assets` 表没有 rating / flag 列（需要 schema 迁移） |
| `asset.new_album` | 相册写入 DTO / repo 尚未实现 |
| `viewer.share` | 没有共享目标 / 导出管线 |
| `selection.select_all` / `selection.extend` | 多选模型未实现（网格目前只有光标） |
| `filter.advanced` | 筛选条件模型未实现 |
| `asset.move_to_album_picker` / `asset.move_target` 的“未设置目标”分支 | 相册面板缺“选择模式”，目前只打开相册浮层 |
| 快捷槽位目标 / `M` 的目标相册 | 只在内存里，重启即丢；待落 `preference.ini` 并加设置下拉 |
| 幻灯片自动翻页 / 视频播放 | 只有三态壳（进入 / 暂停 / 退出），没有定时器与视频解码 |

## 声明动作

```python
from albuswall.ui.builtin.keymap import shortcut


@shortcut(
  "demo.hello",
  keys="Ctrl+Alt+H",  # 默认键（可给多个，或 {键: 参数} 映射）
  label="Say hello",
  description="Print a greeting.",  # 必填；缺省取 docstring 首段
  component="window.detail",  # 组件树里的位置（默认 "app"）
  scope="component",  # "component" | "global"
  arguments={"name": "world"},  # 该动作默认绑定携带的参数
)
def say_hello(ctx, name: str = "") -> bool:
  ...
```

* `description` 缺失且没有 docstring → `ValueError`；
* 参数在**写入绑定时**用 `inspect.signature(...).bind_partial(...)` 校验一次，
  运行期直接 `handler(ctx, **args)`；
* `when: Callable[[Context], bool]` 是唯一的可用性谓词机制；
* `allow_in_text_input` 逐动作控制输入框放行；
* `ctx` 上有 `window` / `container` / `presenters` / `focus`。

## 组件树

`keymap2/components.py` 的 `COMPONENTS` 是模块级常量，`component_path`
用 `while` 向上走。插件要加分组时导入期调用 `register_component(...)`；
不登记的动作统一落在 `"app"` 下。

## 分发

`QShortcut` 的创建与生命周期在 `qt_binder.py`，按键是否该触发由
`DispatchPolicy.allows(action, ctx)` 集中判断：

| 输入 | 规则 |
| --- | --- |
| 模态对话框打开 | 全部放行 |
| 正在 `QKeySequenceEdit` 里录键 | 全部放行 |
| 焦点不在本窗口 | 只有 `scope="global"` 的动作生效 |
| 焦点在文本输入框 | 只有 `global` 或 `allow_in_text_input=True` 的动作生效 |
| `action.when` 返回 False | 跳过这一条 |

总开关：`is_enabled()` / `set_enabled()`。
程序化触发：`trigger_action("window.escape", **args) -> bool`（不看 `when`）。
订阅：`registry.subscribe(listener)` / `subscribe_trigger(listener)` 都返回
取消函数。

## 配置

```ini
[shortcuts]
ctrl+q = window.quit
ctrl+p = ["window.open_page", {"action": "viewer.next", "args": {"page": "x"}}]
```

* 每条 = 键 → 操作列表；元素是 `"action_id"` 或 `{"action": …, "args": …}`；
* 内存里只维护一份「最终值」。写盘时与默认表比较：相同就删掉那一行；
* 用户清空一个键的所有操作 → 写一个空列表（`ctrl+q =`），表示显式解绑；
* 写盘时机：`keymap2.config.attach_persistence()` 订阅注册中心变更，UI 改键后
  立即写入（`save_to_config`）。
* 旧版 `keymap.json`（v1 的 `additions` / `removals` / `bindings`）不再读取，
  用一次性脚本迁移：

```bash
python -m albuswall.ui.builtin.keymap.tools.migrate_keymap \
    --keymap ~/.config/albuswall/keymap.json \
    --config ~/.config/albuswall/albuswall.ini
```

## 设置页

* 搜索框 → `BindingProxy.setFilterRegularExpression`，按键 token 与子操作
  token 都参与匹配；
* 改键 → `QStyledItemDelegate` 的 `QKeySequenceEdit` → `BindingModel.setData`
  → ViewModel → Registry（改键同样过 `policy.validate_binding`）；
* 改参数 / 删操作 → 双击操作行弹 `QDialog`（`build_form` 工厂生成表单）；
* 「Add」表单：按键 + 动作选择器（`QTreeView` + 静态 model）+ 参数表单；
  录键或提交时若该键已有操作，会显示“会一起触发”的冲突提示；
* 「Help」按钮打开只读帮助面板（与 `?` / `Ctrl+/` 同一实现）；
* 修改立即生效：`Registry._notify()` → ViewModel 拉数据 →
  `beginResetModel/endResetModel` → 视图重绘。
