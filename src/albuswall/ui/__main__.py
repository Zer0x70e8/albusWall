#
""""""

import sys

from PySide6.QtWidgets import QApplication

from .window import Window

def main() -> int:
    app = QApplication(sys.argv)
    window = Window()

    window.show()
    return app.exec()

if __name__ == "__main__":
    SystemExit(main())
