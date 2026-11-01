#
"""``python -m albuswall.ui.window``：不依赖业务层的窗口预览入口。"""

from __future__ import annotations

import sys

__all__ = ["main"]


def main() -> int:  # pragma: no cover - 手动预览入口
    from PySide6.QtWidgets import QApplication

    from .window import Window

    app = QApplication(sys.argv)

    # 有主题就套上；没有也能正常显示（不依赖业务层）
    # noinspection broad-exception
    try:
        # noinspection unresolved-references
        from albuswall.resources import qss

        app.setStyleSheet(qss.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 预览入口，资源缺失不影响显示
        pass

    window = Window()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
