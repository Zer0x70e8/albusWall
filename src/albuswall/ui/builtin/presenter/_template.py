#
"""新 presenter 的模板（**不参与 import**，复制到新文件后按需删改）。

约定（与包内其它 presenter 一致）：

- 只接一个 view，业务规则全部推给 service；
- 信号统一走 ``SignalAdapter``，``teardown`` 一句 ``disconnect_all()``；
- 不写 try/except 吞异常，除非是「一个 presenter 坏了不该拖垮整窗」这种
  明确的降级点。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from PySide6.QtCore import QObject

from albuswall.log import getLogger

from ..utils.signal_adapter import SignalAdapter

if TYPE_CHECKING:
    from albuswall.core import Container

    from ..window.xxx import XxxView

_logger = getLogger(__name__)


class XxxPresenter(QObject):
    def __init__(self, target: "XxxView", parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._target = target
        self._adapter = SignalAdapter(self)
        self._wire()

    def setup(self, _: "Container" = None) -> None:
        return None

    def teardown(self) -> None:
        self._adapter.disconnect_all()

    def _wire(self) -> None:
        w = self._target
        self._adapter.connect(w.close_button.clicked, w.close_requested.emit)
