#
"""演示用的**特制 dump**（不经过 ``albuswall.configue``）。

演示要证明的是「被动视图吃得下真实 dump 形状」：这里手工合成一份与
``dump_schema_dict()`` + ``apply_present()`` 同形的数据，覆盖所有控件、
高级项、只读 / 读失败 / 插值、未绑定、未覆盖。

真实集成时这份数据应由 presenter 调 ``dump_schema_dict`` + ``apply_present``
生成；本模块只是演示数据源，视图不认识它。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

__all__ = ["DemoState", "build_demo_dump", "leaf_definition", "leaf_index"]

MAIN_FILE = "/home/demo/.config/albuswall/config.ini"
LOG_FILE = "/home/demo/.config/albuswall/log_config.ini"
SHORTCUTS_FILE = "/home/demo/.config/albuswall/shortcuts.ini"


# ============================================================================
# 叶子 / 分组声明
# ============================================================================

def leaf_definition(
        key: str,
        *,
        section: str,
        config_key: str,
        label: str,
        description: str,
        type: str,
        default: Any,
        widget: str,
        target_file: str | None = MAIN_FILE,
        order: int = 0,
        choices: Iterable[Any] = (),
        choice_labels: Mapping[str, str] | None = None,
        constraints: Mapping[str, float] | None = None,
        advanced: bool = False,
        readonly: bool = False,
        origin_file: str | None = None,
        interpolated: bool = False,
        raw: Any = None,
        read_error: str | None = None,
        editable: bool = True,
) -> dict[str, Any]:
    """一条叶子声明（值 / 状态由 :func:`build_demo_dump` 填）。"""
    return {
        "key": key,
        "name": config_key,
        "group": key.rsplit(".", 1)[0],
        "path": f"[{section}]{config_key}",
        "section": section,
        "config_key": config_key,
        "label": label,
        "description": description,
        "type": type,
        "default": default,
        "widget": widget,
        "choices": list(choices),
        "choice_labels": dict(choice_labels or {}),
        "constraints": dict(constraints) if constraints else None,
        "order": order,
        "advanced": advanced,
        "editable": editable,
        "readonly": readonly,
        "origin_file": origin_file,
        "target_file": target_file,
        "interpolated": interpolated,
        "raw": raw,
        "read_error": read_error,
        "source": "declared",
        "present": False,
        "dirty": False,
        "value": None,
    }


_GROUPS: list[dict[str, Any]] = [
    {
        "key": "ui",
        "title": "外观与语言",
        "description": "主题、语言与窗口标题。语言切换后需要重新 dump 才会更新文案。",
        "order": 0,
        "items": [
            leaf_definition(
                "ui.theme", section="ui", config_key="theme",
                label="主题", description="界面主题；切语言 / 切主题都只需要改这一个键。",
                type="str", default="default", widget="choice",
                choices=("default", "dark", "light"),
                choice_labels={"default": "跟随系统", "dark": "深色", "light": "浅色"},
                order=0,
            ),
            leaf_definition(
                "ui.language", section="ui", config_key="language",
                label="界面语言", description="候选值来自语言注册表，显示名为母语名。",
                type="str", default="zh_CN", widget="choice",
                choices=("zh_CN", "en_US", "ja_JP"),
                choice_labels={"zh_CN": "简体中文", "en_US": "English", "ja_JP": "日本語"},
                order=1,
            ),
            leaf_definition(
                "ui.window_title", section="ui", config_key="window_title",
                label="窗口标题", description="标题栏文字；空串表示使用应用名。",
                type="str", default="albusWall", widget="text", order=2,
            ),
            leaf_definition(
                "ui.accent_color", section="ui", config_key="accent_color",
                label="强调色", description="十六进制颜色，例如 #3d7eff。保存时会校验格式。",
                type="str", default="#3d7eff", widget="text", order=3,
            ),
            leaf_definition(
                "ui.remember_window", section="ui", config_key="remember_window",
                label="记住窗口位置", description="退出时把窗口几何写进 [window]。",
                type="bool", default=True, widget="bool", order=4,
            ),
            leaf_definition(
                "ui.recent_files", section="ui", config_key="recent_files",
                label="最近打开", description="列表值用 JSON 文本编辑；提交时转成 list。",
                type="list[str]", default=[], widget="list", order=5,
            ),
            leaf_definition(
                "ui.window_metrics", section="ui", config_key="window_metrics",
                label="窗口尺寸缓存", description="字典值，用 JSON 文本编辑。",
                type="dict[str, int]", default={}, widget="dict", order=6,
            ),
            leaf_definition(
                "ui.debug_overlay", section="ui", config_key="debug_overlay",
                label="调试浮层", description="基础分组里的高级叶子：关闭「显示高级项」时不渲染。",
                type="bool", default=False, widget="bool", advanced=True, order=7,
            ),
        ],
        "groups": [],
    },
    {
        "key": "view",
        "title": "缩略图显示",
        "description": "网格与渲染质量。",
        "order": 1,
        "items": [
            leaf_definition(
                "view.grid_columns", section="view", config_key="grid_columns",
                label="网格列数", description="0 表示按窗口宽度自动换算。",
                type="int", default=0, widget="int",
                constraints={"minimum": 0, "maximum": 12, "step": 1},
                order=0,
            ),
            leaf_definition(
                "view.thumbnail_quality", section="view", config_key="thumbnail_quality",
                label="缩略图质量", description="JPEG 输出质量，0.05 步进。",
                type="float", default=0.85, widget="float",
                constraints={"minimum": 0.05, "maximum": 1.0, "step": 0.05},
                order=1,
            ),
            leaf_definition(
                "view.sidebar_width", section="view", config_key="sidebar_width",
                label="侧栏宽度", description="像素；未设置时用默认值。",
                type="int", default=240, widget="int",
                constraints={"minimum": 160, "maximum": 480, "step": 10},
                order=2,
            ),
        ],
        "groups": [],
    },
    {
        "key": "thumbnail",
        "title": "缩略图任务",
        "description": "批处理大小与并发。",
        "order": 2,
        "items": [
            leaf_definition(
                "thumbnail.batch_size", section="thumbnail", config_key="batch_size",
                label="批大小", description="每次提交的缩略图数量；必须是 8 的倍数。",
                type="int", default=32, widget="int",
                constraints={"minimum": 8, "maximum": 256, "step": 8},
                order=0,
            ),
            leaf_definition(
                "thumbnail.workers", section="thumbnail", config_key="workers",
                label="并发生成数", description="同时解码的 worker 数量。",
                type="int", default=4, widget="int",
                constraints={"minimum": 1, "maximum": 16, "step": 1},
                order=1,
            ),
            leaf_definition(
                "thumbnail.use_cache", section="thumbnail", config_key="use_cache",
                label="复用缓存", description="命中缓存时跳过重新解码。",
                type="bool", default=True, widget="bool", order=2,
            ),
        ],
        "groups": [
            {
                "key": "thumbnail.advanced",
                "title": "高级缩略图",
                "description": "进阶参数，默认折叠在高级分组里。",
                "order": 10,
                "advanced": True,
                "items": [
                    leaf_definition(
                        "thumbnail.advanced.decoder", section="thumbnail",
                        config_key="decoder", label="解码器",
                        description="auto 优先 Pillow，失败再回退 ffmpeg。",
                        type="str", default="auto", widget="choice",
                        choices=("auto", "pil", "ffmpeg"),
                        choice_labels={
                            "auto": "自动", "pil": "Pillow", "ffmpeg": "FFmpeg",
                        },
                        advanced=True, order=0,
                    ),
                    leaf_definition(
                        "thumbnail.advanced.memory_limit_mb", section="thumbnail",
                        config_key="memory_limit_mb", label="内存上限",
                        description="单次批处理的内存预算（MB）。",
                        type="int", default=512, widget="int",
                        constraints={"minimum": 128, "maximum": 4096, "step": 128},
                        advanced=True, order=1,
                    ),
                ],
                "groups": [],
            },
        ],
    },
    {
        "key": "network",
        "title": "网络",
        "description": "远端数据源与代理。",
        "order": 3,
        "items": [
            leaf_definition(
                "network.endpoint", section="network", config_key="endpoint",
                label="服务地址", description="读取失败示例：值无法解析成 URL。",
                type="str", default="https://example.invalid/api", widget="text",
                read_error="config value is not a valid URL: expected scheme://host",
                order=0,
            ),
            leaf_definition(
                "network.port", section="network", config_key="port",
                label="端口", description="1024–65535。",
                type="int", default=8080, widget="int",
                constraints={"minimum": 1024, "maximum": 65535, "step": 1},
                order=1,
            ),
            leaf_definition(
                "network.proxy_mode", section="network", config_key="proxy_mode",
                label="代理模式", description="只读示例：值来自只读文件。",
                type="str", default="env", widget="choice",
                choices=("env", "none", "manual"),
                choice_labels={"env": "跟随环境变量", "none": "不使用", "manual": "手动"},
                readonly=True, origin_file=MAIN_FILE, order=2,
            ),
            leaf_definition(
                "network.timeout", section="network", config_key="timeout",
                label="超时（秒）", description="未设置时用默认值 15。",
                type="float", default=15.0, widget="float",
                constraints={"minimum": 1.0, "maximum": 120.0, "step": 1.0},
                order=3,
            ),
        ],
        "groups": [],
    },
    {
        "key": "paths",
        "title": "路径",
        "description": "目录与 ${...} 插值示例。",
        "order": 4,
        "items": [
            leaf_definition(
                "paths.theme_dir", section="path", config_key="theme",
                label="主题目录", description="值来自 ${path:config}/theme；界面只显示原串。",
                type="Path", default="/home/demo/.config/albuswall/theme",
                widget="path", interpolated=True,
                raw="${path:config}/theme", order=0,
            ),
            leaf_definition(
                "paths.cache_dir", section="path", config_key="cache",
                label="缓存目录", description="缩略图与日志缓存的位置。",
                type="Path", default="/home/demo/.cache/albuswall", widget="path",
                order=1,
            ),
            leaf_definition(
                "paths.export_dir", section="path", config_key="export",
                label="导出目录", description="未设置时使用 ~/Pictures/albusWall。",
                type="Path", default="/home/demo/Pictures/albusWall",
                widget="path", order=2,
            ),
        ],
        "groups": [],
    },
    {
        "key": "log",
        "title": "日志",
        "description": "独立文件 log_config.ini。",
        "order": 5,
        "items": [
            leaf_definition(
                "log.level", section="log", config_key="level",
                label="日志级别", description="INFO 以上写入文件。",
                type="str", default="INFO", widget="choice",
                choices=("DEBUG", "INFO", "WARNING", "ERROR"),
                target_file=LOG_FILE, order=0,
            ),
            leaf_definition(
                "log.file", section="log", config_key="file",
                label="日志文件", description="可写路径。",
                type="Path", default="/home/demo/.cache/albuswall/log/albuswall.log",
                widget="path", target_file=LOG_FILE, order=1,
            ),
        ],
        "groups": [],
    },
    {
        "key": "plugins",
        "title": "插件",
        "description": "这里故意放两个推不出归属文件的叶子（演示「未绑定」区）。",
        "order": 6,
        "items": [
            leaf_definition(
                "plugins.cache_dir", section="plugins", config_key="cache_dir",
                label="插件缓存目录", description="声明文件尚未注册，target_file 为空。",
                type="Path", default="", widget="path", target_file=None, order=0,
            ),
            leaf_definition(
                "plugins.manifest", section="plugins", config_key="manifest",
                label="插件清单", description="只读展示：无归属文件时提交无处可写。",
                type="str", default="", widget="text", target_file=None, order=1,
            ),
        ],
        "groups": [],
    },
    {
        "key": "about",
        "title": "关于",
        "description": "",
        "order": 9,
        "components": [
            {
                "type": "label",
                "text": (
                    "albusWall 演示数据\n"
                    "这一块是自定义组件（不绑定配置节点），"
                    "由 dump 的 groups[].components 带下来。"
                ),
                "object_name": "SettingAboutText",
            },
        ],
        "items": [],
        "groups": [],
    },
    {
        "key": "advanced",
        "title": "高级",
        "description": "整组都是高级项：关闭「显示高级项」时整棵子树不渲染。",
        "order": 8,
        "advanced": True,
        "items": [
            leaf_definition(
                "advanced.http_timeout", section="advanced", config_key="http_timeout",
                label="HTTP 超时", description="秒。",
                type="float", default=5.0, widget="float",
                constraints={"minimum": 1.0, "maximum": 60.0, "step": 1.0},
                advanced=True, order=0,
            ),
            leaf_definition(
                "advanced.experimental", section="advanced", config_key="experimental",
                label="实验特性", description="不保证兼容。",
                type="bool", default=False, widget="bool", advanced=True, order=1,
            ),
        ],
        "groups": [],
    },
]


_UNCOVERED: list[dict[str, Any]] = [
    {
        "path": "[shortcuts]open_settings",
        "section": "shortcuts",
        "key": "open_settings",
        "typed": True,
        "origin_file": SHORTCUTS_FILE,
        "target_file": SHORTCUTS_FILE,
        "value_repr": "'Ctrl+,' : app.settings",
    },
    {
        "path": "[shortcuts]toggle_theme",
        "section": "shortcuts",
        "key": "toggle_theme",
        "typed": True,
        "origin_file": SHORTCUTS_FILE,
        "target_file": SHORTCUTS_FILE,
        "value_repr": "'Ctrl+Shift+T' : app.theme.toggle",
    },
    {
        "path": "[legacy]unknown_key",
        "section": "legacy",
        "key": "unknown_key",
        "typed": False,
        "origin_file": MAIN_FILE,
        "target_file": MAIN_FILE,
        "value_repr": "some old value",
    },
]


# ============================================================================
# 演示状态（presenter 的假后端）
# ============================================================================

#: 初始「配置文件里真的写了」的键。
_PRESENT: dict[str, Any] = {
    "ui.theme": "dark",
    "ui.language": "zh_CN",
    "ui.window_title": "我的相册",
    "ui.accent_color": "#3d7eff",
    "ui.recent_files": ["2024/海.jpg", "2024/山.jpg"],
    "ui.window_metrics": {"width": 1280, "height": 720},
    "view.grid_columns": 4,
    "view.thumbnail_quality": 0.85,
    "thumbnail.batch_size": 64,
    "thumbnail.workers": 6,
    "network.endpoint": "",
    "network.port": 8080,
    "network.proxy_mode": "env",
    "paths.theme_dir": "/home/demo/.config/albuswall/theme",
    "paths.cache_dir": "/home/demo/.cache/albuswall",
    "log.level": "DEBUG",
    "log.file": "/home/demo/.cache/albuswall/log/albuswall.log",
}


@dataclass
class DemoState:
    """presenter 侧的假配置后端（视图完全不认识它）。"""

    values: dict[str, Any] = field(
        default_factory=lambda: deepcopy(_PRESENT),
    )
    dirty: set[str] = field(default_factory=lambda: {"thumbnail.batch_size"})
    #: 模拟「上一次写盘失败」的节点。
    pending: set[str] = field(default_factory=set)
    #: 模拟失败开关（演示 pending footer + 重试）。
    simulate_pending: bool = False

    def node_of(self, key: str) -> tuple[str, str]:
        leaf = leaf_index()[key]
        return str(leaf["section"]), str(leaf["config_key"])

    def default_of(self, key: str) -> Any:
        return deepcopy(leaf_index()[key]["default"])


def leaf_index() -> dict[str, dict[str, Any]]:
    """``key → 叶子声明``（遍历所有分组，含嵌套）。"""
    index: dict[str, dict[str, Any]] = {}

    def walk(group: Mapping[str, Any]) -> None:
        for item in group.get("items") or ():
            index[str(item["key"])] = item
        for child in group.get("groups") or ():
            walk(child)

    for group in _GROUPS:
        walk(group)
    return index


def _filled_leaf(
        definition: Mapping[str, Any], state: DemoState,
) -> dict[str, Any]:
    leaf = deepcopy(dict(definition))
    key = str(leaf["key"])
    if key in state.pending or key in state.dirty:
        leaf["dirty"] = True
    else:
        leaf["dirty"] = False

    if key in state.values:
        leaf["present"] = True
        leaf["value"] = deepcopy(state.values[key])
    else:
        leaf["present"] = False
        leaf["value"] = deepcopy(leaf.get("default"))
    if leaf.get("read_error"):
        # 读失败时 dump 的值回落 default（与 configue 一致）
        leaf["value"] = deepcopy(leaf.get("default"))
    if leaf.get("interpolated"):
        leaf["value"] = deepcopy(leaf.get("default"))
        leaf["raw"] = leaf.get("raw") or leaf.get("default")
    return leaf


def build_demo_dump(state: DemoState | None = None) -> dict[str, Any]:
    """合成一份与 ``dump_schema_dict`` 同形的 dump（已带 ``apply_present`` 字段）。"""
    state = state or DemoState()

    def walk(source: Mapping[str, Any]) -> dict[str, Any]:
        items = [_filled_leaf(item, state) for item in source.get("items") or ()]
        children = [walk(child) for child in source.get("groups") or ()]
        group = {
            "id": source["key"],
            "key": source["key"],
            "title": source["title"],
            "description": source.get("description") or "",
            "order": source.get("order", 0),
            "editable": True,
            "advanced": bool(source.get("advanced", False)),
            "items": items,
            "groups": children,
            "components": deepcopy(source.get("components") or []),
        }
        return group

    groups = [walk(group) for group in _GROUPS]

    # 派生字段（items / by_file / unbound / summary / item_count）**不在这里算**：
    # ``SettingTree`` 是唯一实现，视图装树时会补齐。这里只产「原始 dump」，
    # 演示与测试因此走的是和真实页面同一条归一化路径。
    return {
        "schema_version": 2,
        "groups": groups,
        "uncovered": deepcopy(_UNCOVERED),
        "source": {"declared": 0, "declared+override": 0, "manual": 0},
    }
