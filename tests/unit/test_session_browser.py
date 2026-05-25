"""Unit tests for session_browser module."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from src.bot.features.session_browser import (
    derive_fallback_title,
    list_sessions_view,
    session_detail_view,
)
from src.claude.session import ClaudeSession


class TestDeriveFallbackTitle:
    def test_uses_first_prompt(self):
        result = derive_fallback_title("Help me debug this webhook", "abc12345abc")
        assert result == "Help me debug this webhook"

    def test_strips_command_message_wrapper(self):
        raw = (
            "<command-message>init</command-message><command-name>/init</command-name>"
        )
        result = derive_fallback_title(raw, "id1id2id3id")
        assert result == "Session id1id2id"

    def test_strips_command_message_keeps_following_text(self):
        raw = (
            "<command-message>continue</command-message>\n"
            "<command-name>/continue</command-name>\n"
            "Actually I want to ask about caching"
        )
        assert (
            derive_fallback_title(raw, "anyid")
            == "Actually I want to ask about caching"
        )

    def test_truncates_long_prompts(self):
        raw = "a" * 200
        result = derive_fallback_title(raw, "anyid")
        assert len(result) == 61  # 60 chars + ellipsis
        assert result.endswith("…")

    def test_no_prompt_returns_id_based(self):
        assert derive_fallback_title("", "abcdef1234") == "Session abcdef12"

    def test_none_prompt_returns_id_based(self):
        assert derive_fallback_title(None, "abcdef1234") == "Session abcdef12"


def _fake_session(sid: str, msgs: int = 5, age_min: int = 60):
    now = datetime.now(UTC) - timedelta(minutes=age_min)
    return ClaudeSession(
        session_id=sid,
        user_id=42,
        project_path=Path("/proj"),
        created_at=now,
        last_used=now,
        total_cost=0.0,
        total_turns=0,
        message_count=msgs,
        tools_used=[],
    )


class TestListSessionsView:
    @pytest.mark.asyncio
    async def test_empty_list_has_no_pagination_buttons(self):
        storage = AsyncMock()
        storage.count_user_sessions = AsyncMock(return_value=0)
        storage.get_user_sessions = AsyncMock(return_value=[])
        text, kb = await list_sessions_view(
            storage=storage, user_id=42, project_path="/proj", page=0
        )
        assert "还没有 session" in text
        assert len(kb.inline_keyboard) == 0

    @pytest.mark.asyncio
    async def test_single_page_no_pagination_buttons(self):
        storage = AsyncMock()
        storage.count_user_sessions = AsyncMock(return_value=3)
        storage.get_user_sessions = AsyncMock(
            return_value=[_fake_session(f"s{i}") for i in range(3)]
        )
        with (
            patch(
                "src.bot.features.session_browser.ClaudeIntegration.read_session_title",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "src.bot.features.session_browser._first_prompt_for",
                new_callable=AsyncMock,
                return_value="hello",
            ),
        ):
            text, kb = await list_sessions_view(
                storage=storage, user_id=42, project_path="/proj", page=0
            )
        # 3 session buttons, no pagination row
        assert len(kb.inline_keyboard) == 3
        assert all(len(row) == 1 for row in kb.inline_keyboard)
        assert kb.inline_keyboard[0][0].callback_data == "sessions:detail:s0"

    @pytest.mark.asyncio
    async def test_multi_page_has_next_button(self):
        storage = AsyncMock()
        storage.count_user_sessions = AsyncMock(return_value=25)
        storage.get_user_sessions = AsyncMock(
            return_value=[_fake_session(f"s{i}") for i in range(10)]
        )
        with patch(
            "src.bot.features.session_browser.ClaudeIntegration.read_session_title",
            new_callable=AsyncMock,
            return_value="title",
        ):
            text, kb = await list_sessions_view(
                storage=storage, user_id=42, project_path="/proj", page=0
            )
        # 10 session rows + 1 nav row
        assert len(kb.inline_keyboard) == 11
        nav_row = kb.inline_keyboard[-1]
        # Page 0: only "next"
        assert len(nav_row) == 1
        assert nav_row[0].callback_data == "sessions:list:1"

    @pytest.mark.asyncio
    async def test_middle_page_has_prev_and_next(self):
        storage = AsyncMock()
        storage.count_user_sessions = AsyncMock(return_value=25)
        storage.get_user_sessions = AsyncMock(
            return_value=[_fake_session(f"s{i}") for i in range(10)]
        )
        with patch(
            "src.bot.features.session_browser.ClaudeIntegration.read_session_title",
            new_callable=AsyncMock,
            return_value="title",
        ):
            text, kb = await list_sessions_view(
                storage=storage, user_id=42, project_path="/proj", page=1
            )
        nav_row = kb.inline_keyboard[-1]
        assert len(nav_row) == 2
        assert nav_row[0].callback_data == "sessions:list:0"
        assert nav_row[1].callback_data == "sessions:list:2"

    @pytest.mark.asyncio
    async def test_last_page_has_only_prev(self):
        storage = AsyncMock()
        storage.count_user_sessions = AsyncMock(return_value=25)
        # Page 2 has 5 sessions
        storage.get_user_sessions = AsyncMock(
            return_value=[_fake_session(f"s{i}") for i in range(5)]
        )
        with patch(
            "src.bot.features.session_browser.ClaudeIntegration.read_session_title",
            new_callable=AsyncMock,
            return_value="title",
        ):
            text, kb = await list_sessions_view(
                storage=storage, user_id=42, project_path="/proj", page=2
            )
        nav_row = kb.inline_keyboard[-1]
        assert len(nav_row) == 1
        assert nav_row[0].callback_data == "sessions:list:1"

    @pytest.mark.asyncio
    async def test_page_clamped_to_valid_range(self):
        storage = AsyncMock()
        storage.count_user_sessions = AsyncMock(return_value=5)
        storage.get_user_sessions = AsyncMock(
            return_value=[_fake_session(f"s{i}") for i in range(5)]
        )
        with patch(
            "src.bot.features.session_browser.ClaudeIntegration.read_session_title",
            new_callable=AsyncMock,
            return_value="title",
        ):
            text, kb = await list_sessions_view(
                storage=storage, user_id=42, project_path="/proj", page=99
            )
        # Clamped to page 0 (only 1 page exists)
        assert "第 1 页 / 共 1 页" in text

    @pytest.mark.asyncio
    async def test_callback_data_under_64_bytes(self):
        storage = AsyncMock()
        storage.count_user_sessions = AsyncMock(return_value=10)
        # Real-shaped UUID session ids
        storage.get_user_sessions = AsyncMock(
            return_value=[
                _fake_session("04e2d0f2-095e-4ed7-b429-66c21569830b") for _ in range(10)
            ]
        )
        with patch(
            "src.bot.features.session_browser.ClaudeIntegration.read_session_title",
            new_callable=AsyncMock,
            return_value="t",
        ):
            _, kb = await list_sessions_view(
                storage=storage, user_id=42, project_path="/proj", page=0
            )
        for row in kb.inline_keyboard:
            for btn in row:
                assert len(btn.callback_data.encode()) <= 64


class TestSessionDetailView:
    @pytest.mark.asyncio
    async def test_renders_metadata_and_three_action_buttons(self):
        storage = AsyncMock()
        storage.load_session = AsyncMock(return_value=_fake_session("abc-123", msgs=23))
        with patch(
            "src.bot.features.session_browser.ClaudeIntegration.read_session_title",
            new_callable=AsyncMock,
            return_value="my title",
        ):
            text, kb = await session_detail_view(
                storage=storage,
                user_id=42,
                session_id="abc-123",
                back_page=0,
            )
        assert "my title" in text
        assert "abc-123" in text
        assert "23" in text
        # Buttons: html / md / json / resume / back
        flat = [btn for row in kb.inline_keyboard for btn in row]
        cbs = {b.callback_data for b in flat}
        assert "sessions:view:abc-123" in cbs
        assert "sessions:resume:abc-123" in cbs
        assert "sessions:export:abc-123:md" in cbs
        assert "sessions:export:abc-123:json" in cbs
        assert "sessions:back:0" in cbs

    @pytest.mark.asyncio
    async def test_back_button_carries_page_number(self):
        storage = AsyncMock()
        storage.load_session = AsyncMock(return_value=_fake_session("abc-123"))
        with (
            patch(
                "src.bot.features.session_browser.ClaudeIntegration.read_session_title",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "src.bot.features.session_browser._first_prompt_for",
                new_callable=AsyncMock,
                return_value="hi",
            ),
        ):
            _, kb = await session_detail_view(
                storage=storage,
                user_id=42,
                session_id="abc-123",
                back_page=5,
            )
        cbs = {b.callback_data for row in kb.inline_keyboard for b in row}
        assert "sessions:back:5" in cbs

    @pytest.mark.asyncio
    async def test_cross_user_session_returns_none(self):
        storage = AsyncMock()
        # load_session is filtered by user_id and returns None on mismatch
        storage.load_session = AsyncMock(return_value=None)
        result = await session_detail_view(
            storage=storage,
            user_id=999,
            session_id="abc-123",
            back_page=0,
        )
        assert result is None
