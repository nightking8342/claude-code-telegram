"""Integration tests for /sessions flow.

Each test wires a real-ish dependency graph (storage + mocked Telegram
update/context) and exercises one branch of the callback dispatcher.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from src.bot.handlers.callback import (
    handle_callback_query,
    handle_sessions_callback,
)
from src.claude.session import ClaudeSession
from src.storage.database import DatabaseManager
from src.storage.session_storage import SQLiteSessionStorage


# Match the stringification storage uses (str(Path(p))) so test queries
# compare equal to stored values across Windows/POSIX.
def _norm(p: str) -> str:
    return str(Path(p))


PROJ = _norm("/proj")


@pytest_asyncio.fixture
async def storage(tmp_path):
    db = DatabaseManager(str(tmp_path / "test.db"))
    await db.initialize()
    yield SQLiteSessionStorage(db)
    await db.close()


def _fake_query(user_id: int, callback_data: str):
    query = MagicMock()
    query.from_user = MagicMock()
    query.from_user.id = user_id
    query.data = callback_data
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.message = MagicMock()
    query.message.reply_text = AsyncMock()
    query.message.reply_document = AsyncMock()
    query.message.chat_id = 12345
    return query


def _fake_context(storage, user_id, current_directory=PROJ):
    context = MagicMock()
    context.user_data = {"current_directory": current_directory}
    settings = MagicMock()
    settings.approved_directory = current_directory
    context.bot_data = {
        "storage": MagicMock(sessions=storage),
        "audit_logger": MagicMock(log_event=AsyncMock()),
        "settings": settings,
    }
    return context


async def _save(storage, user_id, project, sid, age=1):
    now = datetime.now(UTC) - timedelta(minutes=age)
    await storage.save_session(
        ClaudeSession(
            session_id=sid,
            user_id=user_id,
            project_path=Path(project),
            created_at=now,
            last_used=now,
            total_cost=0.0,
            total_turns=0,
            message_count=5,
            tools_used=[],
        )
    )


class TestListCallback:
    @pytest.mark.asyncio
    async def test_list_callback_renders_page(self, storage):
        for i in range(15):
            await _save(storage, 42, PROJ, f"s{i:02d}", age=i)
        query = _fake_query(42, "sessions:list:1")
        context = _fake_context(storage, 42)
        with patch(
            "src.bot.features.session_browser.ClaudeIntegration.read_session_title",
            new_callable=AsyncMock,
            return_value="t",
        ):
            await handle_sessions_callback(query, "list:1", context)
        query.edit_message_text.assert_called_once()
        kw = query.edit_message_text.call_args.kwargs
        # On page 2 of 2, only "prev" button
        kb = kw["reply_markup"]
        nav = kb.inline_keyboard[-1]
        assert len(nav) == 1
        assert nav[0].callback_data == "sessions:list:0"


class TestBackCallback:
    @pytest.mark.asyncio
    async def test_back_re_renders_list_at_given_page(self, storage):
        for i in range(15):
            await _save(storage, 42, PROJ, f"s{i:02d}", age=i)
        query = _fake_query(42, "sessions:back:1")
        context = _fake_context(storage, 42)
        with patch(
            "src.bot.features.session_browser.ClaudeIntegration.read_session_title",
            new_callable=AsyncMock,
            return_value="t",
        ):
            await handle_sessions_callback(query, "back:1", context)
        query.edit_message_text.assert_called_once()


class TestUnknownSubAction:
    @pytest.mark.asyncio
    async def test_unknown_subaction_shows_error(self, storage):
        query = _fake_query(42, "sessions:bogus:xx")
        context = _fake_context(storage, 42)
        await handle_sessions_callback(query, "bogus:xx", context)
        query.edit_message_text.assert_called_once()
        assert "未知" in query.edit_message_text.call_args.args[0]


class TestRouterRegistration:
    @pytest.mark.asyncio
    async def test_main_router_dispatches_sessions(self, storage):
        """handle_callback_query must route sessions:* to handle_sessions_callback."""
        query = _fake_query(42, "sessions:list:0")
        context = _fake_context(storage, 42)
        update = MagicMock()
        update.callback_query = query
        with patch(
            "src.bot.handlers.callback.handle_sessions_callback",
            new_callable=AsyncMock,
        ) as mock_handler:
            await handle_callback_query(update, context)
            mock_handler.assert_called_once()
            call_args = mock_handler.call_args
            assert call_args.args[1] == "list:0"


class TestDetailCallback:
    @pytest.mark.asyncio
    async def test_detail_renders_session_card(self, storage):
        await _save(storage, 42, PROJ, "my-session-id")
        query = _fake_query(42, "sessions:detail:my-session-id")
        context = _fake_context(storage, 42)
        with patch(
            "src.bot.features.session_browser.ClaudeIntegration.read_session_title",
            new_callable=AsyncMock,
            return_value="Hello world",
        ):
            await handle_sessions_callback(
                query, "detail:my-session-id", context
            )
        query.edit_message_text.assert_called_once()
        text = query.edit_message_text.call_args.args[0]
        assert "Hello world" in text
        assert "my-session-id" in text

    @pytest.mark.asyncio
    async def test_detail_cross_user_denied_with_audit(self, storage):
        await _save(storage, 99, PROJ, "victim-sid")
        query = _fake_query(42, "sessions:detail:victim-sid")
        context = _fake_context(storage, 42)
        await handle_sessions_callback(query, "detail:victim-sid", context)
        # Cross-user → answer_callback_query with alert, no edit
        query.answer.assert_called()
        # Audit event must have been logged
        audit_calls = context.bot_data["audit_logger"].log_event.call_args_list
        assert any(
            c.kwargs.get("event_type") == "sessions_cross_user_denied"
            for c in audit_calls
        )

    @pytest.mark.asyncio
    async def test_detail_nonexistent_session(self, storage):
        query = _fake_query(42, "sessions:detail:does-not-exist")
        context = _fake_context(storage, 42)
        await handle_sessions_callback(
            query, "detail:does-not-exist", context
        )
        query.answer.assert_called()
        # Should refresh to list
        query.edit_message_text.assert_called_once()


class TestViewHtmlCallback:
    @pytest.mark.asyncio
    async def test_view_sends_html_document(self, storage):
        await _save(storage, 42, PROJ, "view-test-sid")
        query = _fake_query(42, "sessions:view:view-test-sid")
        context = _fake_context(storage, 42)
        fake_exporter = MagicMock()
        fake_exporter.export_session = AsyncMock(
            return_value=MagicMock(
                content="<html>history</html>",
                filename="session_view-test-sid_20260524.html",
                mime_type="text/html",
                size_bytes=20,
            )
        )
        context.bot_data["session_exporter"] = fake_exporter
        with patch(
            "src.bot.handlers.callback.ClaudeIntegration.read_session_title",
            new_callable=AsyncMock,
            return_value="My Title",
        ):
            await handle_sessions_callback(query, "view:view-test-sid", context)
        query.message.reply_document.assert_called_once()
        kwargs = query.message.reply_document.call_args.kwargs
        # Filename should be aiTitle-flavored (sanitized) or contain id fragment.
        assert "My_Title" in kwargs["filename"] or "view-test" in kwargs["filename"]
        assert kwargs["filename"].endswith(".html")

    @pytest.mark.asyncio
    async def test_view_cross_user_denied(self, storage):
        await _save(storage, 99, PROJ, "victim-view")
        query = _fake_query(42, "sessions:view:victim-view")
        context = _fake_context(storage, 42)
        await handle_sessions_callback(query, "view:victim-view", context)
        query.message.reply_document.assert_not_called()
        query.answer.assert_called()
        # Audit event must have been logged
        audit_calls = context.bot_data["audit_logger"].log_event.call_args_list
        assert any(
            c.kwargs.get("event_type") == "sessions_cross_user_denied"
            for c in audit_calls
        )

    @pytest.mark.asyncio
    async def test_view_export_failure_replies_with_error(self, storage):
        await _save(storage, 42, PROJ, "fail-view")
        query = _fake_query(42, "sessions:view:fail-view")
        context = _fake_context(storage, 42)
        fake_exporter = MagicMock()
        fake_exporter.export_session = AsyncMock(side_effect=ValueError("explode"))
        context.bot_data["session_exporter"] = fake_exporter
        with patch(
            "src.bot.handlers.callback.ClaudeIntegration.read_session_title",
            new_callable=AsyncMock,
            return_value=None,
        ):
            await handle_sessions_callback(query, "view:fail-view", context)
        query.message.reply_text.assert_called_once()
        assert "失败" in query.message.reply_text.call_args.args[0]


class TestResumeCallback:
    @pytest.mark.asyncio
    async def test_resume_sets_user_data(self, storage):
        await _save(storage, 42, PROJ, "resume-test-sid")
        query = _fake_query(42, "sessions:resume:resume-test-sid")
        context = _fake_context(storage, 42)
        with patch(
            "src.bot.handlers.callback.ClaudeIntegration.read_session_title",
            new_callable=AsyncMock,
            return_value="Resume me",
        ):
            await handle_sessions_callback(query, "resume:resume-test-sid", context)
        assert context.user_data["claude_session_id"] == "resume-test-sid"
        assert context.user_data["force_new_session"] is False
        query.message.reply_text.assert_called_once()
        assert "Resume me" in query.message.reply_text.call_args.args[0]

    @pytest.mark.asyncio
    async def test_resume_cross_user_denied(self, storage):
        await _save(storage, 99, PROJ, "victim-resume")
        query = _fake_query(42, "sessions:resume:victim-resume")
        context = _fake_context(storage, 42)
        await handle_sessions_callback(query, "resume:victim-resume", context)
        assert context.user_data.get("claude_session_id") is None
        query.answer.assert_called()
        audit_calls = context.bot_data["audit_logger"].log_event.call_args_list
        assert any(
            c.kwargs.get("event_type") == "sessions_cross_user_denied"
            for c in audit_calls
        )


class TestExportCallback:
    @pytest.mark.asyncio
    async def test_export_top_shows_format_submenu(self, storage):
        await _save(storage, 42, PROJ, "export-test")
        query = _fake_query(42, "sessions:export:export-test")
        context = _fake_context(storage, 42)
        await handle_sessions_callback(query, "export:export-test", context)
        query.edit_message_text.assert_called_once()
        kb = query.edit_message_text.call_args.kwargs["reply_markup"]
        cbs = {b.callback_data for row in kb.inline_keyboard for b in row}
        assert "sessions:export:export-test:md" in cbs
        assert "sessions:export:export-test:json" in cbs
        # Cancel goes back to detail
        assert "sessions:detail:export-test" in cbs

    @pytest.mark.asyncio
    async def test_export_md_sends_markdown(self, storage):
        await _save(storage, 42, PROJ, "exp-md")
        query = _fake_query(42, "sessions:export:exp-md:md")
        context = _fake_context(storage, 42)
        fake_exporter = MagicMock()
        fake_exporter.export_session = AsyncMock(
            return_value=MagicMock(
                content="# session md",
                filename="session_exp-md_20260524.md",
                mime_type="text/markdown",
                size_bytes=12,
            )
        )
        context.bot_data["session_exporter"] = fake_exporter
        with patch(
            "src.bot.handlers.callback.ClaudeIntegration.read_session_title",
            new_callable=AsyncMock,
            return_value="MD title",
        ):
            await handle_sessions_callback(query, "export:exp-md:md", context)
        query.message.reply_document.assert_called_once()
        kwargs = query.message.reply_document.call_args.kwargs
        assert kwargs["filename"].endswith(".md")

    @pytest.mark.asyncio
    async def test_export_json_sends_json(self, storage):
        await _save(storage, 42, PROJ, "exp-json")
        query = _fake_query(42, "sessions:export:exp-json:json")
        context = _fake_context(storage, 42)
        fake_exporter = MagicMock()
        fake_exporter.export_session = AsyncMock(
            return_value=MagicMock(
                content='{"x":1}',
                filename="x.json",
                mime_type="application/json",
                size_bytes=7,
            )
        )
        context.bot_data["session_exporter"] = fake_exporter
        with patch(
            "src.bot.handlers.callback.ClaudeIntegration.read_session_title",
            new_callable=AsyncMock,
            return_value=None,
        ):
            await handle_sessions_callback(query, "export:exp-json:json", context)
        query.message.reply_document.assert_called_once()
        kwargs = query.message.reply_document.call_args.kwargs
        assert kwargs["filename"].endswith(".json")

    @pytest.mark.asyncio
    async def test_export_unknown_format_falls_back(self, storage):
        await _save(storage, 42, PROJ, "exp-x")
        query = _fake_query(42, "sessions:export:exp-x:weird")
        context = _fake_context(storage, 42)
        await handle_sessions_callback(query, "export:exp-x:weird", context)
        query.message.reply_document.assert_not_called()
        query.answer.assert_called()


class TestSessionsCommand:
    @pytest.mark.asyncio
    async def test_lists_only_current_directory(self, storage):
        proj_a = _norm("/proj-a")
        proj_b = _norm("/proj-b")
        # User 42 has sessions in two directories
        await _save(storage, 42, proj_a, "a1")
        await _save(storage, 42, proj_b, "b1")
        # Other user with same directory must NOT appear
        await _save(storage, 99, proj_a, "other1")

        from src.bot.handlers.command import sessions_command

        update = MagicMock()
        update.effective_user.id = 42
        update.message.reply_text = AsyncMock()
        context = _fake_context(storage, 42, current_directory=proj_a)

        with patch(
            "src.bot.features.session_browser.ClaudeIntegration.read_session_title",
            new_callable=AsyncMock,
            return_value="t",
        ):
            await sessions_command(update, context)

        update.message.reply_text.assert_called_once()
        kb = update.message.reply_text.call_args.kwargs["reply_markup"]
        cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
        assert any("a1" in c for c in cbs)
        # b1 (other dir) and other1 (other user) must not appear
        assert not any("b1" in c for c in cbs)
        assert not any("other1" in c for c in cbs)


class TestRegistration:
    @pytest.mark.asyncio
    async def test_sessions_in_bot_commands_agentic(self):
        from src.bot.orchestrator import MessageOrchestrator

        settings = MagicMock()
        settings.agentic_mode = True
        settings.enable_project_threads = False
        orch = MessageOrchestrator(settings, deps={})
        commands = await orch.get_bot_commands()
        names = {c.command for c in commands}
        assert "sessions" in names

    @pytest.mark.asyncio
    async def test_sessions_in_bot_commands_classic(self):
        from src.bot.orchestrator import MessageOrchestrator

        settings = MagicMock()
        settings.agentic_mode = False
        settings.enable_project_threads = False
        orch = MessageOrchestrator(settings, deps={})
        commands = await orch.get_bot_commands()
        names = {c.command for c in commands}
        assert "sessions" in names
