#
"""设置页包。

对外只需要两样东西：

* :class:`Setting` —— 设置页控件（``Window`` 用它）；
* :class:`StackStation` —— 旧的可浮动 docker（Wayland 下暂时不用，保留）。

另外导出四套扩展点，配置项 / 自定义页 / 自定义行走同一套渲染：

* 加一个**页面**（:mod:`~albuswall.ui.builtin.setting.pages`）::

      from albuswall.ui.builtin.setting import page

      @page("licenses", title="Licenses", order=950)
      def build_licenses(host): ...

* 改一个**已有的设置项**（:mod:`~albuswall.ui.builtin.setting.overrides`）::

      from albuswall.ui.builtin.setting import item_patch

      @item_patch("appearance.theme", widget="choice",
                  choices_provider=lambda: ["default"])
      def apply_theme(name, item):
          return {"appearance.theme": name,
                  "appearance.theme_details.theme_path": ...}

* 往一个分组里加一条**非配置行**（:mod:`~albuswall.ui.builtin.setting.extras`）::

      from albuswall.ui.builtin.setting import ExtraSpec, register_extra

      register_extra(ExtraSpec(
          "theme_details.edit", group="appearance.theme_details",
          title="Edit", button="Edit", order=40,
      ))

* 调整**同一层里的上下顺序**：给叶子 / 子分组 / 自定义行写 ``order`` 即可，
  :mod:`~albuswall.ui.builtin.setting.layout` 会按它混排（叶子、子分组不再
  分两段渲染）。
"""

from .backend import SettingBackend
from .controller import SettingController
from .extras import (
    ExtraSpec,
    extra,
    extras_of,
    iter_extras,
    register_extra,
    unregister_extra,
)
from .lazy import LazyGroupBox
from .main import Setting
from .overrides import (
    GroupPatch,
    ItemPatch,
    apply_patches,
    group_patch,
    hide_group,
    hide_item,
    item_patch,
    register_group_patch,
    register_item_patch,
)
from .pages import PageSpec, iter_pages, page, register_page, unregister_page
from .stack import StackStation

__all__ = [
    "ExtraSpec",
    "GroupPatch",
    "ItemPatch",
    "LazyGroupBox",
    "PageSpec",
    "Setting",
    "SettingBackend",
    "SettingController",
    "StackStation",
    "apply_patches",
    "extra",
    "extras_of",
    "group_patch",
    "hide_group",
    "hide_item",
    "item_patch",
    "iter_extras",
    "iter_pages",
    "page",
    "register_extra",
    "register_group_patch",
    "register_item_patch",
    "register_page",
    "unregister_extra",
    "unregister_page",
]
