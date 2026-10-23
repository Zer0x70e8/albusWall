#
"""pytest 配置：所有 Qt 测试强制离屏。

桌面会话里通常已经有 ``QT_QPA_PLATFORM=wayland;xcb``（或 ``xcb``），
测试模块里再写 ``os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")``
是**无效的**——变量已存在时 setdefault 什么也不做，``QApplication`` 就会
开在真实显示器上，测试里一 ``show()`` 就弹窗。

conftest 在收集阶段（任何 test 模块被 import、任何 PySide6 模块被 import
之前）执行，所以这里直接把变量**覆盖**成 offscreen，对 pytest 收集顺序
免疫；各测试文件里同样的覆盖是为了让 ``python test/xxx.py`` 直接跑也不弹窗。
"""

import os

os.environ["QT_QPA_PLATFORM"] = "offscreen"
