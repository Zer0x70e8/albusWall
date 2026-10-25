#
"""ViewModel：snapshot 结构 / 搜索 token / 无 Qt 依赖。"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from albuswall.ui.builtin.keymap.model import Action, Binding
from albuswall.ui.builtin.keymap.viewmodel import KeymapViewModel


class FakeStore:
    def __init__(self, actions, final, defaults=None):
        self._actions = {a.id: a for a in actions}
        self._final = final
        self._defaults = defaults or final

    def iter_actions(self):
        return iter(self._actions.values())

    def get_action(self, action_id):
        return self._actions.get(action_id)

    def effective_bindings(self):
        return self._final

    def default_bindings(self):
        return self._defaults

    def subscribe(self, listener):
        return lambda: None


def _action(action_id, *, component="window.detail", seq=1) -> Action:
    return Action(
        id=action_id, label=f"Label {action_id}", description=f"Does {action_id}",
        keys=("left",), component=component,
        handler=lambda ctx: True, signature=None, takes_context=True, seq=seq,
    )


def _viewmodel() -> KeymapViewModel:
    first = _action("viewer.next", seq=1)
    second = _action("window.open_page", component="window", seq=2)
    final = {
        "left": (Binding("viewer.next"),),
        "ctrl+p": (
            Binding("viewer.next"),
            Binding("window.open_page", (("page", "settings"),)),
        ),
    }
    defaults = {"left": (Binding("viewer.next"),)}
    return KeymapViewModel(FakeStore([first, second], final, defaults))


def test_snapshot_structure():
    rows = _viewmodel().snapshot()
    assert [row.key for row in rows] == ["ctrl+p", "left"]
    multi = rows[0]
    assert [op.action_id for op in multi.operations] == [
        "viewer.next", "window.open_page"]
    assert multi.operations[1].args == (("page", "settings"),)
    assert multi.customized is True
    assert rows[1].customized is False


def test_tokens_cover_key_and_children():
    rows = _viewmodel().snapshot()
    assert rows[1].tokens == ("left",)
    haystack = rows[0].haystack
    for token in ("ctrl+p", "viewer.next", "label viewer.next",
                  "does window.open_page", "window", "settings"):
        assert token in haystack


def test_viewmodel_never_imports_qt():
    script = r"""
import sys
class BlockQt:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] == "PySide6":
            raise ImportError("blocked")
        return None
sys.meta_path.insert(0, BlockQt())
from albuswall.ui.builtin.keymap.viewmodel import KeymapViewModel
from albuswall.ui.builtin.keymap.model import registry
rows = KeymapViewModel(registry).snapshot()
assert isinstance(rows, tuple)
assert "PySide6" not in sys.modules
print("OK")
"""
    src = Path(__file__).resolve().parents[2] / "src"
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True,
        env={"PYTHONPATH": str(src)}, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout
