"""Tests for pending session edit text flows."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.bot.features.session_edit import handle_pending_session_edit


@pytest.mark.asyncio
async def test_pending_rename_consumes_message(tmp_path):
    update = MagicMock()
    update.message.text = "New title"
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.user_data = {
        "session_edit_action": {
            "action": "rename",
            "session_id": "sid",
            "project_path": str(tmp_path),
        }
    }
    settings = MagicMock()
    settings.approved_directory = Path(tmp_path)
    context.bot_data = {"settings": settings}

    with patch(
        "src.bot.features.session_edit.ClaudeIntegration.rename_sdk_session",
        new_callable=AsyncMock,
        return_value=True,
    ) as rename:
        consumed = await handle_pending_session_edit(update, context)

    assert consumed is True
    rename.assert_awaited_once_with("sid", Path(tmp_path), "New title")
    assert "session_edit_action" not in context.user_data
    update.message.reply_text.assert_awaited_once()
