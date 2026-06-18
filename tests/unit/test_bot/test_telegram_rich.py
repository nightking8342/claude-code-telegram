"""Tests for the telegram_rich module (Bot API 10.1 Rich Messages wrapper)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.utils.telegram_rich import (
    RICH_MESSAGE_DRAFT_ENDPOINT,
    RICH_MESSAGE_ENDPOINT,
    send_rich_message,
    send_rich_message_draft,
)


@pytest.fixture
def mock_bot():
    bot = MagicMock()
    bot._post = AsyncMock(return_value={"message_id": 123})
    return bot


class TestSendRichMessage:
    async def test_calls_correct_endpoint(self, mock_bot):
        """send_rich_message calls the sendRichMessage endpoint."""
        await send_rich_message(mock_bot, 456, "**hello**")
        mock_bot._post.assert_called_once()
        call_args = mock_bot._post.call_args
        assert call_args[0][0] == RICH_MESSAGE_ENDPOINT

    async def test_sends_markdown_in_rich_message(self, mock_bot):
        """The rich_message param should contain the markdown."""
        await send_rich_message(mock_bot, 456, "**bold** text")
        data = mock_bot._post.call_args[0][1]
        assert data["rich_message"] == {"markdown": "**bold** text"}

    async def test_sends_chat_id(self, mock_bot):
        """chat_id should be passed correctly."""
        await send_rich_message(mock_bot, 789, "test")
        data = mock_bot._post.call_args[0][1]
        assert data["chat_id"] == 789

    async def test_reply_parameters_passed(self, mock_bot):
        """reply_parameters should be included when provided."""
        params = {"message_id": 42}
        await send_rich_message(mock_bot, 456, "test", reply_parameters=params)
        data = mock_bot._post.call_args[0][1]
        assert data["reply_parameters"] == params

    async def test_reply_parameters_omitted_when_none(self, mock_bot):
        """reply_parameters should not be in data when None."""
        await send_rich_message(mock_bot, 456, "test")
        data = mock_bot._post.call_args[0][1]
        assert "reply_parameters" not in data

    async def test_message_thread_id_passed(self, mock_bot):
        """message_thread_id should be included when provided."""
        await send_rich_message(mock_bot, 456, "test", message_thread_id=99)
        data = mock_bot._post.call_args[0][1]
        assert data["message_thread_id"] == 99

    async def test_message_thread_id_omitted_when_none(self, mock_bot):
        """message_thread_id should not be in data when None."""
        await send_rich_message(mock_bot, 456, "test")
        data = mock_bot._post.call_args[0][1]
        assert "message_thread_id" not in data

    async def test_disable_notification(self, mock_bot):
        """disable_notification should be passed when True."""
        await send_rich_message(
            mock_bot, 456, "test", disable_notification=True
        )
        data = mock_bot._post.call_args[0][1]
        assert data["disable_notification"] is True

    async def test_reply_markup_dict(self, mock_bot):
        """Dict reply_markup should be passed as-is."""
        kb = {"inline_keyboard": [[{"text": "hi", "callback_data": "x"}]]}
        await send_rich_message(mock_bot, 456, "test", reply_markup=kb)
        data = mock_bot._post.call_args[0][1]
        assert data["reply_markup"] == kb

    async def test_reply_markup_object_with_to_json(self, mock_bot):
        """PTB reply_markup objects should be serialized via to_json()."""
        kb = MagicMock()
        kb.to_json.return_value = '{"inline_keyboard": []}'
        await send_rich_message(mock_bot, 456, "test", reply_markup=kb)
        data = mock_bot._post.call_args[0][1]
        kb.to_json.assert_called_once()
        assert data["reply_markup"] == '{"inline_keyboard": []}'

    async def test_returns_api_response(self, mock_bot):
        """Should return the raw API response."""
        mock_bot._post.return_value = {"message_id": 42}
        result = await send_rich_message(mock_bot, 456, "test")
        assert result == {"message_id": 42}


class TestSendRichMessageDraft:
    async def test_calls_correct_endpoint(self, mock_bot):
        """send_rich_message_draft calls the sendRichMessageDraft endpoint."""
        await send_rich_message_draft(mock_bot, 456, 1, "partial")
        mock_bot._post.assert_called_once()
        call_args = mock_bot._post.call_args
        assert call_args[0][0] == RICH_MESSAGE_DRAFT_ENDPOINT

    async def test_sends_draft_id(self, mock_bot):
        """draft_id should be passed correctly."""
        await send_rich_message_draft(mock_bot, 456, 42, "text")
        data = mock_bot._post.call_args[0][1]
        assert data["draft_id"] == 42

    async def test_sends_markdown(self, mock_bot):
        """The rich_message param should contain the markdown."""
        await send_rich_message_draft(mock_bot, 456, 1, "streaming...")
        data = mock_bot._post.call_args[0][1]
        assert data["rich_message"] == {"markdown": "streaming..."}

    async def test_message_thread_id_passed(self, mock_bot):
        """message_thread_id should be included when provided."""
        await send_rich_message_draft(
            mock_bot, 456, 1, "text", message_thread_id=99
        )
        data = mock_bot._post.call_args[0][1]
        assert data["message_thread_id"] == 99

    async def test_message_thread_id_omitted_when_none(self, mock_bot):
        """message_thread_id should not be in data when None."""
        await send_rich_message_draft(mock_bot, 456, 1, "text")
        data = mock_bot._post.call_args[0][1]
        assert "message_thread_id" not in data

    async def test_returns_bool(self, mock_bot):
        """Should return a boolean."""
        mock_bot._post.return_value = True
        result = await send_rich_message_draft(mock_bot, 456, 1, "text")
        assert result is True
