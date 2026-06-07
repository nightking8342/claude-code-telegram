"""Tests for ClaudeIntegration.delete_session()."""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from src.claude.facade import ClaudeIntegration


class TestDeleteSession:
    @pytest.mark.asyncio
    async def test_deletes_sdk_jsonl_and_sqlite_record(self):
        """Both layers are cleaned up on success."""
        mock_storage = AsyncMock()
        with patch(
            "claude_agent_sdk.delete_session"
        ) as mock_sdk_delete:
            result = await ClaudeIntegration.delete_session(
                session_id="abc-123",
                project_path=Path("/proj"),
                session_storage=mock_storage,
            )
        assert result is True
        mock_sdk_delete.assert_called_once_with(
            "abc-123", directory=str(Path("/proj"))
        )
        mock_storage.delete_session.assert_awaited_once_with("abc-123")

    @pytest.mark.asyncio
    async def test_sdk_file_not_found_still_deletes_sqlite(self):
        """If SDK JSONL is already gone, SQLite cleanup still runs."""
        mock_storage = AsyncMock()
        with patch(
            "claude_agent_sdk.delete_session",
            side_effect=FileNotFoundError("no jsonl"),
        ):
            result = await ClaudeIntegration.delete_session(
                session_id="abc-123",
                project_path=Path("/proj"),
                session_storage=mock_storage,
            )
        assert result is True
        mock_storage.delete_session.assert_awaited_once_with("abc-123")

    @pytest.mark.asyncio
    async def test_sdk_import_error_skips_to_sqlite(self):
        """If SDK raises ImportError, skip and delete SQLite."""
        mock_storage = AsyncMock()
        with patch(
            "claude_agent_sdk.delete_session",
            side_effect=ImportError("no module"),
        ):
            result = await ClaudeIntegration.delete_session(
                session_id="abc-123",
                project_path=Path("/proj"),
                session_storage=mock_storage,
            )
        assert result is True
        mock_storage.delete_session.assert_awaited_once_with("abc-123")

    @pytest.mark.asyncio
    async def test_sdk_unexpected_error_still_deletes_sqlite(self):
        """If SDK raises an unexpected error, SQLite cleanup still runs."""
        mock_storage = AsyncMock()
        with patch(
            "claude_agent_sdk.delete_session",
            side_effect=RuntimeError("boom"),
        ):
            result = await ClaudeIntegration.delete_session(
                session_id="abc-123",
                project_path=Path("/proj"),
                session_storage=mock_storage,
            )
        assert result is True
        mock_storage.delete_session.assert_awaited_once_with("abc-123")

    @pytest.mark.asyncio
    async def test_sqlite_failure_raises(self):
        """If SQLite deletion fails, the exception propagates."""
        mock_storage = AsyncMock()
        mock_storage.delete_session.side_effect = RuntimeError("db locked")
        with patch("claude_agent_sdk.delete_session"):
            with pytest.raises(RuntimeError, match="db locked"):
                await ClaudeIntegration.delete_session(
                    session_id="abc-123",
                    project_path=Path("/proj"),
                    session_storage=mock_storage,
                )
