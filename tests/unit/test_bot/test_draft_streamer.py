"""Tests for the DraftStreamer module.

Covers:
- Text accumulation and retrieval
- Tool line accumulation and composition
- Throttle interval respected
- 4096-char truncation with ellipsis prefix
- Self-disabling on API error
- flush() force-sends immediately
- Draft ID generation is non-zero
- Integration with orchestrator callback
"""

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.utils.draft_streamer import (
    _MAX_TOOL_LINES,
    DraftStreamer,
    generate_draft_id,
)
from src.utils.constants import TELEGRAM_MAX_MESSAGE_LENGTH, TELEGRAM_RICH_MESSAGE_MAX_LENGTH

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_bot():
    bot = MagicMock()
    bot.send_message_draft = AsyncMock()
    return bot


@pytest.fixture
def streamer(mock_bot):
    return DraftStreamer(
        bot=mock_bot,
        chat_id=123,
        draft_id=42,
        throttle_interval=0.3,
    )


# ---------------------------------------------------------------------------
# generate_draft_id
# ---------------------------------------------------------------------------


class TestGenerateDraftId:
    def test_non_zero(self):
        """Draft ID must always be non-zero."""
        for _ in range(100):
            did = generate_draft_id()
            assert did != 0

    def test_positive(self):
        """Draft ID must be positive."""
        for _ in range(100):
            did = generate_draft_id()
            assert did > 0

    def test_successive_calls_differ(self):
        """Successive calls should generally produce different draft IDs."""
        ids = {generate_draft_id() for _ in range(50)}
        # Not all identical (statistical — extremely unlikely to fail)
        assert len(ids) > 1


# ---------------------------------------------------------------------------
# DraftStreamer basics
# ---------------------------------------------------------------------------


class TestDraftStreamerAccumulation:
    async def test_append_accumulates_text(self, streamer, mock_bot):
        """Text is accumulated internally."""
        # First call within throttle — send happens immediately (last_send_time=0)
        await streamer.append_text("Hello ")
        assert streamer._accumulated_text == "Hello "
        mock_bot.send_message_draft.assert_called_once()

    async def test_append_empty_string_ignored(self, streamer, mock_bot):
        """Empty string should not trigger a send."""
        await streamer.append_text("")
        mock_bot.send_message_draft.assert_not_called()

    async def test_append_when_disabled(self, streamer, mock_bot):
        """Disabled streamer should not accumulate or send."""
        streamer._enabled = False
        await streamer.append_text("test")
        assert streamer._accumulated_text == ""
        mock_bot.send_message_draft.assert_not_called()


class TestDraftStreamerThrottle:
    async def test_throttle_prevents_rapid_sends(self, streamer, mock_bot):
        """Second append within throttle interval should not trigger send."""
        await streamer.append_text("first")
        mock_bot.send_message_draft.reset_mock()

        # Immediately append again — within throttle
        await streamer.append_text(" second")
        mock_bot.send_message_draft.assert_not_called()
        assert streamer._accumulated_text == "first second"

    async def test_sends_after_throttle_interval(self, streamer, mock_bot):
        """Send should happen once throttle interval has elapsed."""
        await streamer.append_text("first")
        mock_bot.send_message_draft.reset_mock()

        # Simulate time passing beyond throttle
        streamer._last_send_time = time.time() - 1.0
        await streamer.append_text(" second")
        mock_bot.send_message_draft.assert_called_once()
        call_kwargs = mock_bot.send_message_draft.call_args[1]
        assert call_kwargs["text"] == "first second"


class TestDraftStreamerTruncation:
    async def test_long_text_truncated_with_ellipsis(self, streamer, mock_bot):
        """Text longer than 4096 chars should be tail-truncated with ellipsis."""
        long_text = "x" * 5000
        streamer._accumulated_text = long_text
        await streamer.flush()

        call_kwargs = mock_bot.send_message_draft.call_args[1]
        sent_text = call_kwargs["text"]
        assert len(sent_text) == TELEGRAM_MAX_MESSAGE_LENGTH
        assert sent_text[0] == "\u2026"
        # The rest should be the tail of the original
        assert sent_text[1:] == long_text[-(4096 - 1) :]

    async def test_exact_limit_not_truncated(self, streamer, mock_bot):
        """Text exactly at limit should not be truncated."""
        exact_text = "y" * TELEGRAM_MAX_MESSAGE_LENGTH
        streamer._accumulated_text = exact_text
        await streamer.flush()

        call_kwargs = mock_bot.send_message_draft.call_args[1]
        assert call_kwargs["text"] == exact_text


class TestDraftStreamerSelfDisable:
    async def test_api_error_disables_streamer(self, streamer, mock_bot):
        """API error should disable the streamer silently."""
        mock_bot.send_message_draft.side_effect = Exception("API error")
        await streamer.append_text("test")
        assert not streamer._enabled

    async def test_subsequent_sends_skipped_after_disable(self, streamer, mock_bot):
        """After disabling, no more sends should happen."""
        mock_bot.send_message_draft.side_effect = Exception("API error")
        await streamer.append_text("first")
        mock_bot.send_message_draft.reset_mock()
        mock_bot.send_message_draft.side_effect = None

        await streamer.append_text("second")
        mock_bot.send_message_draft.assert_not_called()


class TestDraftStreamerFlush:
    async def test_flush_sends_immediately(self, streamer, mock_bot):
        """flush() should send the current text regardless of throttle."""
        streamer._accumulated_text = "buffered text"
        streamer._last_send_time = time.time()  # pretend we just sent
        await streamer.flush()
        mock_bot.send_message_draft.assert_called_once()
        call_kwargs = mock_bot.send_message_draft.call_args[1]
        assert call_kwargs["text"] == "buffered text"

    async def test_flush_empty_is_noop(self, streamer, mock_bot):
        """flush() with no accumulated text should be a no-op."""
        await streamer.flush()
        mock_bot.send_message_draft.assert_not_called()

    async def test_flush_whitespace_only_is_noop(self, streamer, mock_bot):
        """flush() with only whitespace should be a no-op."""
        streamer._accumulated_text = "   \n\t  "
        await streamer.flush()
        mock_bot.send_message_draft.assert_not_called()

    async def test_flush_when_disabled_is_noop(self, streamer, mock_bot):
        """flush() should not send when streamer is disabled."""
        streamer._enabled = False
        streamer._accumulated_text = "some text"
        await streamer.flush()
        mock_bot.send_message_draft.assert_not_called()


class TestDraftStreamerThreadId:
    async def test_thread_id_passed_when_set(self, mock_bot):
        """message_thread_id should be included in the API call."""
        streamer = DraftStreamer(
            bot=mock_bot,
            chat_id=123,
            draft_id=42,
            message_thread_id=999,
        )
        await streamer.append_text("hello")
        call_kwargs = mock_bot.send_message_draft.call_args[1]
        assert call_kwargs["message_thread_id"] == 999

    async def test_thread_id_omitted_when_none(self, mock_bot):
        """message_thread_id should not be in kwargs when None."""
        streamer = DraftStreamer(
            bot=mock_bot,
            chat_id=123,
            draft_id=42,
            message_thread_id=None,
        )
        await streamer.append_text("hello")
        call_kwargs = mock_bot.send_message_draft.call_args[1]
        assert "message_thread_id" not in call_kwargs


# ---------------------------------------------------------------------------
# Tool lines
# ---------------------------------------------------------------------------


class TestDraftStreamerToolLines:
    async def test_append_tool_sends_draft(self, streamer, mock_bot):
        """append_tool should trigger a draft send (first call, throttle=0)."""
        await streamer.append_tool("\U0001f4d6 Read")
        mock_bot.send_message_draft.assert_called_once()
        call_kwargs = mock_bot.send_message_draft.call_args[1]
        assert call_kwargs["text"] == "\U0001f4d6 Read"

    async def test_append_tool_empty_ignored(self, streamer, mock_bot):
        """Empty tool line should not trigger a send."""
        await streamer.append_tool("")
        mock_bot.send_message_draft.assert_not_called()

    async def test_append_tool_when_disabled(self, streamer, mock_bot):
        """Disabled streamer should not accumulate tools."""
        streamer._enabled = False
        await streamer.append_tool("\U0001f527 Grep")
        assert streamer._tool_lines == []
        mock_bot.send_message_draft.assert_not_called()

    async def test_tool_throttle(self, streamer, mock_bot):
        """Second tool within throttle should not trigger send."""
        await streamer.append_tool("\U0001f4d6 Read")
        mock_bot.send_message_draft.reset_mock()

        await streamer.append_tool("\U0001f527 Grep")
        mock_bot.send_message_draft.assert_not_called()
        assert len(streamer._tool_lines) == 2


class TestDraftStreamerComposition:
    async def test_tools_only(self, streamer, mock_bot):
        """Draft with only tool lines shows tools separated by blank lines."""
        streamer._tool_lines = ["\U0001f4d6 Read", "\U0001f527 Grep"]
        await streamer.flush()
        call_kwargs = mock_bot.send_message_draft.call_args[1]
        assert call_kwargs["text"] == "\U0001f4d6 Read\n\n\U0001f527 Grep"

    async def test_text_only(self, streamer, mock_bot):
        """Draft with only text shows just text (no separator)."""
        streamer._accumulated_text = "Hello world"
        await streamer.flush()
        call_kwargs = mock_bot.send_message_draft.call_args[1]
        assert call_kwargs["text"] == "Hello world"

    async def test_tools_plus_text(self, streamer, mock_bot):
        """Draft with both tools and text shows tools, blank line, text."""
        streamer._tool_lines = ["\U0001f4d6 Read"]
        streamer._accumulated_text = "Response text"
        await streamer.flush()
        call_kwargs = mock_bot.send_message_draft.call_args[1]
        assert call_kwargs["text"] == "\U0001f4d6 Read\n\nResponse text"

    async def test_tool_overflow_shows_count(self, streamer, mock_bot):
        """More than _MAX_TOOL_LINES tools shows overflow count."""
        streamer._tool_lines = [f"tool_{i}" for i in range(_MAX_TOOL_LINES + 5)]
        await streamer.flush()
        call_kwargs = mock_bot.send_message_draft.call_args[1]
        text = call_kwargs["text"]
        assert text.startswith("... +5 more")
        # Should show the last _MAX_TOOL_LINES entries
        assert f"tool_{_MAX_TOOL_LINES + 4}" in text

    async def test_flush_tools_only_sends(self, streamer, mock_bot):
        """flush() with only tool lines should still send."""
        streamer._tool_lines = ["\U0001f4d6 Read"]
        streamer._last_send_time = time.time()
        await streamer.flush()
        mock_bot.send_message_draft.assert_called_once()

    async def test_small_overflow_no_notice(self, streamer, mock_bot):
        """Overflow of 1-2 lines should not show '... +N more' notice."""
        streamer._tool_lines = [f"tool_{i}" for i in range(_MAX_TOOL_LINES + 2)]
        await streamer.flush()
        call_kwargs = mock_bot.send_message_draft.call_args[1]
        text = call_kwargs["text"]
        assert "... +" not in text


class TestDraftStreamerMidStreamDisable:
    async def test_append_noop_after_mid_stream_disable(self, streamer, mock_bot):
        """After self-disable mid-stream, append_text/append_tool don't accumulate."""
        mock_bot.send_message_draft.side_effect = [None, Exception("flake")]
        await streamer.append_tool("tool1")  # succeeds, _tool_lines=["tool1"]
        # Expire throttle so the next append actually triggers _send_draft
        streamer._last_send_time = 0.0
        await streamer.append_text("text")  # fails on send, disables

        assert not streamer._enabled
        mock_bot.send_message_draft.reset_mock()

        await streamer.append_text("more")
        await streamer.append_tool("tool2")
        await streamer.flush()

        # No further API calls after disable
        mock_bot.send_message_draft.assert_not_called()
        # State frozen at point of disable — no new data accumulated
        assert "more" not in streamer._accumulated_text
        assert "tool2" not in streamer._tool_lines


# ---------------------------------------------------------------------------
# Rich Message mode
# ---------------------------------------------------------------------------


class TestDraftStreamerRichMode:
    """Tests for rich message draft streaming (Bot API 10.1)."""

    @pytest.fixture
    def rich_streamer(self, mock_bot):
        from unittest.mock import AsyncMock

        # Patch the import inside _send_draft
        mock_send = AsyncMock(return_value=True)
        streamer = DraftStreamer(
            bot=mock_bot,
            chat_id=123,
            draft_id=42,
            throttle_interval=0.3,
            use_rich_messages=True,
        )
        # Mock the send_rich_message_draft function
        import src.bot.utils.telegram_rich as rich_mod

        rich_mod.send_rich_message_draft = mock_send
        return streamer

    async def test_rich_mode_calls_send_rich_message_draft(
        self, rich_streamer, mock_bot
    ):
        """Rich mode should call send_rich_message_draft instead of send_message_draft."""
        await rich_streamer.append_text("hello **bold**")
        mock_bot.send_message_draft.assert_not_called()

    async def test_rich_mode_uses_32k_truncation(self, rich_streamer, mock_bot):
        """Rich mode should truncate at 32768 chars, not 4096."""
        long_text = "x" * 40000
        rich_streamer._accumulated_text = long_text
        await rich_streamer.flush()

        import src.bot.utils.telegram_rich as rich_mod

        call_args = rich_mod.send_rich_message_draft.call_args
        # The markdown arg is positional: (bot, chat_id, draft_id, markdown, ...)
        markdown = call_args[0][3]
        assert len(markdown) == TELEGRAM_RICH_MESSAGE_MAX_LENGTH
        assert markdown[0] == "…"

    async def test_rich_mode_compose_draft_preserves_markdown(
        self, rich_streamer
    ):
        """Draft composition should preserve raw markdown in rich mode."""
        rich_streamer._accumulated_text = "# Title\n\n**bold** and `code`"
        draft = rich_streamer._compose_draft()
        assert "# Title" in draft
        assert "**bold**" in draft
        assert "`code`" in draft

    async def test_rich_mode_self_disables_on_error(
        self, rich_streamer, mock_bot
    ):
        """Rich mode should self-disable on API error."""
        import src.bot.utils.telegram_rich as rich_mod

        rich_mod.send_rich_message_draft = AsyncMock(
            side_effect=Exception("API error")
        )
        await rich_streamer.append_text("test")
        assert not rich_streamer._enabled

    async def test_rich_mode_flush_when_disabled_is_noop(
        self, rich_streamer, mock_bot
    ):
        """Disabled rich streamer should not send."""
        rich_streamer._enabled = False
        rich_streamer._accumulated_text = "text"
        await rich_streamer.flush()
        mock_bot.send_message_draft.assert_not_called()

    async def test_rich_mode_thread_id_passed(self, mock_bot):
        """message_thread_id should be passed in rich mode."""
        import src.bot.utils.telegram_rich as rich_mod

        mock_send = AsyncMock(return_value=True)
        rich_mod.send_rich_message_draft = mock_send

        streamer = DraftStreamer(
            bot=mock_bot,
            chat_id=123,
            draft_id=42,
            message_thread_id=999,
            use_rich_messages=True,
        )
        await streamer.append_text("hello")
        call_args = mock_send.call_args
        # Check keyword arg
        assert call_args[1].get("message_thread_id") == 999
