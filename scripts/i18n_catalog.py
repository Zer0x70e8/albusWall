#
"""i18n 翻译词表（``scripts/build_i18n.py`` 的输入数据）。

这里是**纯数据**：源文 → 译文。仓库里的界面源文是中英混排的，所以：

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
    "MetadataModel": [
        "是",
        "否",
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
        "剩余 {days} 天",
        "今天删除",
        "已过期",
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
}

# ══════════════════════════════════════════════════
# 英文 → 中文
# ══════════════════════════════════════════════════
ZH_CN: dict[str, str] = {
    # ---- 标题栏 / 菜单 ----
    "album": "相册",
    "extra": "更多",
    "search": "搜索",
    "source": "来源",
    "setting": "设置",
    "exit": "退出",
    "edit": "编辑",
    "Edit": "编辑",
    # ---- 详情页 ----
    "choice": "多选",
    "favourite": "收藏",
    "info": "信息",
    "trash": "删除",
    "recover": "恢复",
    "Close": "关闭",
    # ---- 通用动作 ----
    "add": "添加",
    "apply": "应用",
    "enable": "启用",
    "disable": "禁用",
    "delete": "删除",
    "ensure deleted": "确认删除",
    "layers": "层级",
    "new": "新建",
    "browser": "浏览",
    "no limit": "不限",
    # ---- 源详情表单 ----
    "meta info": "元信息",
    "title": "标题",
    "new ingest source": "新建导入源",
    "description": "描述",
    "example": "示例",
    "tag: ": "标签：",
    "tag1": "标签1",
    "Ingest Source": "导入源",
    "source path (library path)": "源路径（图库路径）",
    "file type(s)": "文件类型",
    "File type verification method": "文件类型校验方式",
    "Suffix Check": "后缀检查",
    "Magic Number Check": "魔数检查",
    "Subfolder recursion": "子文件夹递归",
    "Subfolder recursion depth": "子文件夹递归深度",
    "Triggers": "触发器",
    "Scheduled Update": "定时更新",
    "Scheduled Time": "定时时间",
    "Interval Time": "间隔时间",
    "mode": "模式",
    "e.g., 30m or 1h": "例如 30m 或 1h",
    "Device Insertion Trigger": "设备插入触发器",
    "Target": "目标",
    "Auto-mount (Linux only, requires permissions)": "自动挂载（仅 Linux，需要权限）",
    "Mount point": "挂载点",
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
    # ---- 搜索栏 ----
    "简单模式": "Simple mode",
    "命令模式": "Command mode",
    "命令模式（未实现）": "Command mode (not implemented)",
    "输入标签名筛选当前相册...": "Type a tag name to filter this album...",
    "搜索": "Search",
    "关闭搜索栏": "Close search bar",
    "无匹配标签": "No matching tags",
    "命令模式尚未实现": "Command mode is not implemented yet",
    "当前相册没有匹配的标签：{text}":
        "No matching tags in the current album: {text}",
    # ---- 标签 ----
    "标签名，可用 / 分层": "Tag name; use / for hierarchy",
    "添加到当前图片": "Add to the current image",
    "新建标签": "New tag",
    "新建子标签": "New child tag",
    "新建根标签": "New root tag",
    "重命名标签": "Rename tag",
    "删除标签": "Delete tag",
    "名称": "Name",
    "标签含子节点": "Tag has child tags",
    "该标签下有 {count} 个子标签，是否连同子标签一起删除？":
        "This tag has {count} child tags. Delete them as well?",
    "父标签仍在回收站": "Parent tag is still in the trash",
    "标签已恢复，但父标签仍在回收站，当前为孤儿状态。":
        "The tag was restored, but its parent is still in the trash; "
        "it is currently orphaned.",
    "标签已存在：{text}": "Tag already exists: {text}",
    # ---- 导入源 ----
    "未命名导入源": "Untitled ingest source",
    "选择源目录": "Select source directory",
    # ---- 元信息面板 ----
    "是": "Yes",
    "否": "No",
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
    # ---- 缩略图信息条 ----
    "剩余 {days} 天": "{days} days left",
    "今天删除": "Deleted today",
    "已过期": "Expired",
}

#: 语言代码 → 词表
TRANSLATIONS: dict[str, dict[str, str]] = {
    "en_US": EN_US,
    "zh_CN": ZH_CN,
}


# ══════════════════════════════════════════════════
# 运行时收集的设置树文案（context: SettingSchema）
# ══════════════════════════════════════════════════
def schema_strings() -> list[str]:
    """收集所有会经 ``schema_tr()`` 展示的文案。

    设置树的文字来自配置层登记的数据（分组标题 / 说明、叶子标签 / 说明、
    自定义行、关于页），lupdate 提取不到，只能在这里从登记表里实体化。
    """
    from albuswall.configue.setting_schema import SETTING_SCHEMA
    from albuswall.services.command import register_user_settings
    from albuswall.ui.builtin.setting import overrides
    from albuswall.ui.builtin.setting.extras import iter_extras
    from albuswall.ui.builtin.setting.pages import iter_pages

    register_user_settings()

    collected: list[str] = []

    def add(value: object) -> None:
        if isinstance(value, str) and value.strip() and value not in collected:
            collected.append(value)

    for group in SETTING_SCHEMA.groups():
        add(group.title)
        add(group.description)
    for _group, item, _file in SETTING_SCHEMA.iter_items():
        add(item.display_label)
        add(item.description)
    for spec in iter_extras():
        add(spec.title)
        add(spec.description)
        add(spec.button)
    for page in iter_pages():
        add(page.title)
        add(page.description)
    for patch in overrides.iter_item_patches():
        add(getattr(patch, "label", None))
        add(getattr(patch, "description", None))
        add(getattr(patch, "hint", None))

    # 关于页里的固定字段名（schema_tr 的调用点在 about.py）
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
