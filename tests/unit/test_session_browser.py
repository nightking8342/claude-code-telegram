"""Unit tests for session_browser module."""

import pytest

from src.bot.features.session_browser import derive_fallback_title


class TestDeriveFallbackTitle:
    def test_uses_first_prompt(self):
        result = derive_fallback_title("Help me debug this webhook", "abc12345abc")
        assert result == "Help me debug this webhook"

    def test_strips_command_message_wrapper(self):
        raw = "<command-message>init</command-message><command-name>/init</command-name>"
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
