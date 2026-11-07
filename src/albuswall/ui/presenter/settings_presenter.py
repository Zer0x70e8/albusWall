#
"""设置页 presenter：配置 dump ⇄ 被动视图 ⇄ 统一写回入口。

职责是一条链路，外加一个**布局再处理**阶段：

::

    dump_schema_dict() + apply_present()            → 原始 dump
    SettingLayout / apply_layout()                  → 移动 / 自定义组件
    SettingView.set_dump_schema()                   → 渲染
    SettingView.submit_requested(mode, …)           → MainPresenter.update_settings()
    report                                          → SettingView.show_submit_result()

``SettingLayout`` 是**容器注入的运行时对象**（``container.set("setting_layout",
SettingLayout())``），不是用户配置：没有布局文件、不读写磁盘，生命周期跟着
装配根走（与主题 / 窗口状态同一类"注入进来就用的东西"）。

本模块是设置页**唯一**碰配置器的地方。视图（``albuswall.ui.setting``）仍然
只吃 dump / 通知、只吐 ``{key: typed}``；窗口只负责把页面推进浮层。

三项「再处理」
==============

1. **快捷键分组**：把 ``[shortcuts]`` 自动登记成普通的可编辑设置项
   （``ShortcutRecord`` 类型），挂进「快捷键」分组；不再单列进「未覆盖」；
2. **位置**：叶子行右键「移动到分组…」→ 只改 dump 树结构（叶子 ``key``
   保持原 qualname，写回键不变），布局对象由容器注入；
3. **自定义组件**：``＋ 组件`` → 往布局里加一个只含 label 的展示分组
   （例如「关于」）；内置的「关于」分组也由本模块追加。

布局是**展示层**，不写回配置。``LayoutClaim`` / ``claim_node()`` 只作为
内部能力保留，视图**不暴露**认领入口。
"""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QTimer, Signal

from albuswall.ui.setting import (
    LayoutClaim,
    Scope,
    SettingLayout,
    SettingTree,
    SettingView,
    SubmitMode,
    apply_layout,
    builtin_layout,
    merge_layouts,
    normalize_dump,
)
from albuswall.ui.setting.dialogs import AddComponentDialog, MoveItemDialog
from albuswall.ui.setting.result import write_back_result
from albuswall.ui.window import settings as ui_settings

from .base import Presenter, register_presenter

if TYPE_CHECKING:
    from albuswall.core.container import Container
    from albuswall.ui.window.window import Window

    from .main import MainPresenter

__all__ = ["SettingsPresenter"]

logger = logging.getLogger(__package__)


class SettingsPresenter(Presenter):
    """设置页的数据 / 提交 / 布局再处理适配器。"""

    name = "settings"

    #: 面向用户的错误（主 presenter 统一转成窗口 toast）。
    error = Signal(str)

    def __init__(
            self,
            container: Container,
            *,
            window: Window,
            parent: MainPresenter | None = None,
    ) -> None:
        super().__init__(container, parent=parent)
        self._window = window
        self._view: SettingView = window.settings_page.view
        self._last_values: dict[str, Any] = {}
        #: 布局对象由容器注入（装配根在 ``_build_settings_presenter`` 里 ``set``）。
        #: 取不到就退化为空布局：headless / 单测环境里设置页仍能渲染。
        self._layout = self._injected_layout() or SettingLayout()

    # ------------------------------------------------------------------ #
    # 生命周期 / 对外入口
    # ------------------------------------------------------------------ #
    def on_start(self) -> None:
        self._replay_claims()

        view = self._view
        view.submit_requested.connect(self._on_submit_requested)
        view.retry_requested.connect(self._on_retry)
        view.reload_requested.connect(self.reload)
        view.move_requested.connect(self._on_move_requested)
        view.reset_move_requested.connect(self._on_reset_move_requested)
        view.add_component_requested.connect(self._on_add_component_requested)

    def on_stop(self) -> None:
        view = self._view
        for signal, slot in (
            (view.submit_requested, self._on_submit_requested),
            (view.retry_requested, self._on_retry),
            (view.reload_requested, self.reload),
            (view.move_requested, self._on_move_requested),
            (view.reset_move_requested, self._on_reset_move_requested),
            (view.add_component_requested, self._on_add_component_requested),
        ):
            with contextlib.suppress(RuntimeError, TypeError):
                signal.disconnect(slot)

    def retranslate(self) -> None:
        """语言切换：如果设置页正开着，重建 dump 让文案跟着变。

        用 ``QTimer.singleShot(0)`` 延后一拍：语言切换由配置写入触发时，本
        方法可能在同一个提交调用栈里被调到；等提交结果回填完再重建，避免
        把 ``show_submit_result`` 要用的 binder 提前换掉。
        """
        if self.is_open():
            QTimer.singleShot(0, self.reload)

    def is_open(self) -> bool:
        try:
            return self._window.overlay.currentWidget() is self._window.settings_page
        except (AttributeError, RuntimeError):
            return False

    def open(self) -> None:
        """菜单 / ``app.settings``：重新读 dump 并推进浮层。"""
        if not self._load_dump():
            return
        self._window.show_settings()

    def reload(self) -> None:
        """重新 dump（视图右上角 ⟳ / 语言变化 / 布局变化后）。"""
        self._load_dump()

    # ------------------------------------------------------------------ #
    # 布局对象 / 内部 claim 重放
    # ------------------------------------------------------------------ #
    def _injected_layout(self) -> SettingLayout | None:
        """从容器取注入的布局对象（没有就返回 ``None``，由调用方退化）。"""
        layout = self.service("setting_layout")
        return layout if isinstance(layout, SettingLayout) else None

    def _replay_claims(self) -> None:
        if not self._layout.claims:
            return
        for claim in self._layout.claims:
            try:
                self._register_claim(claim, validate=False)
            except Exception:  # noqa: BLE001 - 单条坏 claim 不阻断设置页
                logger.exception(
                    "settings presenter: cannot replay claim %r", claim.qualname,
                )

    def _register_claim(
            self, claim: LayoutClaim, *, validate: bool = True,
    ) -> None:
        """把一条 claim 登记进配置器的手写覆盖表（进程级）。"""
        config = self.service("config")
        if config is None:
            raise RuntimeError("configuration is unavailable")

        from albuswall.configue.schema import (
            build_schema,
            invalidate_schema,
            register_setting_group,
            register_setting_item,
            setting_group,
            setting_item,
        )

        # 懒 import：只取纯函数展示策略，不注册命令/不拉命令行运行时。
        from albuswall.service.command.handlers._present import (
            register_present_override,
        )

        schema = build_schema(config)
        existing_group = schema.get(claim.group)
        if (
                validate and existing_group is not None
                and any(item.name == claim.name for item in existing_group.items)
        ):
            raise ValueError(
                f"设置项 {claim.name!r} 已在分组 {claim.group!r} 里存在",
            )

        if existing_group is None and claim.group_title:
            register_setting_group(
                setting_group(
                    claim.group,
                    title=claim.group_title,
                    order=claim.group_order or 900,
                ),
            )

        default = self._current_value(claim.section, claim.key)
        value_type = None if claim.typed else str
        item = setting_item(
            claim.name,
            claim.section,
            claim.key,
            label=claim.label or claim.name,
            description=claim.description,
            order=claim.order,
            default=default,
            value_type=value_type,
            file=Path(claim.target_file) if claim.target_file else None,
        )
        register_setting_item(claim.group, item, replace=True)
        register_present_override(
            claim.section,
            claim.key,
            widget=(
                claim.widget
                if claim.widget and claim.widget != "auto" else None
            ),
            advanced=claim.advanced,
        )
        invalidate_schema()

    def _current_value(self, section: str, key: str) -> Any:
        config = self.service("config")
        if config is None:
            return None
        resolver = getattr(config, "resolver", None)
        if resolver is None:
            return None
        try:
            return resolver.get_typed(section, key)
        except Exception:  # noqa: BLE001 - 未声明类型时退回原始文本
            try:
                return resolver.raw_of(section, key)
            except Exception:  # noqa: BLE001 - 读不到就当没有默认值
                return None

    # ------------------------------------------------------------------ #
    # dump
    # ------------------------------------------------------------------ #
    def _load_dump(self) -> bool:
        try:
            dump = self._build_dump()
        except Exception as exc:  # noqa: BLE001 - 配置结构坏了也要给用户看得懂的提示
            logger.exception("settings presenter: cannot build schema dump")
            self.error.emit(self.tr("无法读取配置结构：%s") % exc)
            return False
        try:
            tree = normalize_dump(dump)
        except Exception as exc:  # noqa: BLE001 - 版本不认识等结构问题
            logger.exception("settings presenter: dump rejected")
            self.error.emit(self.tr("设置结构无法渲染：%s") % exc)
            return False
        # 唯一的归一化点：presenter 建树、视图直接装（不再自己 parse 一遍）。
        self._view.set_tree(tree)
        return True

    @property
    def tree(self) -> SettingTree | None:
        """当前设置树（视图安装的那棵；未打开时为 ``None``）。"""
        return self._view.tree

    def _build_dump(self) -> dict[str, Any]:
        config = self.service("config")
        if config is None:
            raise RuntimeError("configuration is unavailable")

        self._register_shortcut_settings(config)

        from albuswall.configue.schema import dump_schema_dict
        from albuswall.service.command.handlers._present import apply_present

        dump = apply_present(
            dump_schema_dict(config=config),
            show_advanced=True,
        )
        self._format_shortcut_leaves(dump, config)
        # 内置「关于」先挂进 dump，再应用布局：这样「移动到关于」也有目标。
        # 用户自己用「＋ 组件」建了 about 分组时以用户的为准。
        if not self._layout.has_group("about"):
            self._append_about(dump, config)
        # 内置默认布局（「主题」分组）先应用，用户布局按 key 覆盖它。
        dump = apply_layout(dump, merge_layouts(builtin_layout(), self._layout))
        return dump

    # ------------------------------------------------------------------ #
    # 快捷键设置项（自动登记，不暴露「认领」）
    # ------------------------------------------------------------------ #
    def _register_shortcut_settings(self, config: Any) -> None:
        """把 ``[shortcuts]`` 的每个节点登记成普通可编辑设置项。

        它们不是 ``@configclass`` 字段（命令路径运行期才知道），但配置器已经
        通过 load hook 给它们补了 ``ShortcutRecord`` 类型。这里用跟「认领」
        相同的内部登记入口，把它们挂进一个普通的「快捷键」分组；提交时由
        ``save_values`` 的 ``ShortcutRecord`` 转换器写回用户的
        ``shortcuts.ini``。
        """
        from albuswall.common.keymap import ShortcutRecord
        from albuswall.configue.schema import (
            build_schema,
            invalidate_schema,
            register_setting_group,
            register_setting_item,
            setting_group,
            setting_item,
        )
        from albuswall.ui.keymap.config import ShortcutConfig

        resolver = getattr(config, "resolver", None)
        if resolver is None:
            return
        try:
            keys = sorted(
                key for section, key in resolver.nodes()
                if section == "shortcuts"
            )
        except Exception:  # noqa: BLE001 - 半初始化状态不阻断设置页
            return
        if not keys:
            return

        try:
            defaults = ShortcutConfig().load_defaults(strict=False)
        except Exception:  # noqa: BLE001 - 默认表坏了就只显示当前值
            defaults = {}

        registry = self.service("shortcuts")
        get_slot = getattr(registry, "get_slot", None)
        schema = build_schema(config)
        if schema.get("shortcuts") is None:
            register_setting_group(
                setting_group(
                    "shortcuts",
                    title="快捷键",
                    description=(
                        "每个命令的绑定记录；值是 JSON 数组，例如 "
                        '["Ctrl+Q"] 或 ["F11", "Ctrl+Shift+F"]。'
                    ),
                    order=900,
                ),
            )

        for order, key in enumerate(keys):
            binding = defaults.get(key) if isinstance(defaults, dict) else None
            default_text = _shortcut_record_text(binding)
            slot = get_slot(key) if callable(get_slot) else None
            help_text = str(getattr(slot, "help", "") or "")
            item = setting_item(
                key,
                "shortcuts",
                key,
                label=help_text or key,
                description="快捷键绑定记录（JSON）。空数组 [] 表示显式解绑。",
                order=order,
                default=default_text if default_text else None,
                value_type=ShortcutRecord,
            )
            register_setting_item("shortcuts", item, replace=True)
            from albuswall.service.command.handlers._present import (
                register_present_override,
            )
            register_present_override(
                "shortcuts", key, widget="text",
            )
        invalidate_schema()

    def _format_shortcut_leaves(
            self, dump: dict[str, Any], config: Any,
    ) -> None:
        """把 dump 里的 ShortcutRecord 值换成可直接编辑的 INI 文本。"""
        from albuswall.common.keymap import ShortcutRecord

        defaults: dict[str, Any] = {}
        try:
            from albuswall.ui.keymap.config import ShortcutConfig

            defaults = ShortcutConfig().load_defaults(strict=False)
        except Exception:  # noqa: BLE001
            defaults = {}

        def walk(groups: Any) -> None:
            for group in groups or ():
                for leaf in group.get("items") or ():
                    if leaf.get("section") != "shortcuts":
                        continue
                    raw = leaf.get("raw")
                    if isinstance(raw, ShortcutRecord):
                        text = raw.to_ini()
                    elif raw is None:
                        text = ""
                    else:
                        text = str(raw)
                    key = str(leaf.get("config_key") or "")
                    default_text = _shortcut_record_text(defaults.get(key))
                    leaf["value"] = text
                    leaf["raw"] = text
                    leaf["default"] = default_text or None
                    leaf["type"] = "ShortcutRecord"
                    leaf["widget"] = "text"
                    # 默认表只读，但写回目标是用户的 shortcuts.ini；对设置页
                    # 来说这一行是可编辑的（configue 的 readonly 只看 origin）。
                    leaf["readonly"] = False
                    leaf["choices"] = []
                    leaf["choice_labels"] = {}
                    leaf["constraints"] = None
                    leaf["interpolated"] = False
                walk(group.get("groups") or ())

        walk(dump.get("groups") or ())

    def _append_about(self, dump: dict[str, Any], config: Any) -> None:
        """追加内置「关于」分组（只显示一段 label，不绑定配置节点）。"""
        groups = dump.setdefault("groups", [])
        from albuswall import __author__, __title__, __version__

        base = getattr(config, "base_dir", None)
        text = "\n".join([
            f"{__title__} {__version__}",
            f"作者：{__author__}",
            f"配置目录：{base or '-'}",
        ])
        component = {
            "type": "label",
            "text": text,
            "object_name": "SettingAboutText",
        }
        existing = next(
            (group for group in groups
             if str(group.get("key") or "") == "about"),
            None,
        )
        if existing is not None:
            # 认领进内置 About 后它会变成真实 schema 分组；别把 label 弄丢。
            existing.setdefault("components", [])
            if not existing["components"]:
                existing["components"].append(component)
            return
        groups.append({
            "id": "about",
            "key": "about",
            "title": "关于",
            "description": "",
            "order": 9999,
            "editable": True,
            "advanced": False,
            "items": [],
            "groups": [],
            "components": [component],
            "item_count": 0,
        })

    # ------------------------------------------------------------------ #
    # 再处理：移动 / 组件（claim 为内部 API）
    # ------------------------------------------------------------------ #
    def claim_node(
            self,
            entry: dict[str, Any],
            *,
            group: str,
            name: str = "",
            label: str = "",
            description: str = "",
            widget: str = "",
            advanced: bool = False,
            order: int = 0,
            group_title: str = "",
            group_order: int = 900,
    ) -> bool:
        """认领一条 uncovered 节点；成功后重画并跳到新叶子。"""
        if not group_title:
            title, group_order = self._group_meta(group)
            group_title = title
        claim = LayoutClaim(
            section=str(entry.get("section") or ""),
            key=str(entry.get("key") or ""),
            group=str(group),
            name=name,
            label=label,
            description=description,
            widget=widget,
            advanced=advanced,
            order=order,
            group_title=group_title,
            group_order=group_order,
            typed=bool(entry.get("typed")),
            target_file=(
                str(entry["target_file"]) if entry.get("target_file") else None
            ),
        )
        try:
            self._register_claim(claim)
        except Exception as exc:  # noqa: BLE001 - 认领失败要提示
            logger.exception("settings presenter: cannot claim node")
            self.error.emit(self.tr("认领失败：%s") % exc)
            return False
        self._layout.add_claim(claim)
        self.reload()
        self._view.set_scope(Scope.all())
        self._view.select_anchor(claim.qualname)
        return True

    def move_item(
            self,
            key: str,
            group: str,
            *,
            order: int = 0,
            group_title: str = "",
            group_description: str = "",
            group_order: int = 0,
    ) -> bool:
        """把叶子移到目标分组（只改展示；叶子 key / 写回键不变）。"""
        current = self._leaf_group(key)
        if current is None:
            self.error.emit(self.tr("找不到配置项 %s") % key)
            return False
        if group == current and not group_title:
            self._layout.remove_move(key)
        else:
            self._layout.set_move(
                key,
                group,
                order=order,
                group_title=group_title,
                group_description=group_description,
                group_order=group_order,
            )
        self.reload()
        self._view.set_scope(Scope.all())
        self._view.select_anchor(key)
        return True

    def reset_move(self, key: str) -> bool:
        """撤销某个叶子的位置调整（落回内置默认位置）。

        「恢复默认位置」= 删掉**用户**的 move；内置默认布局（例如 ``ui.theme``
        在「主题」分组里）仍然生效，所以叶子会回到默认摆放处，而不是裸露在
        它声明的段里。
        """
        if not self._layout.remove_move(key):
            return False
        self.reload()
        self._view.set_scope(Scope.all())
        self._view.select_anchor(key)
        return True

    def add_component_group(
            self,
            key: str,
            *,
            title: str = "",
            description: str = "",
            order: int = 900,
            label: str = "",
    ) -> bool:
        """添加一个只含 label 的自定义分组。"""
        self._layout.add_component_group(
            key,
            title=title or key,
            description=description,
            order=order,
            label=label,
        )
        self.reload()
        self._view.set_scope(Scope.all())
        self._view.select_anchor(key)
        return True

    # ------------------------------------------------------------------ #
    # 对话框槽
    # ------------------------------------------------------------------ #
    def _on_move_requested(self, key: str) -> None:
        current = self._leaf_group(key)
        groups = self._group_options(exclude=current)
        dialog = MoveItemDialog(
            key, groups, parent=self._window.settings_page,
        )
        if dialog.exec() != MoveItemDialog.DialogCode.Accepted:
            return
        group, title, description, group_order, order = dialog.target()
        self.move_item(
            key,
            group,
            order=order,
            group_title=title,
            group_description=description,
            group_order=group_order,
        )

    def _on_reset_move_requested(self, key: str) -> None:
        self.reset_move(key)

    def _on_add_component_requested(self) -> None:
        dialog = AddComponentDialog(parent=self._window.settings_page)
        if dialog.exec() != AddComponentDialog.DialogCode.Accepted:
            return
        spec = dialog.spec()
        self.add_component_group(
            spec.key,
            title=spec.title,
            description=spec.description,
            order=spec.order,
            label=spec.components[0].text if spec.components else "",
        )

    # ------------------------------------------------------------------ #
    # 分组选项 / 叶子定位
    # ------------------------------------------------------------------ #
    def _group_options(
            self, *, exclude: str | None = None,
    ) -> list[tuple[str, str]]:
        """「移动到分组…」的候选分组（合成 ``__*`` 分组不参与）。"""
        tree = self.tree
        if tree is None:
            return [
                (group.key, group.title or group.key)
                for group in self._layout.groups
                if group.key != exclude
            ]
        return [
            (key, title)
            for key, title in tree.group_titles().items()
            if not key.startswith("__") and key != exclude
        ]

    def _group_meta(self, key: str) -> tuple[str, int]:
        """当前树里某个分组的 ``(title, order)``（找不到返回空/0）。"""
        tree = self.tree
        group = tree.group(key) if tree is not None else None
        if group is None:
            return "", 0
        return str(group["title"] or key), int(group["order"] or 0)

    def _leaf_group(self, key: str) -> str | None:
        """叶子所在的分组 key（找不到返回 ``None``）。"""
        tree = self.tree
        if tree is None:
            return None
        group = tree.group_of(key)
        return group["key"] if group is not None else None

    # ------------------------------------------------------------------ #
    # 提交
    # ------------------------------------------------------------------ #
    def _on_submit_requested(self, mode_text: str, values: Any) -> None:
        values = dict(values or {})
        self._last_values = values
        mode = str(mode_text)
        kwargs: dict[str, Any] = {"collect_errors": True}
        if mode == SubmitMode.DRY_RUN.value:
            kwargs["dry_run"] = True
        elif mode == SubmitMode.DRAFT.value:
            kwargs["flush"] = False

        try:
            report = self._update(values, **kwargs)
        except Exception as exc:  # noqa: BLE001 - 提交失败必须回填到界面
            logger.exception("settings presenter: submit failed")
            report = self._error_report(str(exc))
        self._view.show_submit_result(report)

    def _update(self, values: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        """走主 presenter 的统一入口（没有主 presenter 时退回配置接口）。"""
        parent = self.parent
        updater = getattr(parent, "update_settings", None)
        if callable(updater):
            return updater(values, **kwargs)
        return ui_settings.update_settings(values, **kwargs)

    def _on_retry(self) -> None:
        """footer「重试」：把仍然 dirty 的节点再落一次盘。"""
        config = self.service("config")
        if config is None:
            self._view.show_submit_result(
                self._error_report(self.tr("配置不可用，无法重试")),
            )
            return
        try:
            written = config.flush(strict=False)
            dirty = sorted(tuple(node) for node in config.dirty_nodes())
            pending = [f"[{section}]{key}" for section, key in dirty]
            last_error = getattr(config, "last_flush_error", None)
            report = write_back_result(
                written=[str(path) for path in written],
                applied=list(self._last_values),
                pending=pending,
                ok=not pending,
                error=(
                    str(last_error)
                    if pending and last_error is not None else None
                ),
            )
        except Exception as exc:  # noqa: BLE001 - 重试失败要回填，不能吞
            logger.exception("settings presenter: retry flush failed")
            report = self._error_report(str(exc))
        self._view.show_submit_result(report)

    @staticmethod
    def _error_report(message: str) -> dict[str, Any]:
        """给视图的「提交失败」结果（与真实写回报告同形）。"""
        return write_back_result(ok=False, error=str(message))

    # ------------------------------------------------------------------ #
    # 观测
    # ------------------------------------------------------------------ #
    @property
    def view(self) -> SettingView:
        return self._view

    @property
    def layout(self) -> SettingLayout:
        """容器注入的布局对象（没有文件、不落盘）。"""
        return self._layout


def _shortcut_record_text(binding: Any) -> str:
    """把 ``ShortcutBinding`` / ``ShortcutRecord`` 转成 INI 记录文本。"""
    if binding is None:
        return ""
    from albuswall.common.keymap import ShortcutRecord

    if isinstance(binding, ShortcutRecord):
        record = binding
    else:
        record = ShortcutRecord(
            keys=tuple(str(key) for key in getattr(binding, "keys", ())),
            args=tuple(getattr(binding, "args", ())),
            kwargs=dict(getattr(binding, "kwargs", {})),
        )
    try:
        return record.to_ini()
    except Exception:  # noqa: BLE001 - 坏记录只影响这一行
        return ""


# --------------------------------------------------------------------------- #
# 登记
# --------------------------------------------------------------------------- #

@register_presenter
def _build_settings_presenter(
        container: Container,
        main: MainPresenter,
) -> Presenter | None:
    window = getattr(main, "window", None)
    if window is None:
        logger.warning("settings presenter skipped: no window bound")
        return None
    if getattr(window, "settings_page", None) is None:
        logger.warning("settings presenter skipped: window has no settings page")
        return None
    # 装配根负责注入布局对象：它是运行时状态（不是用户配置），所以走容器，
    # 不走 ``setting_layout.json``。重复构造 presenter 时沿用同一份对象。
    if not isinstance(container.get("setting_layout"), SettingLayout):
        container.set("setting_layout", SettingLayout())
    return SettingsPresenter(container, window=window, parent=main)
