"""动作 handler 的接线：上下文模式（Space / Esc）与库级守卫。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from albuswall.ui.builtin import actions as builtin_actions  # noqa: F401
from albuswall.ui.builtin import library
from albuswall.ui.builtin.keymap import registry
from albuswall.ui.builtin.keymap.model import Context


class _FakePresenters:
    def __init__(self) -> None:
        self.is_quick_preview = False
        self.is_slideshow = False
        self.detail_visible = False
        self.calls: list[str] = []

    def enter_quick_preview(self) -> bool:
        self.calls.append("enter")
        self.is_quick_preview = True
        return True

    def exit_quick_preview(self) -> bool:
        self.calls.append("exit")
        self.is_quick_preview = False
        return True

    def toggle_playback(self) -> bool:
        self.calls.append("play")
        return self.is_slideshow

    def enter_slideshow(self) -> bool:
        self.calls.append("slideshow")
        self.is_slideshow = True
        self.is_quick_preview = True
        return True

    def escape_layer(self) -> bool:
        self.calls.append("escape")
        return False


@pytest.fixture()
def presenters(monkeypatch):
    fake = _FakePresenters()
    registry.set_context(presenters=fake)
    yield fake
    registry.set_context(presenters=None)


def test_space_opens_quick_preview_from_grid(presenters):
    assert registry.trigger_action("preview.space") is True
    assert presenters.calls == ["enter"]
    assert presenters.is_quick_preview


def test_space_closes_quick_preview_first(presenters):
    presenters.is_quick_preview = True
    assert registry.trigger_action("preview.space") is True
    assert presenters.calls == ["exit"]


def test_space_in_slideshow_pauses_instead_of_closing(presenters):
    presenters.is_quick_preview = True
    presenters.is_slideshow = True
    assert registry.trigger_action("preview.space") is True
    assert presenters.calls == ["play"]
    assert presenters.is_quick_preview


def test_space_in_plain_detail_only_plays(presenters):
    presenters.detail_visible = True
    assert registry.trigger_action("preview.space") is False
    assert presenters.calls == ["play"]


def test_play_pause_action_uses_manager(presenters):
    presenters.is_slideshow = True
    assert registry.trigger_action("preview.play_pause") is True
    assert presenters.calls == ["play"]


def test_escape_goes_through_mode_layer(monkeypatch):
    fake = _FakePresenters()

    def _escape() -> bool:
        fake.calls.append("escape-mode")
        return True

    fake.escape_layer = _escape  # type: ignore[method-assign]
    registry.set_context(
        presenters=fake,
        window=SimpleNamespace(overlay=None, detail=None),
    )
    try:
        assert registry.trigger_action("window.escape") is True
        assert fake.calls == ["escape-mode"]
    finally:
        registry._context = Context()  # 还原全局上下文（含 window）


# ---------------------------------------------------------------- 库级守卫
class _FakeViewService:
    def __init__(self) -> None:
        self._scopes = {
            "00000000-0000-0000-0000-000000000001": "active",
            "00000000-0000-0000-0000-000000000002": "deleted",
        }

    def get_scope(self, album_uuid):
        return self._scopes.get(str(album_uuid))


class _FakeContainer:
    def __init__(self, **services) -> None:
        self._services = services

    def get(self, name, default=None):
        return self._services.get(name, default)


def _ctx(**services):
    return SimpleNamespace(
        window=None,
        container=_FakeContainer(**services),
        presenters=None,
        focus=None,
    )


def test_system_album_detection():
    ctx = _ctx(view_service=_FakeViewService())
    assert library.is_system_album(ctx, "00000000-0000-0000-0000-000000000002")
    assert library.is_trash_album(ctx, "00000000-0000-0000-0000-000000000002")
    assert not library.is_system_album(ctx, "11111111-1111-1111-1111-111111111111")
    assert not library.can_be_slot_target(
        ctx, "00000000-0000-0000-0000-000000000002")
    assert library.can_be_slot_target(
        ctx, "11111111-1111-1111-1111-111111111111")


def test_move_to_album_refuses_system_target():
    ctx = _ctx(view_service=_FakeViewService())
    assert library.move_to_album(
        ctx, "00000000-0000-0000-0000-000000000002",
        ("11111111-1111-1111-1111-111111111111",),
    ) is False


def test_move_to_album_without_repo_is_a_noop():
    ctx = _ctx(view_service=_FakeViewService())
    assert library.move_to_album(
        ctx, "22222222-2222-2222-2222-222222222222") is False


def test_todo_interfaces_return_false_and_never_raise():
    ctx = _ctx()
    assert library.rename_asset(ctx) is False
    assert library.export_assets(ctx) is False
    assert library.set_rating(ctx, 3) is False
    assert library.set_flag(ctx, "pick") is False
    assert library.new_album(ctx) is False
    assert library.show_advanced_filter(ctx) is False
    assert library.rename_album(
        ctx, "00000000-0000-0000-0000-000000000002", "x") is False
    assert library.delete_album(
        ctx, "00000000-0000-0000-0000-000000000002") is False


def test_quick_slot_lookup():
    ctx = _ctx()
    session = library.state()
    session.clear_undo()
    session.slots.clear()
    try:
        assert library.move_to_slot(ctx, 1) is False  # 未配置 → TODO
        assert library.set_quick_slot(ctx, 1, "33333333-3333-3333-3333-333333333333")
        assert session.slots[1] == "33333333-3333-3333-3333-333333333333"
        # 已配置但没有 repo → 静默失败（不抛）
        assert library.move_to_slot(ctx, 1) is False
    finally:
        session.slots.clear()
