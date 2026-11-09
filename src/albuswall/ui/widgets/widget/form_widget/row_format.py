# -*- coding: utf-8 -*-
"""表单行格式枚举。

设计原则：
- 枚举只承担「形态」「读写状态」「装饰位置」「校验时机」四轴，互相正交；
- 值语义（JSON / PATH / BOOL ...）由 add_row 的 transform / validator / 专属参数表达；
- 布局语义由各枚举值自身的隐式契约描述，不再需要 is_full_width 之类的外部判断
  （RowSpec 上的 is_inline / is_full_width 仅为便捷读取，不改变契约本身）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import FrozenSet, Optional, Union


# ============================================================
# 1. 控件形态
# ============================================================
class RowControl(Enum):
    """控件形态：只决定 widget 类与隐式布局契约。

    隐式布局：
      · 行内  —— 左 label(9) + 右 field(3)
      · 整行  —— 占满宽度，无 label 列
      · CHECKBOX —— 行内例外：控件在前、text 在后，无 label 列

    叠加 ResetPosition 后的布局增量（存在性由 default 隐式决定）：
      · 行内 + INLINE      → label(9) : field(2) : button(1)
      · 行内 + BELOW       → 控件行 + 按钮行（右对齐）
      · 整行 + INLINE      → field flex + 尾部按钮
      · 整行 + BELOW       → 控件行 + 按钮行
      · CHECKBOX + INLINE  → checkbox + text 一段 + 尾部按钮
      · CHECKBOX + BELOW   → 控件行 + 按钮行
    """

    # ---- 行内：左 label(9) + 右 field(3) ----
    KV = auto()            # QLineEdit
    MULTILINE = auto()     # QPlainTextEdit
    NUMBER = auto()        # QSpinBox / QDoubleSpinBox
    PASSWORD = auto()      # 命名糖：≡ KV + echo_mode="password"
    KEYSEQUENCE = auto()   # QKeySequenceEdit
    SLIDER = auto()        # QSlider；orientation 覆盖方向
    COMBO = auto()         # QComboBox
    RADIO = auto()         # QRadioButton 组（纵向）
    CHECKBOX = auto()      # 隐式布局例外：控件在前、text 在后，无 label 列
    COLOR = auto()         # QLineEdit + 预览 + 拾色
    PATH = auto()          # QLineEdit + 浏览；browse_kind 决定行为
    SUBFORM = auto()       # 嵌套；schema / rows 二选一

    # ---- 整行：占满宽度，无 label 列 ----
    SEPARATOR = auto()     # 分割线
    LABEL = auto()         # 纯展示文本；wrap 控制单/多行


# ============================================================
# 2. 读写状态
# ============================================================
class RowAccess(Enum):
    """读写状态（与 RowControl 正交）。

    与 RowControl 组合使用，如 KV + READONLY、SUBFORM + DISABLED。
    DISABLED 视觉为 Qt 默认灰显；如需保持只读外观请由主题层覆盖 :disabled 样式。
    """

    READWRITE = auto()     # 默认，可编辑
    READONLY = auto()      # 可选中、可复制，不可编辑
    DISABLED = auto()      # 灰显，不可聚焦、不可选中、不可编辑


# ============================================================
# 3. 路径浏览类型
# ============================================================
class BrowseKind(Enum):
    """PATH 的浏览对话框类型与校验口径。"""

    FILE = auto()          # 打开文件对话框
    DIRECTORY = auto()     # 目录对话框
    SAVE = auto()          # 保存文件对话框（校验可写）
    ANY = auto()           # 只取路径字符串，不校验存在性/类型


# ============================================================
# 4. 重置按钮位置
# ============================================================
class ResetPosition(Enum):
    """重置按钮位置。

    存在性由 default 隐式决定：
      · default is None                    → 无按钮（位置参数静默忽略）
      · default 有值 + INLINE（默认）       → 内联按钮
      · default 有值 + BELOW               → 下方按钮
      · default 有值 + reset_position=None → 无按钮，但 default 保留给程序化重置
    """

    INLINE = auto()        # 控件右侧内联
    BELOW = auto()         # 控件下方右对齐


# ============================================================
# 5. 校验触发时机
# ============================================================
class ValidationMode(Enum):
    """校验触发时机（替代原 live_validate: bool）。"""

    ON_COMMIT = auto()     # 失焦 / 回车时校验（默认）
    ON_CHANGE = auto()     # 每次编辑即校验


# ============================================================
# 6. 参数作用域表（供公开守卫使用）
# ============================================================
# 所有形态都接受的结构参数（在整行控件上静默忽略）
COMMON_PARAMS: FrozenSet[str] = frozenset({
    "label", "value", "ident", "default",
    "transform", "validator", "validation_mode",
    "label_span", "field_span",
})

# 形态专属参数：仅该形态可接受，其它形态显式传入 → 报错
CONTROL_PARAMS: dict = {
    RowControl.KV:          frozenset({"placeholder"}),
    RowControl.MULTILINE:   frozenset({"placeholder", "height"}),
    RowControl.PASSWORD:    frozenset({"placeholder"}),
    RowControl.NUMBER:      frozenset({"minimum", "maximum", "step", "decimals"}),
    RowControl.SLIDER:      frozenset({"minimum", "maximum", "step", "orientation"}),
    RowControl.COMBO:       frozenset({"items"}),
    RowControl.RADIO:       frozenset({"items", "orientation"}),
    RowControl.CHECKBOX:    frozenset({"text", "wrap"}),
    RowControl.LABEL:       frozenset({"text", "wrap"}),
    RowControl.SEPARATOR:   frozenset(),
    RowControl.PATH:        frozenset({"browse_kind", "placeholder"}),
    RowControl.SUBFORM:     frozenset({"schema", "rows"}),
    RowControl.KEYSEQUENCE: frozenset(),
    RowControl.COLOR:       frozenset(),
}

# 结构参数在这些形态上静默忽略（不报错，便于批量套模板）
IGNORED_PARAMS: dict = {
    RowControl.LABEL:     frozenset({"ident", "value", "default",
                                      "transform", "validator"}),
    RowControl.SEPARATOR: frozenset({"ident", "value", "default",
                                      "transform", "validator",
                                      "label", "text", "wrap"}),
}


# ============================================================
# 7. 查询层（公开纯函数，逻辑单一来源）
# ============================================================
def supports(control: Union[RowControl, str], param: str) -> bool:
    """形态是否接受该参数。

    自定义 str 控件一律返回 True（全部透传到下游）。
    """
    if isinstance(control, str):
        return True
    if param in COMMON_PARAMS:
        return True
    return param in CONTROL_PARAMS.get(control, frozenset())


def supported_params(control: Union[RowControl, str]) -> FrozenSet[str]:
    """该形态接受的全部参数集合；自定义 str 控件返回空 frozenset。"""
    if isinstance(control, str):
        return frozenset()          # 自定义控件：无固定集合，全透传
    return COMMON_PARAMS | CONTROL_PARAMS.get(control, frozenset())


# ============================================================
# 8. 组合声明（预设与细调的统一载体）
# ============================================================
@dataclass(frozen=True)
class RowSpec:
    """一行最终形态的组合声明。

    调用方既可直接用 PRESET_* 常量，也可自行组合；control 可以是 str
    表示自定义控件，此时 access / reset_position 仍然生效。
    """

    control: Union[RowControl, str]
    access: RowAccess = RowAccess.READWRITE
    reset_position: Optional[ResetPosition] = ResetPosition.INLINE

    @property
    def is_custom(self) -> bool:
        return isinstance(self.control, str)

    @property
    def is_full_width(self) -> bool:
        return self.control in (RowControl.SEPARATOR, RowControl.LABEL)

    @property
    def is_inline(self) -> bool:
        """是否走「左 label + 右 field」的行内布局。

        自定义 str 控件按行内处理（默认预期）；
        CHECKBOX 虽然是行内类，但无 label 列，单独返回 False。
        """
        if self.is_custom:
            return True
        return not self.is_full_width and self.control is not RowControl.CHECKBOX

    def supports(self, param: str) -> bool:
        """形态是否接受该参数。自定义 str 控件一律返回 True。"""
        return supports(self.control, param)


# ============================================================
# 9. 常用预设
# ============================================================
PRESET_KV = RowSpec(RowControl.KV)
PRESET_KV_READONLY = RowSpec(RowControl.KV, RowAccess.READONLY)
PRESET_KV_DISABLED = RowSpec(RowControl.KV, RowAccess.DISABLED)
PRESET_MULTILINE = RowSpec(RowControl.MULTILINE)
PRESET_NUMBER = RowSpec(RowControl.NUMBER)
PRESET_PASSWORD = RowSpec(RowControl.PASSWORD)
PRESET_KEYSEQUENCE = RowSpec(RowControl.KEYSEQUENCE)
PRESET_SLIDER = RowSpec(RowControl.SLIDER)
PRESET_COMBO = RowSpec(RowControl.COMBO)
PRESET_COMBO_READONLY = RowSpec(RowControl.COMBO, RowAccess.READONLY)
PRESET_RADIO = RowSpec(RowControl.RADIO)
PRESET_CHECKBOX = RowSpec(RowControl.CHECKBOX)
PRESET_COLOR = RowSpec(RowControl.COLOR)
PRESET_PATH = RowSpec(RowControl.PATH)
PRESET_SUBFORM = RowSpec(RowControl.SUBFORM)
PRESET_SEPARATOR = RowSpec(RowControl.SEPARATOR, reset_position=None)
PRESET_LABEL = RowSpec(RowControl.LABEL, reset_position=None)
