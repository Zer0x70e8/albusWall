#
"""``form_widget.row_history`` —— 行值历史栈的纯逻辑用例（不依赖 Qt）。

历史栈是「undo / 状态点 / 恢复默认按钮」三处的共同基础，规则很小但容易写歪：

* 值或 action 任一变化才入栈（同值同动作不入栈）；
* 入栈会截断 redo 分支；
* 定长，超出丢最老的条目且索引同步；
* 只 undo，不 redo。
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from albuswall.ui.widgets.widget.form_widget.row_history import (  # noqa: E402
    ACTION_INIT,
    ACTION_MODIFY,
    ACTION_RESTORE,
    RowHistory,
    values_equal,
)


# ==================== values_equal ====================

@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        (None, None, True),
        (None, False, True),
        (None, 0, True),
        (None, "", True),
        (None, [], True),
        (False, None, True),
        ("", None, True),
        (0, None, True),
        ("a", "a", True),
        ("a", "b", False),
        (0, 1, False),
        ([1], [1], True),
        ([1], [2], False),
    ],
)
def test_values_equal(a, b, expected):
    assert values_equal(a, b) is expected


def test_values_equal_survives_broken_eq():
    class Explosive:
        def __eq__(self, other):
            raise RuntimeError("boom")

    assert values_equal(Explosive(), object()) is False


# ==================== RowHistory ====================

def test_starts_at_initial_value():
    history = RowHistory("v1")
    assert history.values() == ["v1"]
    assert history.current() == ("v1", ACTION_INIT)
    assert history.current_raw() == "v1"
    assert history.can_undo() is False
    assert history.can_redo() is False


def test_push_appends_and_moves_index():
    history = RowHistory("v1")
    assert history.push("v2") is True
    assert history.push("v3", ACTION_RESTORE) is True
    assert history.values() == ["v1", "v2", "v3"]
    assert history.current() == ("v3", ACTION_RESTORE)
    assert history.can_undo() is True


def test_push_skips_exact_duplicate():
    """与「当前值 + 当前动作」完全相同时不入栈。"""
    history = RowHistory("v1")
    assert history.push("v1", ACTION_INIT) is False
    history.push("v2", ACTION_MODIFY)
    assert history.push("v2", ACTION_MODIFY) is False
    assert history.values() == ["v1", "v2"]


def test_push_same_value_new_action_is_recorded():
    """值没变但动作变了（restore）仍要入栈，状态点靠 action 区分来源。"""
    history = RowHistory("v1")
    assert history.push("v1", ACTION_RESTORE) is True
    assert history.current() == ("v1", ACTION_RESTORE)


def test_undo_returns_previous_entry_and_stops_at_start():
    history = RowHistory("v1")
    history.push("v2")
    assert history.undo() == ("v1", ACTION_INIT)
    assert history.undo() is None
    assert history.can_undo() is False
    assert history.values() == ["v1"]


def test_push_after_undo_truncates_redo_branch():
    history = RowHistory("v1")
    history.push("v2")
    history.push("v3")
    history.undo()
    assert history.values() == ["v1", "v2"]
    history.push("v3b")
    assert history.values() == ["v1", "v2", "v3b"]
    assert "v3" not in history.values()


def test_limit_drops_oldest_and_keeps_index_consistent():
    history = RowHistory(0, limit=3)
    for value in range(1, 6):
        history.push(value)
    assert history.values() == [3, 4, 5]
    assert history.current_raw() == 5
    assert history.can_undo() is True
    history.undo()
    assert history.current_raw() == 4
    history.undo()
    assert history.current_raw() == 3
    assert history.undo() is None


def test_reset_replaces_everything():
    history = RowHistory("v1")
    history.push("v2")
    history.push("v3")
    history.reset("v9")
    assert history.values() == ["v9"]
    assert history.current() == ("v9", ACTION_INIT)
    assert history.can_undo() is False


def test_limit_is_at_least_one():
    history = RowHistory("v1", limit=0)
    history.push("v2")
    history.push("v3")
    assert history.values() == ["v3"]
