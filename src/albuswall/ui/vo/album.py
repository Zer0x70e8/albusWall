#
""""""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Any, Mapping, Optional

# 图标字段：None = 不处理；Path = 加载图标；"" = 清空图标
IconValue = Optional[Path]

# 文本字段：None = 不处理；str = 文本；"" = 显式清空文本
TextValue = Optional[str]


@dataclass(slots=True)
class TitleBarVO:
    """TitleBar 的视图数据对象。

    只承载"要显示什么"，不持有任何 Qt 控件引用。

    字段取值约定（渲染层必须遵守）：
      - None        ：不处理，保持控件当前状态
      - ""          ：显式清空（图标字段清空图标 / 文本字段清空文本）
      - Path / str  ：图标路径 / 文本
    """

    # ---- 操作栏 ----
    window_title: TextValue = None

    # ---- 工具栏：窗口图标 + 标题 ----
    cover: Optional[str] = None
    title: TextValue = None
    description: TextValue = None

    # ---- 工具栏：按钮（图标 + 文本分开，互不影响）----
    album_icon: IconValue = None
    album_text: TextValue = None

    extra_icon: IconValue = None
    extra_text: TextValue = None

    search_icon: IconValue = None
    search_text: TextValue = None

    # ---- 搜索栏 ----
    search_expanded: Optional[bool] = None

    # ---------------- 构造 / 转换 ----------------

    @classmethod
    def from_dict(cls, data: Optional[Mapping[str, Any]]) -> "TitleBarVO":
        """从 dict 构造，自动忽略未知字段，缺失字段用默认值。"""
        if not data:
            return cls()
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def updated(self, **changes: Any) -> "TitleBarVO":
        """返回替换了部分字段的新 VO（不修改自身）。"""
        return replace(self, **changes)
