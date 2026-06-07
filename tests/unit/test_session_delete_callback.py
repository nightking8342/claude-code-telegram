"""Tests for session delete callback handlers."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.bot.handlers.callback import handle_sessions_callback


def _make_query(session_id: str = "abc-123"):
    """Create a mock callback query."""
    query = AsyncMock()
    query.from_user.id = 42
    query.data = f"sessions:confirm_delete:{session_id}"
    query.answer = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.message = AsyncMock()
    query.message.reply_text = AsyncMock()
    return query


def _make_context(
    current_session_id: str | None = None,
    directory: str = "/proj",
):
    """Create a mock context with bot_data and user_data."""
    context = MagicMock()
    context.user_data = {
        "current_directory": directory,
        "claude_session_id": current_session_id,
    }
    storage = AsyncMock()
    storage.load_session = AsyncMock(return_value=None)
    context.bot_data = {
        "storage": MagicMock(sessions=storage),
        "settings": MagicMock(session_timeout_hours=24, approved_directory="/proj"),
        "audit_logger": AsyncMock(),
    }
    return context


class TestConfirmDelete:
    @pytest.mark.asyncio
    async def test_shows_confirm_keyboard_for_owned_session(self):
        query = _make_query("abc-123")
        context = _make_context(current_session_id="other")
        with patch(
            "src.bot.handlers.callback._check_session_ownership",
            return_value="owned",
        ):
            await handle_sessions_callback(query, "confirm_delete:abc-123", context)
        query.edit_message_reply_markup.assert_called_once()
        call_kwargs = query.edit_message_reply_markup.call_args
        kb = call_kwargs.kwargs.get("reply_markup") or call_kwargs[1].get("reply_markup")
        flat = [btn for row in kb.inline_keyboard for btn in row]
        cbs = {b.callback_data for b in flat}
        assert "sessions:do_delete:abc-123" in cbs
        assert "sessions:detail:abc-123" in cbs

    @pytest.mark.asyncio
    async def test_blocks_delete_for_current_session(self):
        query = _make_query("abc-123")
        context = _make_context(current_session_id="abc-123")
        with patch(
            "src.bot.handlers.callback._check_session_ownership",
            return_value="owned",
        ):
            await handle_sessions_callback(query, "confirm_delete:abc-123", context)
        query.answer.assert_called_once()
        assert "当前" in query.answer.call_args[0][0]

    @pytest.mark.asyncio
    async def test_blocks_delete_for_cross_user(self):
        query = _make_query("abc-123")
        context = _make_context(current_session_id="other")
        with patch(
            "src.bot.handlers.callback._check_session_ownership",
            return_value="cross_user",
        ):
            await handle_sessions_callback(query, "confirm_delete:abc-123", context)
        query.answer.assert_called_once()
        assert "无权" in query.answer.call_args[0][0]


class TestDoDelete:
    @pytest.mark.asyncio
    async def test_executes_delete_and_returns_to_list(self):
        query = _make_query("abc-123")
        context = _make_context(current_session_id="other")
        with (
            patch(
                "src.bot.handlers.callback._check_session_ownership",
                return_value="owned",
            ),
            patch(
                "src.bot.handlers.callback.ClaudeIntegration.delete_session",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_delete,
            patch(
                "src.bot.handlers.callback.list_sessions_view",
                new_callable=AsyncMock,
                return_value=("session list text", MagicMock()),
            ),
        ):
            await handle_sessions_callback(query, "do_delete:abc-123", context)
        mock_delete.assert_awaited_once()
        query.edit_message_text.assert_called_once()
        query.answer.assert_called_once()

    @pytest.mark.asyncio
    async def test_blocks_delete_for_current_session(self):
        query = _make_query("abc-123")
        context = _make_context(current_session_id="abc-123")
        with patch(
            "src.bot.handlers.callback._check_session_ownership",
            return_value="owned",
        ):
            await handle_sessions_callback(query, "do_delete:abc-123", context)
        query.answer.assert_called_once()
        assert "当前" in query.answer.call_args[0][0]

    @pytest.mark.asyncio
    async def test_handles_delete_failure(self):
        query = _make_query("abc-123")
        context = _make_context(current_session_id="other")
        with (
            patch(
                "src.bot.handlers.callback._check_session_ownership",
                return_value="owned",
            ),
            patch(
                "src.bot.handlers.callback.ClaudeIntegration.delete_session",
                new_callable=AsyncMock,
                side_effect=RuntimeError("db locked"),
            ),
        ):
            await handle_sessions_callback(query, "do_delete:abc-123", context)
        query.message.reply_text.assert_called_once()
        assert "失败" in query.message.reply_text.call_args[0][0]

    @pytest.mark.asyncio
    async def test_blocks_missing_session(self):
        query = _make_query("abc-123")
        context = _make_context(current_session_id="other")
        with (
            patch(
                "src.bot.handlers.callback._check_session_ownership",
                return_value="missing",
            ),
            patch(
                "src.bot.handlers.callback.list_sessions_view",
                new_callable=AsyncMock,
                return_value=("session list text", MagicMock()),
            ),
        ):
            await handle_sessions_callback(query, "do_delete:abc-123", context)
        query.answer.assert_called_once()
        assert "不存在" in query.answer.call_args[0][0] or "已删除" in query.answer.call_args[0][0]
