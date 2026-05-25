"""Tests for CLI session scanning and merging into /sessions browser."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from src.bot.features.session_browser import list_sessions_view, session_detail_view
from src.claude.facade import ClaudeIntegration


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for obj in entries:
            f.write(json.dumps(obj) + "\n")


def _make_entry(
    entry_type: str, timestamp: str = "2026-05-20T10:00:00Z", **extra
) -> dict:
    obj = {"type": entry_type, "timestamp": timestamp, **extra}
    return obj


class TestScanCliSessions:
    @pytest.mark.asyncio
    async def test_scan_finds_sessions(self, tmp_path):
        proj_dir = tmp_path / ".claude" / "projects" / "test-proj"
        proj_dir.mkdir(parents=True)
        sid = "abc-123-def"
        _write_jsonl(
            proj_dir / f"{sid}.jsonl",
            [
                _make_entry("system"),
                _make_entry("ai-title", aiTitle="My Session Title"),
                _make_entry("user"),
                _make_entry("assistant"),
                _make_entry("user"),
            ],
        )

        with patch(
            "src.claude.facade.ClaudeIntegration._encode_project_path",
            return_value="test-proj",
        ), patch("os.path.expanduser", return_value=str(tmp_path)):
            results = await ClaudeIntegration.scan_cli_sessions(Path("/fake"))

        assert len(results) == 1
        assert results[0]["session_id"] == sid
        assert results[0]["ai_title"] == "My Session Title"
        assert results[0]["message_count"] == 2
        assert results[0]["created_at"] is not None

    @pytest.mark.asyncio
    async def test_scan_empty_directory(self, tmp_path):
        proj_dir = tmp_path / ".claude" / "projects" / "empty"
        proj_dir.mkdir(parents=True)

        with patch(
            "src.claude.facade.ClaudeIntegration._encode_project_path",
            return_value="empty",
        ), patch("os.path.expanduser", return_value=str(tmp_path)):
            results = await ClaudeIntegration.scan_cli_sessions(Path("/fake"))

        assert results == []

    @pytest.mark.asyncio
    async def test_scan_missing_directory(self, tmp_path):
        with patch(
            "src.claude.facade.ClaudeIntegration._encode_project_path",
            return_value="no-such-dir",
        ), patch("os.path.expanduser", return_value=str(tmp_path)):
            results = await ClaudeIntegration.scan_cli_sessions(Path("/fake"))

        assert results == []

    @pytest.mark.asyncio
    async def test_scan_skips_tiny_files(self, tmp_path):
        proj_dir = tmp_path / ".claude" / "projects" / "tiny"
        proj_dir.mkdir(parents=True)
        (proj_dir / "tiny-session.jsonl").write_text("{}\n")
        # File is only 3 bytes, should be skipped

        with patch(
            "src.claude.facade.ClaudeIntegration._encode_project_path",
            return_value="tiny",
        ), patch("os.path.expanduser", return_value=str(tmp_path)):
            results = await ClaudeIntegration.scan_cli_sessions(Path("/fake"))

        assert results == []

    @pytest.mark.asyncio
    async def test_scan_skips_bot_sessions(self, tmp_path):
        """Sessions created by bot (entrypoint=sdk-py) should be excluded."""
        proj_dir = tmp_path / ".claude" / "projects" / "test-proj"
        proj_dir.mkdir(parents=True)
        # CLI session — should be included
        _write_jsonl(
            proj_dir / "cli-sid.jsonl",
            [
                _make_entry("system", entrypoint="cli"),
                _make_entry("user"),
            ],
        )
        # Bot session — should be excluded
        _write_jsonl(
            proj_dir / "bot-sid.jsonl",
            [
                _make_entry("system", entrypoint="sdk-py"),
                _make_entry("user"),
                _make_entry("assistant"),
            ],
        )
        # SDK+CLI session — should also be excluded
        _write_jsonl(
            proj_dir / "mixed-sid.jsonl",
            [
                _make_entry("system", entrypoint="sdk-cli"),
                _make_entry("user"),
            ],
        )

        with patch(
            "src.claude.facade.ClaudeIntegration._encode_project_path",
            return_value="test-proj",
        ), patch("os.path.expanduser", return_value=str(tmp_path)):
            results = await ClaudeIntegration.scan_cli_sessions(Path("/fake"))

        assert len(results) == 1
        assert results[0]["session_id"] == "cli-sid"

    @pytest.mark.asyncio
    async def test_scan_no_title(self, tmp_path):
        proj_dir = tmp_path / ".claude" / "projects" / "notitle"
        proj_dir.mkdir(parents=True)
        _write_jsonl(
            proj_dir / "no-title-session.jsonl",
            [_make_entry("system"), _make_entry("user")],
        )

        with patch(
            "src.claude.facade.ClaudeIntegration._encode_project_path",
            return_value="notitle",
        ), patch("os.path.expanduser", return_value=str(tmp_path)):
            results = await ClaudeIntegration.scan_cli_sessions(Path("/fake"))

        assert len(results) == 1
        assert results[0]["ai_title"] is None


class TestListSessionsMerge:
    @pytest.mark.asyncio
    async def test_cli_sessions_merged_with_db(self, tmp_path):
        """CLI sessions should appear alongside DB sessions."""
        storage = MagicMock()
        storage.count_user_sessions = AsyncMock(return_value=1)
        storage.get_user_sessions = AsyncMock(
            return_value=[
                SimpleNamespace(
                    session_id="db-session",
                    last_used=datetime(2026, 5, 20, 12, 0, tzinfo=UTC),
                    message_count=5,
                )
            ]
        )

        cli_data = [
            {
                "session_id": "cli-session",
                "ai_title": "CLI Title",
                "created_at": datetime(2026, 5, 19, 10, 0, tzinfo=UTC),
                "message_count": 3,
                "last_used": datetime(2026, 5, 21, 8, 0, tzinfo=UTC),
            }
        ]

        with patch(
            "src.bot.features.session_browser.ClaudeIntegration.scan_cli_sessions",
            new_callable=AsyncMock,
            return_value=cli_data,
        ), patch(
            "src.bot.features.session_browser.ClaudeIntegration.read_session_title",
            new_callable=AsyncMock,
            return_value="Title",
        ):
            text, kb = await list_sessions_view(
                storage=storage,
                user_id=42,
                project_path="/proj",
                page=0,
            )

        # Both sessions should appear
        assert "共 2 个" in text
        buttons = [b for row in kb.inline_keyboard for b in row]
        cbs = [b.callback_data for b in buttons]
        assert "sessions:detail:db-session" in cbs
        assert "sessions:detail:cli-session" in cbs

    @pytest.mark.asyncio
    async def test_db_session_takes_precedence_over_cli(self, tmp_path):
        """If same session_id exists in both DB and CLI, DB version wins."""
        storage = MagicMock()
        storage.count_user_sessions = AsyncMock(return_value=1)
        storage.get_user_sessions = AsyncMock(
            return_value=[
                SimpleNamespace(
                    session_id="shared-session",
                    last_used=datetime(2026, 5, 20, 12, 0, tzinfo=UTC),
                    message_count=10,
                )
            ]
        )

        cli_data = [
            {
                "session_id": "shared-session",
                "ai_title": "CLI Title",
                "created_at": datetime(2026, 5, 19, 10, 0, tzinfo=UTC),
                "message_count": 3,
                "last_used": datetime(2026, 5, 21, 8, 0, tzinfo=UTC),
            }
        ]

        with patch(
            "src.bot.features.session_browser.ClaudeIntegration.scan_cli_sessions",
            new_callable=AsyncMock,
            return_value=cli_data,
        ), patch(
            "src.bot.features.session_browser.ClaudeIntegration.read_session_title",
            new_callable=AsyncMock,
            return_value="Title",
        ):
            text, kb = await list_sessions_view(
                storage=storage,
                user_id=42,
                project_path="/proj",
                page=0,
            )

        # Only 1 session (deduped)
        assert "共 1 个" in text

    @pytest.mark.asyncio
    async def test_sorted_by_last_used_desc(self):
        """Sessions should be sorted by last_used, newest first."""
        storage = MagicMock()
        storage.count_user_sessions = AsyncMock(return_value=1)
        storage.get_user_sessions = AsyncMock(
            return_value=[
                SimpleNamespace(
                    session_id="old-db",
                    last_used=datetime(2026, 5, 1, 0, 0, tzinfo=UTC),
                    message_count=1,
                )
            ]
        )

        cli_data = [
            {
                "session_id": "new-cli",
                "ai_title": "New",
                "created_at": datetime(2026, 5, 20, 0, 0, tzinfo=UTC),
                "message_count": 1,
                "last_used": datetime(2026, 5, 22, 0, 0, tzinfo=UTC),
            }
        ]

        with patch(
            "src.bot.features.session_browser.ClaudeIntegration.scan_cli_sessions",
            new_callable=AsyncMock,
            return_value=cli_data,
        ), patch(
            "src.bot.features.session_browser.ClaudeIntegration.read_session_title",
            new_callable=AsyncMock,
            return_value="Title",
        ):
            text, kb = await list_sessions_view(
                storage=storage,
                user_id=42,
                project_path="/proj",
                page=0,
            )

        buttons = [b for row in kb.inline_keyboard for b in row]
        # First button should be the newer CLI session
        assert "new-cli" in buttons[0].callback_data


class TestSessionDetailCli:
    @pytest.mark.asyncio
    async def test_detail_renders_cli_session(self):
        """session_detail_view should render CLI sessions found via scan."""
        storage = MagicMock()
        storage.load_session = AsyncMock(return_value=None)

        cli_data = [
            {
                "session_id": "cli-sid",
                "ai_title": "CLI Session",
                "created_at": datetime(2026, 5, 20, 10, 0, tzinfo=UTC),
                "message_count": 5,
                "last_used": datetime(2026, 5, 22, 8, 0, tzinfo=UTC),
            }
        ]

        with patch(
            "src.bot.features.session_browser.ClaudeIntegration.scan_cli_sessions",
            new_callable=AsyncMock,
            return_value=cli_data,
        ), patch(
            "src.bot.features.session_browser.ClaudeIntegration.read_session_title",
            new_callable=AsyncMock,
            return_value="CLI Session",
        ):
            result = await session_detail_view(
                storage=storage,
                user_id=42,
                session_id="cli-sid",
                back_page=0,
                project_path="/proj",
            )

        assert result is not None
        text, kb = result
        assert "CLI Session" in text
        assert "CLI session" in text  # indicator text
        buttons = [b for row in kb.inline_keyboard for b in row]
        cbs = [b.callback_data for b in buttons]
        assert "sessions:resume:cli-sid" in cbs

    @pytest.mark.asyncio
    async def test_detail_returns_none_for_missing(self):
        """session_detail_view returns None for truly missing sessions."""
        storage = MagicMock()
        storage.load_session = AsyncMock(return_value=None)

        with patch(
            "src.bot.features.session_browser.ClaudeIntegration.scan_cli_sessions",
            new_callable=AsyncMock,
            return_value=[],
        ):
            result = await session_detail_view(
                storage=storage,
                user_id=42,
                session_id="does-not-exist",
                back_page=0,
                project_path="/proj",
            )

        assert result is None


class TestOwnershipCheckCli:
    @pytest.mark.asyncio
    async def test_cli_session_returns_owned(self):
        """_check_session_ownership should return 'owned' for CLI sessions."""
        from src.bot.handlers.callback import _check_session_ownership

        storage = MagicMock()
        storage.load_session = AsyncMock(return_value=None)
        # No DB attribute → skip DB check
        storage.db = None
        storage.db_manager = None

        cli_data = [
            {
                "session_id": "cli-owned",
                "ai_title": "Title",
                "created_at": None,
                "message_count": 1,
                "last_used": datetime.now(UTC),
            }
        ]

        with patch(
            "src.bot.handlers.callback.ClaudeIntegration.scan_cli_sessions",
            new_callable=AsyncMock,
            return_value=cli_data,
        ):
            result = await _check_session_ownership(
                storage, 42, "cli-owned", "/proj"
            )

        assert result == "owned"

    @pytest.mark.asyncio
    async def test_missing_session_returns_missing(self):
        """_check_session_ownership returns 'missing' for nonexistent sessions."""
        from src.bot.handlers.callback import _check_session_ownership

        storage = MagicMock()
        storage.load_session = AsyncMock(return_value=None)
        storage.db = None
        storage.db_manager = None

        with patch(
            "src.bot.handlers.callback.ClaudeIntegration.scan_cli_sessions",
            new_callable=AsyncMock,
            return_value=[],
        ):
            result = await _check_session_ownership(
                storage, 42, "no-such-sid", "/proj"
            )

        assert result == "missing"
