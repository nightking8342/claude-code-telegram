"""Pagination & count tests for SQLiteSessionStorage."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio

from src.claude.session import ClaudeSession
from src.storage.database import DatabaseManager
from src.storage.session_storage import SQLiteSessionStorage


@pytest_asyncio.fixture
async def storage(tmp_path):
    db = DatabaseManager(str(tmp_path / "test.db"))
    await db.initialize()
    yield SQLiteSessionStorage(db)
    await db.close()


def _norm(p: str) -> str:
    """Match the stringification storage uses (str(Path(p))), so test queries
    compare equal to stored values across Windows/POSIX."""
    return str(Path(p))


def _make_session(user_id: int, project_path: str, session_id: str, age_min: int):
    now = datetime.now(UTC) - timedelta(minutes=age_min)
    return ClaudeSession(
        session_id=session_id,
        user_id=user_id,
        project_path=Path(project_path),
        created_at=now,
        last_used=now,
        total_cost=0.0,
        total_turns=0,
        message_count=0,
        tools_used=[],
    )


class TestPagination:
    @pytest.mark.asyncio
    async def test_limit_offset_returns_slice(self, storage):
        for i in range(25):
            await storage.save_session(
                _make_session(1, "/proj", f"sess{i:02d}", age_min=i)
            )
        page1 = await storage.get_user_sessions(
            1, project_path=_norm("/proj"), limit=10, offset=0
        )
        page2 = await storage.get_user_sessions(
            1, project_path=_norm("/proj"), limit=10, offset=10
        )
        page3 = await storage.get_user_sessions(
            1, project_path=_norm("/proj"), limit=10, offset=20
        )
        assert len(page1) == 10
        assert len(page2) == 10
        assert len(page3) == 5
        # Sorted by last_used DESC: sess00 (youngest) is first on page1
        assert page1[0].session_id == "sess00"
        assert page3[-1].session_id == "sess24"

    @pytest.mark.asyncio
    async def test_project_path_filter(self, storage):
        await storage.save_session(_make_session(1, "/proj-a", "a1", 1))
        await storage.save_session(_make_session(1, "/proj-b", "b1", 2))
        result = await storage.get_user_sessions(1, project_path=_norm("/proj-a"))
        assert len(result) == 1
        assert result[0].session_id == "a1"

    @pytest.mark.asyncio
    async def test_backward_compat_no_filters(self, storage):
        await storage.save_session(_make_session(1, "/proj-a", "a1", 1))
        await storage.save_session(_make_session(1, "/proj-b", "b1", 2))
        result = await storage.get_user_sessions(1)
        assert len(result) == 2


class TestCount:
    @pytest.mark.asyncio
    async def test_count_returns_total(self, storage):
        for i in range(7):
            await storage.save_session(_make_session(1, "/proj", f"s{i}", age_min=i))
        assert await storage.count_user_sessions(1, project_path=_norm("/proj")) == 7

    @pytest.mark.asyncio
    async def test_count_excludes_inactive(self, storage):
        await storage.save_session(_make_session(1, "/proj", "active1", 1))
        await storage.save_session(_make_session(1, "/proj", "kill1", 2))
        await storage.delete_session("kill1")
        assert await storage.count_user_sessions(1, project_path=_norm("/proj")) == 1

    @pytest.mark.asyncio
    async def test_count_filters_by_project_path(self, storage):
        await storage.save_session(_make_session(1, "/proj-a", "a1", 1))
        await storage.save_session(_make_session(1, "/proj-b", "b1", 2))
        assert await storage.count_user_sessions(1, project_path=_norm("/proj-a")) == 1
        assert await storage.count_user_sessions(1, project_path=_norm("/proj-b")) == 1
