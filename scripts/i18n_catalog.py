#
"""i18n 翻译词表（``scripts/build_i18n.py`` 的输入数据）。

这里是**纯数据**：源文 → 译文。控件 / presenter / 委托里的 ``tr("...")``
源文统一写**英文**；少数 lupdate 提取不到的动态文案（元信息面板的字段
标签）仍是**中文**数据。所以：

* ``zh_CN`` 只需要把**英文**源文翻成中文；
* ``en_US`` 只需要把**中文**源文翻成英文；
* 源文本身已经是目标语言时不用写条目，Qt 会回退到源文。

``MANUAL_CONTEXTS`` 是 lupdate 提取不到的部分（模块级 ``tr()`` /
``schema_tr()`` 的上下文常量），``schema_strings()`` 在构建时从设置树里
收集标题 / 标签 / 说明，避免手工维护一份会漂移的清单。

改译文：直接改这里，然后 ``python scripts/build_i18n.py --refresh``
（``--refresh`` 会用词表覆盖 ``.ts`` 里的旧译文；不加时只补缺失条目，
保留在 Qt Linguist 里做的手工修改）。
"""

from __future__ import annotations

from typing import Iterable

from albuswall.ui.languages import LANGUAGES as _LANGUAGES

__all__ = [
    "EN_US",
    "LANGUAGES",
    "MANUAL_CONTEXTS",
    "TRANSLATIONS",
    "ZH_CN",
    "schema_strings",
]

#: 需要生成翻译文件的语言代码（与 ``albuswall.ui.languages`` 保持一致）
LANGUAGES: tuple[str, ...] = tuple(language.code for language in _LANGUAGES)

#: lupdate 看不到的上下文 → 源文列表。
#: 上下文必须与源码里的翻译调用常量一致：
#: ``MetadataModel`` / ``InfoCellDelegate`` / ``SettingRow`` /
#: ``SettingRowEditor`` 见对应模块的 ``_TR_CONTEXT``；``SettingSchema``
#: 见 ``setting/translation.py``。
MANUAL_CONTEXTS: dict[str, list[str]] = {
    # ``self.tr(spec.label)`` 的标签来自模块级 ``FieldSpec`` 数据（lupdate
    # 提取不到动态参数）；「是 / 否」是字面量，已由 lupdate 从 ``tr_bool``
    # 提取成英文源文，不再登记。
    "MetadataModel": [
        "标题",
        "描述",
        "宽度",
        "高度",
        "文件大小",
        "MIME 类型",
        "拍摄时间",
        "加入时间",
        "修改时间",
        "删除时间",
        "已收藏",
        "已删除",
        "文件路径",
    ],
    "InfoCellDelegate": [
        # 源文已统一为英文（信息条不再依赖中文源文，英文界面不会漏出中文）
        "%1 days left",
        "Deleted today",
        "Expired",
    ],
    "SettingRow": [
        "contains a ${…} placeholder; resolved once the related items are set",
        "not written yet",
        "(none)",
    ],
    # 编辑器的上下文常量见 setting/editors.py 的 ``_TR_CONTEXT``
    "SettingEditors": [
        "(default)",
        "This item is read-only here.",
        "Expected an integer, got {text!r}",
        "Expected a number, got {text!r}",
        "JSON parse error: {message}",
        "A JSON object {…} is required here.",
        "Partially checked = not set explicitly (follows the default).",
        "(default directory)",
        "(default file)",
        "Select directory",
        "Select file",
        "Browse…",
        "(default / auto)",
        "Default: {value}",
    ],
    # 按键映射页：动作名 / 说明来自 registry（运行时字符串，lupdate 提取不到）
    "KeymapPage": [
        "Every action of the application and the key it is bound to. Click a "
        "key field and press the new key, or clear it to unbind. Changes are "
        "saved immediately.",
        "Close the current layer",
        "Close the topmost layer: suggestion list, search bar, information "
        "panel, detail view or overlay.",
        "Open settings",
        "Open the settings overlay.",
        "Open album list",
        "Open the album overlay and focus the album list.",
        "Open import sources",
        "Open the import source overlay.",
        "Open the More menu",
        "Open the title bar menu (import sources, settings, quit) and drive "
        "it with the arrow keys.",
        "Back / close overlay",
        "Close the current overlay and return to the album grid.",
        "Minimize window",
        "Minimize the main window.",
        "Toggle maximize",
        "Maximize or restore the main window.",
        "Quit albuswall",
        "Close the application.",
        "Focus search",
        "Expand the search bar and put the cursor in it.",
        "Close detail view",
        "Close the detail view (Esc also works).",
        "Previous image",
        "Show the previous asset while the detail view is open.",
        "Next image",
        "Show the next asset while the detail view is open.",
        "Toggle favourite",
        "Toggle the favourite state of the current asset.",
        "Toggle information panel",
        "Show or hide the metadata panel of the detail view.",
        "Trash / restore asset",
        "Move the current asset to the trash, or restore it when it is "
        "already trashed.",
        "Activate focused control",
        "Press the focused button (the keyboard equivalent of clicking it).",
        "Activate focused control (keypad Enter)",
        "Same as Activate focused control, for the numeric keypad Enter key.",
        "Focus next region",
        "Move the keyboard focus to the next area (title bar, list, grid, "
        "tag bar…).",
        "Focus previous region",
        "Move the keyboard focus to the previous area.",
        "Focus the image grid",
        "Put the keyboard focus on the thumbnail grid: arrow keys move the "
        "cursor, Enter opens the asset.",
        "Focus tag input",
        "Focus the tag input of the detail view.",
        "Enable key bindings",
        "When off, the configured keys are ignored (the built-in button "
        "shortcuts still work).",
        "Search actions or keys…",
        "Reset all",
        "Unknown action (not registered anymore): {id}",
        "Default: {value}",
        "customized",
        "Remove",
        "Only single-key shortcuts are supported; {value} has more than "
        "one key.",
        "Remove this leftover binding",
        "Reset to default: {value}",
        "(unbound)",
        "{shown} of {total} actions shown",
        "Key conflicts: {details}",
        "Saved to {path}",
        "Saved in memory only (the configuration system is not ready yet).",
    ],
}

# ══════════════════════════════════════════════════
# 英文 → 中文
# ══════════════════════════════════════════════════
ZH_CN: dict[str, str] = {
    # ---- 界面文案（self.tr 源文已统一为英文） ----
    "%1 days left": "剩余 %1 天",
    "(no other album yet)": "（还没有其它相册）",
    "A virtual root ingest source cannot be deleted": "虚拟根导入源不可删除",
    "Add": "添加",
    "Add to album": "添加到指定相册",
    "Add to the current image": "添加到当前图片",
    "Added": "已添加",
    "Added to “{album}”": "已添加到「{album}」",
    "Album": "相册",
    "Album actions are unavailable in the trash view": "回收站视图不支持相册操作",
    "Album title:": "相册标题：",
    "Apply": "应用",
    "Auto-mount (Linux only, requires permissions)": "自动挂载（仅 Linux，需要权限）",
    "Browse": "浏览",
    "Close": "关闭",
    "Close search bar": "关闭搜索栏",
    "Command mode": "命令模式",
    "Command mode (not implemented)": "命令模式（未实现）",
    "Command mode is not implemented yet": "命令模式尚未实现",
    "Confirm delete": "确认删除",
    "Could not add to the album": "未能添加到相册",
    "Delete": "删除",
    "Delete tag": "删除标签",
    "Deleted today": "今天清除",
    "Description": "描述",
    "Device insertion trigger": "设备插入触发器",
    "Disable": "禁用",
    "Edit": "编辑",
    "Enable": "启用",
    "Example": "示例",
    "Expired": "已过期",
    "Favorite": "收藏",
    "File type verification method": "文件类型校验方式",
    "File type(s)": "文件类型",
    "Import Source": "导入源",
    "Info": "信息",
    "Ingest Source": "导入源",
    "Interval time": "间隔时间",
    "Magic number check": "魔数检查",
    "Meta info": "元信息",
    "Mode": "模式",
    "More": "更多",
    "Mount point": "挂载点",
    "Move to Trash": "移入回收站",
    "Move to album": "移动到指定相册",
    "Moved": "已移动",
    "Moved to “{album}”": "已移动到「{album}」",
    "Name": "名称",
    "New": "新建",
    "New album": "新建相册",
    "New album and move into it…": "新建相册并移入…",
    "New child tag": "新建子标签",
    "New ingest source": "新建导入源",
    "New root tag": "新建根标签",
    "New tag": "新建标签",
    "No": "否",
    "No limit": "不限",
    "No matching tags": "无匹配标签",
    "No matching tags in the current album: {text}": "当前相册没有匹配的标签：{text}",
    "Parent tag is still in the trash": "父标签仍在回收站",
    "Quit": "退出",
    "Remove from this album": "从本相册移除",
    "Removed from this album": "已从本相册移除",
    "Rename album": "重命名相册",
    "Rename tag": "重命名标签",
    "Restore": "恢复",
    "Scheduled time": "定时时间",
    "Scheduled update": "定时更新",
    "Search": "搜索",
    "Select": "多选",
    "Select source directory": "选择源目录",
    "Simple mode": "简单模式",
    "Source path (library path)": "源路径（图库路径）",
    "Space to exit · K to play / pause": "空格退出 · K 播放 / 暂停",
    "Subfolder recursion": "子文件夹递归",
    "Subfolder recursion depth": "子文件夹递归深度",
    "Suffix check": "后缀检查",
    "Tag 1": "标签1",
    "Tag name; use / for hierarchy": "标签名，可用 / 分层",
    "Tag tree": "标签树",
    "Tags:": "标签：",
    "Target": "目标",
    "The album service is unavailable": "相册服务不可用",
    "The asset is not in the current album": "资产不在当前相册里",
    "The asset is not in the current album; nothing was moved": "资产不在当前相册里，未移动",
    "The file service is unavailable": "文件服务不可用",
    "The ingest source service is unavailable": "导入源服务不可用",
    "The parent tag does not exist": "父标签不存在",
    "The settings page is not implemented yet": "设置页尚未实现",
    "The tag does not exist": "标签不存在",
    "The tag has child tags": "标签含子标签",
    "The tag was restored, but its parent is still in the trash; it is currently orphaned.": "标签已恢复，但父标签仍在回收站，当前为孤儿状态。",
    "This feature is not implemented yet": "该功能尚未实现",
    "This tag has {count} child tags. Delete them as well?": "该标签下有 {count} 个子标签，是否连同子标签一起删除？",
    "Triggers": "触发器",
    "Type a tag name to filter the current album...": "输入标签名筛选当前相册...",
    "Untitled ingest source": "未命名导入源",
    "Yes": "是",
    "e.g., 30m or 1h": "例如 30m 或 1h",
    "“Trash” is not an album, so assets cannot be moved or removed": "「回收站」不是相册，无法移动或移除资产",
    # ---- 设置页外壳 ----
    "Settings": "设置",
    "Reload the setting tree (setting.schema)": "重新加载设置树（setting.schema）",
    "{count} settings": "{count} 项设置",
    "{count} groups": "{count} 个分组",
    "writes {count} files": "写入 {count} 个文件",
    "same structure as albusctl setting.schema": "与 albusctl setting.schema 结构相同",
    "No setting tree available (the config system may not be ready yet).":
        "没有可用的设置树（配置系统可能尚未就绪）。",
    "{key} updated": "{key} 已更新",
    "(This group failed to build; see the log for details.)":
        "（该分组构建失败，详见日志。）",
    # ---- 设置行 ----
    "the value comes from a read-only source": "该值来自只读来源",
    "this item is not editable": "该项不可编辑",
    "Written to:": "已写入：",
    "Written to the config": "已写入配置",
    "Reserved: this action is not wired yet.": "预留：该操作尚未接线。",
    "contains a ${…} placeholder; resolved once the related items are set":
        "包含 ${…} 占位符；相关项设置后会解析出具体值",
    "not written yet": "尚未写入",
    "(none)": "（无）",
    # ---- 设置编辑器 ----
    "(default)": "（默认）",
    "This item is read-only here.": "该项在此处只读。",
    "Expected an integer, got {text!r}": "需要整数，实际收到 {text!r}",
    "Expected a number, got {text!r}": "需要数字，实际收到 {text!r}",
    "JSON parse error: {message}": "JSON 解析失败：{message}",
    "A JSON object {…} is required here.": "此处需要一个 JSON 对象 {…}。",
    "Partially checked = not set explicitly (follows the default).":
        "半选 = 未显式设置（跟随默认值）。",
    "(default directory)": "（默认目录）",
    "(default file)": "（默认文件）",
    "Select directory": "选择目录",
    "Select file": "选择文件",
    "Browse…": "浏览…",
    "(default / auto)": "（默认 / 自动）",
    "Default: {value}": "默认值：{value}",
    # ---- 关于页 ----
    "About": "关于",
    "Title": "标题",
    "Version": "版本",
    "Author": "作者",
    "Thanks for using it.": "感谢使用。",
    # ---- 按键映射页 ----
    "Key bindings": "按键映射",
    "Every action of the application and the key it is bound to. Click a "
    "key field and press the new key, or clear it to unbind. Changes are "
    "saved immediately.":
        "应用的全部动作及其绑定的按键。点选按键输入框后直接按新键即可修改，"
        "清空则取消绑定。修改立即保存。",
    "Close the current layer": "关闭当前层",
    "Close the topmost layer: suggestion list, search bar, information "
    "panel, detail view or overlay.":
        "关闭最上面的一层：搜索建议、搜索栏、信息面板、详情页或浮层。",
    "Open settings": "打开设置",
    "Open the settings overlay.": "打开设置浮层。",
    "Open album list": "打开相册列表",
    "Open the album overlay and focus the album list.":
        "打开相册浮层并把焦点放到相册列表。",
    "Open import sources": "打开导入源",
    "Open the import source overlay.": "打开导入源浮层。",
    "Open the More menu": "打开「更多」菜单",
    "Open the title bar menu (import sources, settings, quit) and drive "
    "it with the arrow keys.":
        "打开标题栏菜单（导入源、设置、退出），用方向键选择。",
    "Back / close overlay": "返回 / 关闭浮层",
    "Close the current overlay and return to the album grid.":
        "关闭当前浮层，回到相册网格。",
    "Minimize window": "最小化窗口",
    "Minimize the main window.": "最小化主窗口。",
    "Toggle maximize": "最大化 / 还原窗口",
    "Maximize or restore the main window.": "最大化或还原主窗口。",
    "Quit albuswall": "退出 albuswall",
    "Close the application.": "关闭应用。",
    "Focus search": "聚焦搜索",
    "Expand the search bar and put the cursor in it.":
        "展开搜索栏并把光标放进去。",
    "Close detail view": "关闭详情页",
    "Close the detail view (Esc also works).": "关闭详情页（Esc 同样有效）。",
    "Previous image": "上一张",
    "Show the previous asset while the detail view is open.":
        "详情页打开时显示上一张图片。",
    "Next image": "下一张",
    "Show the next asset while the detail view is open.":
        "详情页打开时显示下一张图片。",
    "Toggle favourite": "切换收藏",
    "Toggle the favourite state of the current asset.":
        "切换当前资产的收藏状态。",
    "Toggle information panel": "切换信息面板",
    "Show or hide the metadata panel of the detail view.":
        "显示或隐藏详情页的元信息面板。",
    "Trash / restore asset": "删除 / 恢复资产",
    "Move the current asset to the trash, or restore it when it is "
    "already trashed.": "把当前资产移入回收站；已在回收站时则恢复。",
    "Activate focused control": "激活当前控件",
    "Press the focused button (the keyboard equivalent of clicking it).":
        "按下当前获得焦点的按钮（等价于鼠标点击）。",
    "Activate focused control (keypad Enter)": "激活当前控件（小键盘回车）",
    "Same as Activate focused control, for the numeric keypad Enter key.":
        "同「激活当前控件」，对应数字键盘的回车键。",
    "Focus next region": "聚焦下一个区域",
    "Move the keyboard focus to the next area (title bar, list, grid, "
    "tag bar…).": "把键盘焦点移到下一个区域（标题栏、列表、网格、标签栏…）。",
    "Focus previous region": "聚焦上一个区域",
    "Move the keyboard focus to the previous area.": "把键盘焦点移到上一个区域。",
    "Focus the image grid": "聚焦图片网格",
    "Put the keyboard focus on the thumbnail grid: arrow keys move the "
    "cursor, Enter opens the asset.":
        "把键盘焦点放到缩略图网格：方向键移动光标，回车打开图片。",
    "Focus tag input": "聚焦标签输入框",
    "Focus the tag input of the detail view.": "聚焦详情页的标签输入框。",
    "Enable key bindings": "启用按键映射",
    "When off, the configured keys are ignored (the built-in button "
    "shortcuts still work).":
        "关闭后不再响应这里配置的按键（按钮自带的快捷键仍然有效）。",
    "Search actions or keys…": "搜索动作或按键…",
    "Reset all": "全部恢复默认",
    "Unknown action (not registered anymore): {id}":
        "未知动作（已不再登记）：{id}",
    "customized": "已自定义",
    "Remove": "移除",
    "Only single-key shortcuts are supported; {value} has more than "
    "one key.": "只支持单个按键组合；{value} 包含多个按键。",
    "Remove this leftover binding": "移除这条残留绑定",
    "Reset to default: {value}": "恢复默认：{value}",
    "(unbound)": "（未绑定）",
    "{shown} of {total} actions shown": "显示 {shown} / {total} 个动作",
    "Key conflicts: {details}": "按键冲突：{details}",
    "Saved to {path}": "保存到 {path}",
    "Saved in memory only (the configuration system is not ready yet).":
        "仅保存在内存中（配置系统尚未就绪）。",
    "Key bindings file name": "按键映射文件名",
    "JSON file holding the key bindings overrides (live in the config "
    "directory).": "保存按键映射覆盖值的 JSON 文件（位于配置目录）。",
    # ==================================================
    # 设置树（context: SettingSchema）
    # ==================================================
    "Appearance": "外观",
    "Theme selection, interface language, window title, theme files and "
    "missing-file completion.":
        "主题选择、界面语言、窗口标题、主题文件与缺失文件补全。",
    "Theme details": "主题详情",
    "Files and directories of the selected theme. They are filled in "
    "automatically when a theme is picked above, so they rarely need a "
    "manual edit.":
        "所选主题的文件与目录。在上面选中主题时会自动填写，通常无需手动编辑。",
    "Missing completion": "缺失补全",
    "Copy missing QSS / icons from the bundled theme into the user theme "
    "directory.": "把内置主题中缺失的 QSS 与图标补到用户主题目录。",
    "Thumbnails": "缩略图",
    "Where preview images are generated and cached.": "预览图的生成与缓存位置。",
    "Import": "导入",
    "How aggressively new files are picked up.": "新文件被发现的积极程度。",
    "Trash & cleanup": "回收站与清理",
    "Removing thumbnails that no longer belong to any file.":
        "清理不再属于任何文件的缩略图。",
    "Advanced": "高级",
    "Tuning knobs and paths. Wrong values can make the app behave badly; "
    "fold this group behind an 'Advanced' page.":
        "调优参数与路径。错误的值会让应用表现异常；建议把这一组收进「高级」页面。",
    "Paths": "路径",
    "Where albuswall keeps its files. Changes take effect on the next start.":
        "albuswall 保存文件的位置。修改在下次启动时生效。",
    "File names": "文件名",
    "Relative to the directory above.": "相对于上一行的目录。",
    "Runtime": "运行时",
    "Low-level switches; wrong values can break startup.":
        "底层开关；错误的值可能导致启动失败。",
    "Logging": "日志",
    "Thumbnail tuning": "缩略图调优",
    "Import tuning": "导入调优",
    "Trash tuning": "回收站调优",
    "Task service": "任务服务",
    "Concurrency and back-pressure of the task pool.":
        "任务池的并发与背压。",
    "IPC": "IPC",
    "Cross-process control endpoint (albusctl).": "跨进程控制端点（albusctl）。",
    "Database": "数据库",
    "Language": "语言",
    "Interface language. Switching applies immediately; texts without a "
    "translation keep their source wording.":
        "界面语言。切换立即生效；没有译文的文案保留原文。",
    "Theme": "主题",
    "Theme name; also selects resources/themes/<name> as the source "
    "directory.": "主题名；同时选中 resources/themes/<name> 作为来源目录。",
    "Window title": "窗口标题",
    "Overrides the window title. Empty = follow the album name.":
        "覆盖窗口标题。留空 = 跟随相册名。",
    "Stylesheet": "样式表",
    "QSS file applied to the window. Empty = completed from the bundled "
    "theme.": "应用到窗口的 QSS 文件。留空 = 从内置主题补全。",
    "Icon directory": "图标目录",
    "Absolute icon directory. Empty = derived from the stylesheet's parent.":
        "图标目录的绝对路径。留空 = 由样式表所在目录推导。",
    "Theme directory": "主题目录",
    "Root directory for themes.": "主题的根目录。",
    "Complete missing theme files": "补全缺失的主题文件",
    "Master switch; false = leave the theme alone.":
        "总开关；false = 不处理主题。",
    "Copy missing stylesheet": "复制缺失的样式表",
    "Copy missing icons": "复制缺失的图标",
    "Only once": "仅一次",
    "true = write a marker into the theme dir and never complete again; "
    "delete .albuswall_theme_initialized to re-arm.":
        "true = 在主题目录写一个标记，之后不再补全；删除 "
        ".albuswall_theme_initialized 可重新启用。",
    "Share thumbnails with the desktop": "与桌面共享缩略图",
    "true = write into $XDG_CACHE_HOME/thumbnails so other freedesktop apps "
    "reuse them; false = keep them in albuswall's own cache.":
        "true = 写入 $XDG_CACHE_HOME/thumbnails，供其他 freedesktop 应用复用；"
        "false = 保存在 albuswall 自己的缓存里。",
    "Thumbnail cache directory": "缩略图缓存目录",
    "Used when sharing with the desktop is off. ~ is expanded.":
        "关闭与桌面共享时使用。会展开 ~。",
    "Thumbnails per batch": "每批缩略图数量",
    "Candidates per round": "每轮候选数量",
    "Lower it if importing makes the UI stutter; raise it to drain a large "
    "library faster.":
        "如果导入时界面卡顿就调低；想更快处理大型图库就调高。",
    "Clean orphaned thumbnails": "清理孤立缩略图",
    "true / false forces cleanup on or off; leave the value empty (default) "
    "to let the platform decide.":
        "true / false 强制开启或关闭清理；留空（默认）由平台决定。",
    "Clean up after an unclean shutdown": "异常退出后清理",
    "On boot, purge orphan thumbnails if the last run did not shut down "
    "cleanly.": "启动时若上次未正常退出，则清除孤立缩略图。",
    "Remove empty cache directories": "删除空的缓存目录",
    "Trim now-empty spec directories after deleting thumbnails.":
        "删除缩略图后清掉变空的规格目录。",
    "Config directory": "配置目录",
    "Data directory": "数据目录",
    "Cache directory": "缓存目录",
    "Temp directory": "临时目录",
    "Stylesheet file name": "样式表文件名",
    "Preference file name": "偏好文件名",
    "Window state file name": "窗口状态文件名",
    "Database file name": "数据库文件名",
    "Log config file name": "日志配置文件名",
    "Debug mode": "调试模式",
    "Print extra diagnostics on start. Does not change the log level.":
        "启动时输出额外诊断信息，不改变日志级别。",
    "UI implementation": "UI 实现",
    "Name of the UI plugin to load (restart required).":
        "要加载的 UI 插件名（需要重启）。",
    "Do not load bundled UI resources": "不加载内置 UI 资源",
    "Skip type check on load": "加载时跳过类型检查",
    "Only useful when a hand-written value cannot be converted and you want "
    "the app to start anyway.":
        "仅在手写的值无法转换、但仍希望应用能启动时有用。",
    "Log level": "日志级别",
    "Log config path": "日志配置路径",
    "Empty = <config dir>/<files.log>; consumed by "
    "logging.config.fileConfig.":
        "留空 = <配置目录>/<files.log>；由 logging.config.fileConfig 使用。",
    "Failure cooldown (s)": "失败冷却（秒）",
    "How long a permanently failed file is skipped before it is tried "
    "again.": "永久失败的文件被跳过多久后重试。",
    "Write retries": "写入重试次数",
    "Write retry backoff (s)": "写入重试退避（秒）",
    "Image format": "图像格式",
    "Reserved: output is always PNG.": "预留：输出始终为 PNG。",
    "Quality": "质量",
    "Reserved: PNG output ignores this.": "预留：PNG 输出忽略此项。",
    "Spec sizes": "规格尺寸",
    'JSON object: spec name -> pixel size, e.g. {"small": 128, "medium": '
    '512, "large": 1024}. Keys must be existing spec names.':
        'JSON 对象：规格名 -> 像素尺寸，例如 {"small": 128, "medium": 512, '
        '"large": 1024}。键必须是已存在的规格名。',
    "XDG spec directory map": "XDG 规格目录映射",
    "JSON object: spec name -> share directory name (normal / x-large / "
    "xx-large). Empty = built-in map.":
        "JSON 对象：规格名 -> 共享目录名（normal / x-large / xx-large）。"
        "留空 = 内置映射。",
    "Per-file timeout (s)": "单文件超时（秒）",
    "The worker stops waiting after this; the child process is not killed.":
        "工作线程等待超过该时间即放弃；子进程不会被杀死。",
    "Initial backoff (s)": "初始退避（秒）",
    "Max backoff (s)": "最大退避（秒）",
    "Reconciler name": "对账器名称",
    "Scheduling priority": "调度优先级",
    "Debounce (s)": "去抖（秒）",
    "Orphan minimum age (s)": "孤立缩略图最小存活（秒）",
    "Protects thumbnails written moments ago.": "保护刚刚写入的缩略图。",
    "Session marker path": "会话标记路径",
    "Empty = <platform cache>/albuswall/.session.":
        "留空 = <平台缓存目录>/albuswall/.session。",
    "Initial concurrency": "初始并发",
    "Minimum concurrency": "最小并发",
    "Maximum concurrency": "最大并发",
    "Empty = derive from the CPU count.": "留空 = 根据 CPU 数量推导。",
    "Process workers": "进程工作数",
    "CPU high watermark (%)": "CPU 高水位（%）",
    "CPU low watermark (%)": "CPU 低水位（%）",
    "Memory high watermark (%)": "内存高水位（%）",
    "Memory low watermark (%)": "内存低水位（%）",
    "Load check interval (s)": "负载检查间隔（秒）",
    "Scheduler threads": "调度线程数",
    "Max queued tasks": "最大排队任务数",
    "Submit timeout (s)": "提交超时（秒）",
    "Low-load streak": "低负载连续次数",
    "Consecutive idle checks before concurrency drops.":
        "并发下调前需要连续空闲检查的次数。",
    "Wait warning threshold (s)": "等待告警阈值（秒）",
    "Enable the IPC endpoint": "启用 IPC 端点",
    "Call timeout (s)": "调用超时（秒）",
    "Max message size (bytes)": "最大消息大小（字节）",
    "Max connections": "最大连接数",
    "Bridge internal signals as events": "把内部信号桥接为事件",
    "Fail if the database is missing": "数据库缺失时报错",
    "false = create it on first start.": "false = 首次启动时创建。",
    "Reserved action for a future theme editor: it will open the stylesheet "
    "and the icons of the selected theme for editing.":
        "为主题编辑器预留的操作：将来会打开所选主题的样式表与图标以便编辑。",
    "Version, author and a short description of the project.":
        "项目的版本、作者与简介。",
    "AlbusWall - Hyprland Image Gallery\n"
    "Your wall of memories, frame by frame.\n"
    "A clean grid gallery for your ricing shots.\n"
    "\n"
    "Additional subtitles:\n"
    "...":
        "AlbusWall —— Hyprland 图片廊\n"
        "每一帧，都是你珍藏的记忆。\n"
        "为你的 ricing 截图打造的清爽网格图库。\n"
        "\n"
        "更多副标题：\n"
        "……",
    "Pick one of the themes found on disk. Themes are directories that "
    "contain a QSS file: the bundled ones live in resources/themes/, user "
    "themes go either next to the current theme directory or under "
    "<config dir>/themes.":
        "从磁盘上找到的主题中选择一个。主题是包含 QSS 文件的目录：内置主题位于 "
        "resources/themes/，用户主题放在当前主题目录旁或 <配置目录>/themes 下。",
    "Restart required": "需要重启",
    "Directory of the selected theme. Picking a theme above fills this in "
    "automatically; the stylesheet and the icon directory are derived from "
    "it.":
        "所选主题的目录。在上面选中主题时会自动填写；样式表与图标目录由它推导。",
    "Stylesheet file of the selected theme. Empty = derived from the theme "
    "directory (${path:theme}/${files:qss}).":
        "所选主题的样式表文件。留空 = 由主题目录推导"
        "（${path:theme}/${files:qss}）。",
    "Icon directory of the selected theme. Empty = icons/ cover/ imgs/ "
    "assets/ next to the stylesheet.":
        "所选主题的图标目录。留空 = 样式表旁的 icons/ cover/ imgs/ assets/。",
}

# ══════════════════════════════════════════════════
# 中文 → 英文
# ══════════════════════════════════════════════════
EN_US: dict[str, str] = {
    # ---- 元信息面板（动态标签，源文仍是中文） ----
    "标题": "Title",
    "描述": "Description",
    "宽度": "Width",
    "高度": "Height",
    "文件大小": "File size",
    "MIME 类型": "MIME type",
    "拍摄时间": "Taken at",
    "加入时间": "Added at",
    "修改时间": "Modified at",
    "删除时间": "Deleted at",
    "已收藏": "Favorite",
    "已删除": "Deleted",
    "文件路径": "File path",
}

#: 语言代码 → 词表
TRANSLATIONS: dict[str, dict[str, str]] = {
    "en_US": EN_US,
    "zh_CN": ZH_CN,
}


# ══════════════════════════════════════════════════
# 运行时收集的设置树文案（context: SettingSchema）
# ══════════════════════════════════════════════════
def _settings_ui_available() -> bool:
    """设置页是否已经恢复到 UI 层（决定要不要收集设置树文案）。"""
    try:
        import albuswall.ui.builtin.setting  # noqa: F401
    except ModuleNotFoundError:
        return False
    return True


def schema_strings() -> list[str]:
    """收集所有会经 ``schema_tr()`` 展示的文案。

    设置树的文字来自配置层登记的数据（分组标题 / 说明、叶子标签 / 说明），
    lupdate 提取不到，只能在这里从登记表里实体化。

    设置树只在设置页里渲染，而设置页（``ui/builtin/setting``）在当前重构
    里还没恢复：此时直接返回空表，``.ts`` 里就不会塞进一批没人展示的配置
    键名。设置页恢复后本函数自动生效，无需改这里，只要重跑
    ``python scripts/build_i18n.py``。
    """
    from albuswall.configue.schema import build_schema

    collected: list[str] = []

    def add(value: object) -> None:
        if isinstance(value, str) and value.strip() and value not in collected:
            collected.append(value)

    if not _settings_ui_available():
        return collected

    try:
        schema = build_schema()
    except Exception:  # pragma: no cover - 配置层未装配时退化成空树
        schema = None
    if schema is not None:
        for group in schema.groups():
            add(group.title)
            add(group.description)
        for _group, item in schema.iter_items():
            add(item.display_label)
            add(item.description)

    # 关于页里的固定字段名（schema_tr 的调用点在 about 页）
    for literal in ("Title", "Version", "Author", "Thanks for using it."):
        add(literal)
    # 关于页展示的项目简介就是 albuswall 包的 docstring
    from albuswall.__about__ import __doc__ as about_doc

    add((about_doc or "").strip())

    return collected


# 兼容老名字（build_i18n.py 早期版本用过）
def iter_manual_sources() -> Iterable[tuple[str, str]]:
    for context, sources in MANUAL_CONTEXTS.items():
        for source in sources:
            yield context, source
