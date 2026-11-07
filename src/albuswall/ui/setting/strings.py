"""设置页的**用户可见文案**（全部集中在这里）。

为什么集中
==========

设置页里原来散着 60 多条中文字面量（按钮 / 状态 / 空态 / tooltip / 预览表），
既没法接 i18n（`tr()` 无处可挂），也没法整体校对。现在只有这一个文件写中文：

* 视图 / 行 / 分组框 / 预览表 / 契约里的展示文案都从这里取；
* 以后接 i18n 只需要给这里包一层 ``tr()``（或换成可替换的 ``Strings`` 对象），
  不用回来 grep 半个包；
* 测试断言的中文仍然来自这里，所以行为不变。

约定
====

* 带占位的文案用 ``str.format``（``{n}`` / ``{detail}`` / …），调用方负责填；
* 不带占位的是常量，直接引用；
* 这里**只放展示文案**：键名、段名、字段名这些"数据"不进来。
"""

from __future__ import annotations

# 这里不写 __all__：全是平铺常量，任何名字都可直接引用。

# ============================================================================
# 页头 / 导航
# ============================================================================
HEADER_TITLE = "设置"
HEADER_SUMMARY = "{groups} 个分组 · {items} 项 · {files} 个文件 · {uncovered} 项未声明"
HEADER_SUMMARY_EMPTY = "尚未接收 dump"
READY = "就绪"
ADVANCED_TOGGLE = "显示高级项"
ADD_COMPONENT = "＋ 组件"
ADD_COMPONENT_TOOLTIP = "添加一个自定义组件分组（例如只显示 label 的「关于」）"
RELOAD_TOOLTIP = "请求外部重新提供 dump（视图不自己读配置）"
SCOPE_TOOLTIP = "按归属文件 / 未绑定 / 未覆盖过滤右侧内容"
WAITING_TREE = "等待 dump…"
NOTHING_IN_SCOPE = "此范围没有可显示的设置项"

# ============================================================================
# 提交按钮 / 计数
# ============================================================================
BUTTON_PREVIEW = "预览"
BUTTON_SAVE = "保存"
BUTTON_DRAFT = "存为草稿"
BUTTON_RETRY = "重试"
SUBMIT_COUNT = "{mode}：{count} 项"
DIRTY_COUNT = "{count} 项未提交"

# ============================================================================
# footer / 状态
# ============================================================================
FOOTER_VALIDATION = "{count} 项校验未通过"
FOOTER_UNPLACED = "；{count} 项无法定位"
FOOTER_PENDING = "{count} 项未落盘"
FOOTER_PENDING_DETAIL = "（{detail}）"
FOOTER_PREVIEW = "预览：{changed}/{total} 项会变更（未写入）"
FOOTER_SAVED = "已保存"
FOOTER_SAVED_FILES = "已保存（{count} 个文件）"
FOOTER_SUBMIT_FAILED = "提交失败"
STATUS_SYNCED = "已同步外部变更"
STATUS_RELOADED = "已重载"
STATUS_DIRTY = "有未提交的修改"
STATUS_RESET = "已重置到默认值（未提交）"
STATUS_VALIDATION_FAILED = "校验未通过"
STATUS_PENDING = "未落盘"
STATUS_PREVIEW_DONE = "预览完成"
STATUS_SUBMIT_FAILED = "提交失败"
STATUS_SAVED = "已保存"

# ============================================================================
# 预览表
# ============================================================================
PREVIEW_TITLE = "变更预览"
PREVIEW_HINT = "{count} 项待写入（dry-run，不会改动任何文件）"
PREVIEW_COLUMNS = ("键", "旧值", "新值", "文件", "变更")
PREVIEW_YES = "是"
PREVIEW_NO = "否"
PREVIEW_CLOSE = "关闭"

# ============================================================================
# 范围 / 可编辑原因
# ============================================================================
SCOPE_ALL = "全部"
SCOPE_UNBOUND = "未绑定"
SCOPE_UNCOVERED = "未覆盖"
SCOPE_ALL_TOOLTIP = "全部已声明且能推出归属文件的叶子"
SCOPE_UNBOUND_TOOLTIP = "推不出归属文件（target_file 为空）的叶子"
SCOPE_UNCOVERED_TOOLTIP = "文件里有、设置树枚举不到的节点（只读）"

REASON_FIXED = "该项声明为固定值（editable=False），不能在界面修改"
REASON_READONLY = "值来自只读文件，不能在界面修改"
REASON_READ_ERROR = "当前值读取失败，先修复配置文件后再编辑"
REASON_INTERPOLATED = "值来自 ${...} 插值，只能编辑配置里的原始字符串"
REASON_UNBOUND = "推不出归属文件（target_file 为空），提交时无处可写"

# ============================================================================
# 叶子行
# ============================================================================
BADGE_ADVANCED = "高级"
BADGE_DEFAULT = "默认"
TOOLTIP_DIRTY = "有未提交的修改"
TOOLTIP_DEFAULT = "配置里没有这个键，当前显示的是声明默认值"
TOOLTIP_RESET = "重置为声明默认值（只改界面，提交时才生效）"
TOOLTIP_BROWSE = "选择文件或目录"
TOOLTIP_UNCOVERED = "未声明节点：设置树枚举不到，视图只读展示"
HINT_NO_TARGET = "无归属文件"
HINT_USING_DEFAULT = "使用默认值"
HINT_INTERPOLATED = "含 ${...} 插值，仅显示原串"
HINT_READ_ERROR = "读取失败：{detail}"
PICK_FILE = "选择文件"
PICK_DIRECTORY = "选择目录"
CHOICE_UNKNOWN = "⚠ {value}"

# ============================================================================
# 分组框 / 合成分组
# ============================================================================
GROUP_COUNT = "{visible} 项"
GROUP_COUNT_OF = "{visible}/{total} 项"
GROUP_ADVANCED = "高级"

UNBOUND_TITLE = "未绑定"
UNBOUND_DESCRIPTION = (
    "这些叶子推不出归属文件（target_file 为空），提交时无处可写，因此只读。"
)
UNCOVERED_TITLE = "未覆盖"
UNCOVERED_DESCRIPTION = "文件里有、但设置树枚举不到的节点；视图只做只读展示。"
UNCOVERED_META_TYPED = "typed"
UNCOVERED_META_UNTYPED = "untyped"
UNCOVERED_META_NO_TARGET = "无归属文件"

# ============================================================================
# 行右键菜单 / 杂项
# ============================================================================
MENU_MOVE_TO_GROUP = "移动到分组…"
MENU_RESET_POSITION = "恢复默认位置"
VALIDATION_FALLBACK = "校验失败"
COMPONENT_UNSUPPORTED = "[不支持的组件 {kind}] {text}"
TOOLTIP_UNCOVERED_LOCK = "未声明节点，不能在这里编辑"

# ============================================================================
# 再处理对话框（认领 / 移动 / 添加组件）
# ============================================================================
READ_ERROR_FALLBACK = "读取失败"
DETAIL_RAW_VALUE = "原始值：{value}"
DETAIL_FILE = "文件：{path}"
JSON_INVALID = "JSON 无效：{message}（第 {line} 行）"

DIALOG_MOVE_TITLE = "移动配置项"
DIALOG_MOVE_HEADING = "把 <b>{key}</b> 移动到："
DIALOG_MOVE_TARGET = "目标分组"
DIALOG_MOVE_ORDER = "组内顺序"
DIALOG_ADD_TITLE = "添加组件分组"
DIALOG_ADD_KEY = "分组 id"
DIALOG_ADD_TITLE_LABEL = "标题"
DIALOG_ADD_DESCRIPTION = "说明"
DIALOG_ADD_ORDER = "顺序"
DIALOG_ADD_LABEL = "分组里显示的 label 文本"
DIALOG_NEW_GROUP_OPTION = "新建分组…"
DIALOG_EMPTY_OPTION = "（不新建）"
DIALOG_DEFAULT_TITLE = "关于"
DIALOG_NEW_GROUP_ID = "新分组 id"
DIALOG_NEW_GROUP_ID_PLACEHOLDER = "新分组 id，例如 appearance"
DIALOG_NEW_GROUP_TITLE = "新分组标题"
DIALOG_NEW_GROUP_TITLE_PLACEHOLDER = "显示名，例如 外观"
DIALOG_NEW_GROUP_DESCRIPTION = "新分组说明"
DIALOG_NEW_GROUP_DESCRIPTION_PLACEHOLDER = "可选说明"
DIALOG_NEW_GROUP_ORDER = "新分组顺序"
DIALOG_GROUP_TARGET = "目标分组"
DIALOG_REQUIRE_GROUP = "请选择目标分组，或填写新分组 id"
DIALOG_REQUIRE_KEY = "分组 id 不能为空"
DIALOG_REQUIRE_LABEL = "label 文本不能为空"
