# -*- coding: utf-8 -*-
"""``python -m albuswall.ui.widgets.widget.form_widget`` —— 可运行演示 + 自检。

默认无头：构建一份覆盖全部形态的表单、跑一遍断言、打印汇总后退出。
带 ``--gui``（别名 ``-g`` / ``--show``）才真正弹窗并进入 Qt 事件循环；
无图形环境时自动退到 offscreen 平台，因此 GUI 路径也能在 CI 里冒烟跑。

    uv run -m albuswall.ui.widgets.widget.form_widget                 # 无头自检
    uv run -m albuswall.ui.widgets.widget.form_widget --gui           # 图形界面演示
    uv run -m albuswall.ui.widgets.widget.form_widget --auto-close 2  # 闪一下自动退出
"""

from __future__ import annotations

import argparse
import os
import sys


def _has_display() -> bool:
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _prepare_platform(gui: bool) -> str:
    """决定 Qt 平台插件，返回实际使用的名字（用于汇报）。

    - ``gui=True`` 且有显示环境 → 交给 Qt 自己选（xcb / wayland ...）
    - 其余情况（无头自检，或 GUI 但无显示）→ offscreen
    """
    if gui and _has_display():
        return os.environ.get("QT_QPA_PLATFORM") or "（Qt 默认：xcb / wayland）"
    # 无头自检：不弹窗，固定 offscreen，保证与运行环境的显示服务无关
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    return "offscreen"


def build_parser() -> argparse.ArgumentParser:
    """命令行参数：默认只跑无头自检。"""
    parser = argparse.ArgumentParser(
        prog="form_widget",
        description="FormView 全形态演示：默认无头自检，加 --gui 才弹窗。",
    )
    parser.add_argument(
        "--gui", "-g", "--show",
        dest="gui", action="store_true",
        help="打开图形界面演示并进入 Qt 事件循环（无显示环境时用 offscreen 兜底）",
    )
    parser.add_argument(
        "--auto-close", dest="auto_close", type=float, metavar="SEC", default=None,
        help="图形演示 SEC 秒后自动退出（隐含 --gui），便于无头冒烟测试",
    )
    return parser


def _register_demo_control() -> None:
    """演示自定义 str 控件：3 ★ 打分。"""
    from PySide6.QtWidgets import QSpinBox, QWidget

    from .form_view import FormView

    def build_stars(parent: QWidget, options: dict) -> QWidget:
        spin = QSpinBox(parent)
        spin.setRange(1, 5)
        spin.setSuffix(" ★")
        value = options.get("value")
        spin.setValue(int(value) if value is not None else 1)
        return spin

    def read_stars(widget: QWidget) -> int:
        assert isinstance(widget, QSpinBox)
        return widget.value()

    def write_stars(widget: QWidget, raw) -> None:
        if isinstance(widget, QSpinBox):
            widget.setValue(int(raw))

    FormView.register_control(
        "stars",
        build_stars,
        read=read_stars,
        write=write_stars,
        changed_signal="valueChanged",
    )


def build_demo(window):
    """构建覆盖全部形态的演示表单，返回 FormView。"""
    from PySide6.QtCore import Qt

    from .form_view import FormView
    from .row_format import (
        PRESET_KV_READONLY, BrowseKind, ResetPosition, RowAccess, RowControl,
        ValidationMode,
    )

    form = FormView(window)

    def section(title: str) -> None:
        form.add_row(f"— {title} —", control=RowControl.LABEL,
                     wrap=False)
        form.add_row(control=RowControl.SEPARATOR)

    # ---- 1. KV：无 value / 有 value / 有 default ----
    section("KV 文本")
    h_kv_empty = form.add_row("备注（无 value 无 default）", ident="kv_empty")
    h_kv_value = form.add_row("姓名（有 value 无 default）", "张三", ident="name")
    h_kv_default = form.add_row("昵称（有 value 有 default）", "小张",
                                ident="nick", default="匿名")
    h_kv_ro = form.add_row("只读 ID", "0x9F3A", ident="uid",
                           spec=PRESET_KV_READONLY)
    h_kv_disabled = form.add_row("禁用字段", "不可编辑", ident="frozen",
                                 access=RowAccess.DISABLED)

    # ---- 2. NUMBER / MULTILINE / PASSWORD / KEYSEQUENCE ----
    section("数值与文本域")
    h_age = form.add_row("年龄（int）", 25, control=RowControl.NUMBER,
                         ident="age", minimum=0, maximum=150)
    h_weight = form.add_row("体重（float，有 default）", 60.5,
                            control=RowControl.NUMBER, ident="weight",
                            decimals=2, minimum=0.0, maximum=500.0,
                            step=0.1, default=50.0)
    h_note = form.add_row("备注（多行）", "第一行\n第二行",
                          control=RowControl.MULTILINE, ident="note",
                          height=80)
    h_pwd = form.add_row("密码", "", control=RowControl.PASSWORD,
                         ident="pwd", placeholder="请输入密码")
    h_hotkey = form.add_row("快捷键", "Ctrl+S",
                            control=RowControl.KEYSEQUENCE, ident="hotkey")

    # ---- 3. SLIDER / COMBO / RADIO / CHECKBOX ----
    section("选择类控件")
    h_volume = form.add_row("音量", 30, control=RowControl.SLIDER,
                            ident="volume", minimum=0, maximum=100, step=5)
    h_city = form.add_row("城市（显示 label、提交 value）", "sh",
                          control=RowControl.COMBO, ident="city",
                          items=[("北京", "bj"), ("上海", "sh")])
    h_city_bad = form.add_row("当前值不在选项里 → ⚠ 条目", "gz",
                              control=RowControl.COMBO, ident="city_bad",
                              items=[("北京", "bj"), ("上海", "sh")])
    h_theme = form.add_row("主题", "dark", control=RowControl.RADIO,
                           ident="theme",
                           items=[("浅色", "light"), ("深色", "dark")],
                           orientation=Qt.Orientation.Horizontal)
    h_vip = form.add_row("会员", True, control=RowControl.CHECKBOX,
                         ident="vip", text="启用会员")

    # ---- 4. COLOR / PATH / SUBFORM ----
    section("复合字段")
    h_color = form.add_row("主题色", "#3366FF", control=RowControl.COLOR,
                           ident="color")
    h_dir = form.add_row("日志目录", "/var/log", control=RowControl.PATH,
                         ident="logdir", browse_kind=BrowseKind.DIRECTORY)
    h_thumb = form.add_row(
        "缩略图（schema 子表单）", {"small": 128, "large": 512},
        control=RowControl.SUBFORM, ident="thumb",
        schema={
            "small": {"control": RowControl.NUMBER, "label": "小图",
                      "minimum": 16, "maximum": 4096},
            "large": {"control": RowControl.NUMBER, "label": "大图",
                      "minimum": 16, "maximum": 8192},
        },
    )

    # ---- 5. 重置按钮位置 ----
    section("重置按钮：INLINE / BELOW")
    h_inline = form.add_row("内联重置（默认）", "inline", ident="reset_inline",
                            default="默认值")
    h_below = form.add_row("下方重置", "below", ident="reset_below",
                           default="默认值",
                           reset_position=ResetPosition.BELOW)
    h_no_btn = form.add_row("无按钮（reset_position=None）", "no-btn",
                            ident="reset_none", default="默认值",
                            reset_position=None)

    # ---- 6. 自定义控件 / 校验时机 ----
    section("自定义控件与校验")
    h_stars = form.add_row("评分（自定义 stars 控件）", 3, control="stars",
                           ident="stars", default=1)
    h_limit = form.add_row("最多 3 个字符（ON_CHANGE 即时校验）", "abc",
                           control=RowControl.KV, ident="limit",
                           validation_mode=ValidationMode.ON_CHANGE,
                           validator=lambda v: (len(v or "") <= 3,
                                                "最多 3 个字符"))
    h_json = form.add_row("规格（JSON 语义 via transform）", {"size": 128},
                          control=RowControl.MULTILINE, ident="specs",
                          transform=__import__("json").loads)
    h_flush = form.add_row("flush 基线演示", "v1", ident="flush")

    # ---- 7. 整行收尾 ----
    form.add_row(control=RowControl.SEPARATOR)
    form.add_row("—— 演示结束 ——", control=RowControl.LABEL, wrap=True)

    return form, {
        "kv_empty": h_kv_empty, "name": h_kv_value,
        "kv_default": h_kv_default, "kv_ro": h_kv_ro,
        "kv_disabled": h_kv_disabled,
        "age": h_age, "weight": h_weight, "note": h_note, "pwd": h_pwd,
        "hotkey": h_hotkey, "volume": h_volume, "city": h_city,
        "city_bad": h_city_bad, "theme": h_theme, "vip": h_vip,
        "color": h_color, "logdir": h_dir, "thumb": h_thumb,
        "reset_inline": h_inline, "reset_below": h_below,
        "reset_none": h_no_btn, "stars": h_stars, "limit": h_limit,
        "specs": h_json, "flush": h_flush,
    }


def self_check(form, handles) -> list[str]:
    """跑一遍语义断言，返回检查项名字列表（全部通过才返回）。"""
    import json

    from .row_format import ResetPosition, RowAccess, RowControl, RowSpec
    from .row_guard import check_row_args

    done: list[str] = []

    def ok(name: str) -> None:
        done.append(name)

    # ---- 值语义 ----
    assert handles["kv_default"].initial_value() == "小张"
    assert handles["kv_default"].default_value() == "匿名"
    assert handles["kv_empty"].default_value() is None
    ok("initial / default 语义")

    # ---- 历史栈 ----
    h = handles["age"]
    h.set_value(30)
    h.set_value(35)
    assert h.get_value() == 35
    assert h.can_undo() is True
    h.undo()
    assert h.get_value() == 30, "undo 应回到 30"
    assert h.history()[-1] == 30
    ok("历史栈 undo")

    # ---- 显式 default / 自然默认 ----
    handles["weight"].set_value(88.8)
    handles["weight"].restore_default()
    assert handles["weight"].get_value() == 50.0
    h.restore_default()  # 无 default → 回落到 minimum=0
    assert h.get_value() == 0
    ok("restore_default")

    # ---- 左侧描述 label 上的状态点 ----
    name = handles["name"]
    assert name.badge_label is not None
    assert name.badge_label.text() == "姓名（有 value 无 default）"
    name.set_value("李四")
    assert name.is_dirty() and name.badge_state == "dirty", "改动后应是淡蓝点"
    name.set_value("张三")
    assert not name.is_dirty() and name.badge_state is None, "回到基线应无点"
    assert handles["weight"].is_default()
    assert handles["weight"].badge_state == "default", "恢复默认后应是绿点"
    vip = handles["vip"]
    assert vip.badge_state is None, "CHECKBOX 初始无点"
    vip.set_value(False)      # 没配 default：只标脏，不会变绿
    assert vip.badge_state == "dirty", "改动后应是淡蓝点"
    vip.set_value(True)
    assert vip.badge_state is None, "回到基线应无点"
    assert h.is_dirty() and not h.is_default(), "自然默认不等于「默认值」态"
    ok("状态点：绿=默认值 / 淡蓝=已修改")

    # ---- 读写状态 ----
    assert handles["kv_ro"].access is RowAccess.READONLY
    assert handles["kv_ro"].field.isReadOnly() is True
    assert handles["kv_disabled"].access is RowAccess.DISABLED
    assert handles["kv_disabled"].field.isEnabled() is False
    assert handles["kv_disabled"].lock_icon is not None
    handles["kv_disabled"].set_access(RowAccess.READONLY)
    assert handles["kv_disabled"].field.isReadOnly() is True
    handles["kv_disabled"].set_access(RowAccess.DISABLED, reason="示例锁定")
    assert handles["kv_disabled"].field.isEnabled() is False
    ok("RowAccess 落到控件")

    # ---- 重置按钮存在性 ----
    assert handles["reset_inline"].spec.reset_position is ResetPosition.INLINE
    assert handles["reset_below"].spec.reset_position is ResetPosition.BELOW
    assert handles["reset_none"].spec.reset_position is None
    assert handles["reset_none"]._reset_btn is None
    ok("ResetPosition / 按钮存在性")

    # ---- 控件回读 ----
    assert handles["city"].field.currentData() == "sh"
    assert handles["city_bad"].field.currentText() == "⚠ gz"
    assert handles["theme"].get_value() == "dark"
    assert handles["vip"].get_value() is True
    assert handles["volume"].get_value() == 30
    assert handles["hotkey"].get_value() == "Ctrl+S"
    assert handles["color"].get_value() == "#3366FF"
    assert handles["logdir"].get_value() == "/var/log"
    ok("各形态读回")

    # ---- SUBFORM ----
    assert handles["thumb"].get_value() == {"small": 128, "large": 512}
    handles["thumb"].set_value({"small": 256, "large": 1024})
    assert handles["thumb"].get_value() == {"small": 256, "large": 1024}
    ok("SUBFORM 嵌套读写")

    # ---- 自定义控件 ----
    assert handles["stars"].get_value() == 3
    handles["stars"].set_value(5)
    assert handles["stars"].get_value() == 5
    handles["stars"].restore_default()
    assert handles["stars"].get_value() == 1
    ok("自定义 str 控件")

    # ---- 校验时机 ----
    limit = handles["limit"]
    limit.field.setText("abcd")
    assert limit.validate() is False
    assert limit.error_text == "最多 3 个字符"
    limit.field.setText("abc")
    assert limit.validate() is True
    assert limit.error_text == ""
    ok("ValidationMode.ON_CHANGE 校验")

    # ---- transform（JSON 语义）----
    assert handles["specs"].get_value() == {"size": 128}
    handles["specs"].field.setPlainText(json.dumps({"size": 256}))
    assert handles["specs"].get_value() == {"size": 256}
    ok("transform 值语义")

    # ---- 守卫：不支持 / 互斥 / 越界 ----
    rejections = 0
    for kwargs in (
        {"control": RowControl.NUMBER, "items": [("a", 1)]},   # 参数不适用
        {"control": RowControl.SUBFORM},                        # schema/rows 二选一
        {"control": RowControl.NUMBER, "minimum": 10, "maximum": 1},
        {"control": RowControl.KV, "label_span": 9, "field_span": 9},
    ):
        try:
            form.add_row("非法行", **kwargs)
        except ValueError:
            rejections += 1
    assert rejections == 4, f"守卫应拒绝 4 次，实际 {rejections}"
    try:
        form.add_row("spec 与 control 互斥", spec=RowSpec(RowControl.KV),
                     control=RowControl.KV)
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("spec 与 control 应互斥")
    ok("row_guard 运行时守卫")

    # ---- check_row_args 公开可用 ----
    issues = check_row_args(RowSpec(RowControl.SUBFORM))
    assert any(i.kind.name == "MUTEX" for i in issues)
    ok("check_row_args 汇总 Issue")

    # ---- 行删除 ----
    before = form.row_count
    handles["city"].remove()
    assert form.row_count == before - 1
    assert handles["theme"].get_value() == "dark"
    assert handles["name"].get_value() == "张三"
    ok("删除中间行，其余句柄仍有效")

    # ---- 回归 1：标脏以「上次提交（flush）」为基线，而非 add_row 的初值 ----
    fl = handles["flush"]              # add_row(value="v1")，基线 v1
    assert not fl.is_dirty() and fl.badge_state is None
    fl.set_value("v2")
    assert fl.is_dirty() and fl.badge_state == "dirty", "偏离基线应标脏"
    fl.mark_clean()                    # 模拟一次 flush
    assert not fl.is_dirty(), "flush 后基线应前移到 v2"
    assert fl.badge_state is None, "flush 后同值不应再标脏"
    fl.set_value("v2")                 # 与基线相同
    assert fl.badge_state is None, "与基线一致不应标脏"
    fl.set_value("v3")
    assert fl.badge_state == "dirty"
    ok("标脏以 flush 基线判定（值相同不标脏）")

    # ---- 回归 2：恢复默认按钮随「修改历史」变化 ----
    ri = handles["reset_inline"]       # value="inline"，default="默认值"
    assert ri.reset_btn is not None and ri.reset_btn.isEnabled()
    ri.restore_default()
    assert ri.is_default() and not ri.reset_btn.isEnabled(), "已在默认值需禁用按钮"
    ri.set_value("改过")
    assert ri.reset_btn.isEnabled(), "偏离默认值需启用按钮"
    ri.undo()                          # 回退到「默认值」
    assert not ri.reset_btn.isEnabled(), "历史回退到默认值后按钮应禁用"
    ri.undo()                          # 再回退到 "inline"
    assert ri.reset_btn.isEnabled(), "历史回退到非默认值后按钮应启用"
    assert handles["reset_none"].reset_btn is None, "reset_position=None 不应建按钮"
    ok("恢复默认按钮随历史变化（启用 / 禁用）")

    # ---- 收集（同时把基线前移）----
    assert handles["flush"].is_dirty()
    submitted = form.collect()
    assert submitted is not None, "演示表单不应有非法行"
    assert submitted["age"] == 0
    assert submitted["thumb"] == {"small": 256, "large": 1024}
    assert submitted["stars"] == 1
    assert not handles["flush"].is_dirty(), "collect 成功后基线应前移"
    ok("collect 全量提交（并前移标脏基线）")

    return done


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    gui = bool(args.gui or args.auto_close is not None)
    platform = _prepare_platform(gui)

    from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QVBoxLayout, QWidget

    from .form_view import FormView

    _register_demo_control()

    app = QApplication.instance()
    if not isinstance(app, QApplication):
        app = QApplication([sys.argv[0]])
    app.setQuitOnLastWindowClosed(False)

    window = QWidget()
    window.setWindowTitle("FormView —— 全形态演示")
    window.resize(960, 900)
    outer = QVBoxLayout(window)
    outer.setContentsMargins(20, 20, 20, 20)

    form, handles = build_demo(window)
    outer.addWidget(form, 1)

    def do_collect() -> None:
        result = form.collect()
        if result is None:
            print("[collect] 用户选择「重新修改」，已 focus 首个非法行")
            return
        print("[collect] 全合法，提交值：")
        print(__import__("json").dumps(result, ensure_ascii=False,
                                      default=str, indent=2))

    submit = QPushButton("提交（form.collect()）")
    submit.clicked.connect(do_collect)
    outer.addWidget(submit)

    hint = QLabel(
        "提示：ON_CHANGE 行的校验是实时的；ON_COMMIT 行失焦 / 回车时才校验。",
    )
    hint.setWordWrap(True)
    outer.addWidget(hint)

    done = self_check(form, handles)
    print("=" * 62)
    print("FormView 演示自检")
    print("=" * 62)
    print(f"控件总数：{form.row_count}   可用 control："
          f"{', '.join(FormView.supported_controls())}")
    for name in done:
        print(f"  ✓ {name}")
    print(f"自检项：{len(done)} 项，全部通过")
    print("=" * 62)
    if not gui:
        print("当前为无头模式（只自检不弹窗）；加 --gui 可打开图形界面演示。")
        return 0

    from PySide6.QtCore import QTimer

    print(f"图形界面模式：QT_QPA_PLATFORM={platform}")
    window.show()
    if args.auto_close is not None:
        delay_ms = max(0, int(args.auto_close * 1000))
        QTimer.singleShot(delay_ms, app.quit)
        print(f"{args.auto_close:g} 秒后自动关闭。")
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
