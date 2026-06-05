"""Unit tests for session export."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.bot.features.session_export import ExportFormat, SessionExporter


class TestSessionExporter:
    @pytest.mark.asyncio
    async def test_export_prefers_sdk_transcript(self):
        storage = MagicMock()
        storage.sessions.is_btw_fork_session = AsyncMock(return_value=False)
        exporter = SessionExporter(storage=storage)

        session = {
            "id": "sdk-session",
            "user_id": 0,
            "created_at": datetime(2026, 6, 4, tzinfo=UTC),
            "updated_at": datetime(2026, 6, 4, 1, tzinfo=UTC),
        }
        messages = [
            {
                "id": 0,
                "role": "user",
                "content": "hello from sdk",
                "created_at": datetime(2026, 6, 4, tzinfo=UTC),
            }
        ]

        with patch(
            "src.bot.features.session_export._read_sdk_session",
            return_value=(session, messages),
        ) as read_sdk:
            exported = await exporter.export_session(
                user_id=42,
                session_id="sdk-session",
                format=ExportFormat.MARKDOWN,
                project_path="/proj",
            )

        read_sdk.assert_called_once_with("sdk-session", "/proj")
        assert "hello from sdk" in exported.content
        storage.sessions.get_session.assert_not_called()
