import sys
from PySide6.QtWidgets import QApplication, QWidget, QPushButton, QVBoxLayout
from PySide6.QtCore import Qt

# 把「通用 QPushButton 规则」和「#AlbumCloseButton 规则」都放在同一张表里
STYLESHEET = """
QPushButton {
    color: black;
    background: rgba(96, 96, 96, 0.8);   /* 关键：这里用了 background 复合属性 */
    min-width: 3em;  max-width: 3em;
    min-height: 3em; max-height: 3em;
    border: 2px solid rgba(80, 80, 80, 0.25);
    border-radius: 1.5em;
    padding: 0;
}

#AlbumCloseButton {
    min-width: 0; max-width: 16777211;
    border: 2px solid rgba(80, 80, 80, 1);
    background: blue;                    /* 关键：这里也是 background */
    color: rgba(192, 192, 192, 0.8);
}
"""

class Demo(QWidget):
    def __init__(self, mode: str):
        super().__init__()
        self.mode = mode
        lay = QVBoxLayout(self)
        btn = QPushButton("✕", self)
        btn.setObjectName("AlbumCloseButton")           # 模拟 auto_set_object_names 的结果
        btn.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        lay.addWidget(btn)

        if mode == "self":
            btn.setStyleSheet(STYLESHEET)   # 写在按钮自己身上 —— 背景生效
        elif mode == "parent":
            self.setStyleSheet(STYLESHEET)  # 写在父窗口上 —— 背景不生效（你要复现的）
        elif mode == "app":
            QApplication.instance().setStyleSheet(STYLESHEET)

app = QApplication(sys.argv)
w1 = Demo("self");   w1.setWindowTitle("写在按钮上"); w1.show()
w2 = Demo("parent"); w2.setWindowTitle("写在父窗口上"); w2.show()
sys.exit(app.exec())