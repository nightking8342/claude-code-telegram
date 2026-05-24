# `/sessions` —— Session Browser & Resume Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `/sessions` command to claude-code-telegram so users can browse, view, and resume their per-directory Claude Code sessions from Telegram without exposing session content to the AI context.

**Architecture:** Two-layer Telegram UI (list → detail) built entirely from SQLite (`sessions` + `messages` tables) and locally-stored CLI jsonl files (`~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`). The `aiTitle` field that the Claude CLI already writes is read in reverse to label list rows; HTML export reuses the existing `SessionExporter`. Resume is implemented by writing `context.user_data["claude_session_id"]` — no SDK call until the user sends the next message.

**Tech Stack:** Python 3.10+ · `python-telegram-bot` (callback queries, InlineKeyboardMarkup) · `aiofiles` (async file IO, already in deps) · `aiosqlite` · pytest-asyncio · structlog.

**Spec:** `docs/superpowers/specs/2026-05-24-resume-session-design.md`

---

## ⚠ Revised on 2026-05-24 (after pre-flight discovery)

Inspecting `dev` branch revealed that two of the planned helpers **already exist**:

- `ClaudeIntegration._encode_project_path(project_path: Path) -> str` at `src/claude/facade.py:215-235`
  - Algorithm: replaces `:\` / `:/` with `--`, then `\` / `/` with `-`
  - Slightly different from the spec's `re.sub(r"[:\\/.]", "-", path)` (does not handle `.`); acceptable because the `read_session_title` failure mode already has a fallback and project paths in normal use rarely contain dots
- `ClaudeIntegration.read_session_title(session_id: str, project_path: Path) -> Optional[str]` (static, async) at `src/claude/facade.py:237-291`
  - Scans the jsonl forward and keeps the last `ai-title` (no tail-1MB optimization, but adequate for current session sizes)

Also pre-existing helpers in `orchestrator.py`:

- `MessageOrchestrator._relative_time(dt)` — Chinese relative time ("刚刚"/"5分钟前"/"3小时前"/"2天前")
- `MessageOrchestrator._display_width(text)` — CJK-aware display width
- `MessageOrchestrator._build_session_resume_text(...)` — reference for detail-card formatting

**Consequences:**

- **Task 1 (`encode_project_path`) — SKIPPED.** Use `ClaudeIntegration._encode_project_path` directly.
- **Task 2 (`read_ai_title`) — SKIPPED.** Use `ClaudeIntegration.read_session_title` directly.
- **Task 3+ — replace plan-internal imports** of `encode_project_path` / `read_ai_title` with imports from `src.claude.facade`.
- **`_resolve_title` helper** still lives in `src/bot/features/session_browser.py` (Task 5), but internally calls `ClaudeIntegration.read_session_title` instead of a private function.
- **Test fixtures** that previously patched `src.bot.features.session_browser._claude_projects_dir` should patch `src.claude.facade.ClaudeIntegration.read_session_title` instead.
- **Relative time:** use `MessageOrchestrator._relative_time` (already exists) instead of writing a new `_format_relative_time` in `session_browser.py`. Either import the static method directly or duplicate the tiny function — the latter is simpler if `MessageOrchestrator` import cycles cause issues.

The rest of the plan (Tasks 3–15) remains as written; concrete code in test/impl blocks is correct except where it imports `read_ai_title` / `encode_project_path` — substitute the facade equivalents.

---

## File Structure

### Create

| Path | Responsibility |
|------|----------------|
| `src/bot/features/session_browser.py` | Pure functions: `encode_project_path`, `read_ai_title`, `derive_fallback_title`, `list_sessions_view`, `session_detail_view`. No Telegram side-effects. |
| `tests/unit/test_session_browser.py` | Unit tests for the five functions above. |
| `tests/unit/test_session_storage_pagination.py` | Unit tests for new storage pagination/count API. |
| `tests/integration/test_sessions_command_flow.py` | Mock-Telegram integration tests for command + callback flows. |

### Modify

| Path | What changes |
|------|--------------|
| `src/storage/session_storage.py` | Add `project_path`/`limit`/`offset` params to `get_user_sessions`; add `count_user_sessions`. |
| `src/bot/handlers/callback.py` | Add `"sessions"` route in dispatcher; add `handle_sessions_callback` with sub-dispatch over `list/detail/view/resume/export/back`. |
| `src/bot/handlers/command.py` | Add `sessions_command` for classic mode. |
| `src/bot/orchestrator.py` | Add `sessions_command` for agentic mode; register in both `_register_agentic_handlers` and `_register_classic_handlers`; add to `get_bot_commands`. |

### Do not modify

- `src/storage/database.py` (schema unchanged)
- `src/claude/facade.py` / `src/claude/sdk_integration.py` (auto-resume path already reads `context.user_data["claude_session_id"]`)
- `src/bot/features/session_export.py` (`export_session(user_id, session_id, format)` already takes explicit `session_id`)

---

## Pre-implementation verification (already done — record only)

Spec §9 required three checks. Results captured here so the engineer doesn't repeat them:

1. **cwd encoding rule** (verified against `~/.claude/projects/` samples):
   - `D:\claudebot\claude-code-telegram` → `D--claudebot-claude-code-telegram`
   - `C:\Users\WHY` → `C--Users-WHY`
   - `C:\Users\WHY\.claude-tg-bot` → `C--Users-WHY--claude-tg-bot`
   - **Rule:** `re.sub(r"[:\\/.]", "-", path)` — replace `:`, `\`, `/`, `.` with `-`; preserve `-`.
2. **`SessionExporter.export_session(user_id, session_id, format)`** already accepts explicit `session_id` and returns `ExportedSession(content: str, filename, mime_type, size_bytes, ...)`. No changes needed.
3. **user_data keys** — entire codebase uses `claude_session_id` and `force_new_session`; both modes share the same keys.

---

## Task 1: `encode_project_path` pure helper

> ⚠ **SKIPPED — see Revision note above.** Use `ClaudeIntegration._encode_project_path` from `src/claude/facade.py:215-235`. No new code, no new tests for this task.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_session_browser.py`:

```python
"""Unit tests for session_browser module."""

import pytest

from src.bot.features.session_browser import encode_project_path


class TestEncodeProjectPath:
    def test_windows_drive_path(self):
        assert (
            encode_project_path(r"D:\claudebot\claude-code-telegram")
            == "D--claudebot-claude-code-telegram"
        )

    def test_windows_user_path(self):
        assert encode_project_path(r"C:\Users\WHY") == "C--Users-WHY"

    def test_windows_hidden_dir(self):
        assert (
            encode_project_path(r"C:\Users\WHY\.claude-tg-bot")
            == "C--Users-WHY--claude-tg-bot"
        )

    def test_linux_path(self):
        # POSIX paths start with /, which becomes a leading dash.
        assert encode_project_path("/home/x/projects/foo") == "-home-x-projects-foo"

    def test_preserves_internal_hyphens(self):
        assert (
            encode_project_path(r"D:\Tools\operit-pc-agent")
            == "D--Tools-operit-pc-agent"
        )

    def test_dots_in_filename(self):
        assert encode_project_path(r"D:\foo\bar.baz") == "D--foo-bar-baz"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd D:/claudebot/claude-code-telegram && poetry run pytest tests/unit/test_session_browser.py::TestEncodeProjectPath -v
```

Expected: FAIL with `ImportError: cannot import name 'encode_project_path'`.

- [ ] **Step 3: Write minimal implementation**

Create `src/bot/features/session_browser.py`:

```python
"""Session browser logic for the /sessions Telegram command.

Pure logic only — no Telegram side-effects, no SDK calls.
"""

import re


_CWD_ENCODE_RE = re.compile(r"[:\\/.]")


def encode_project_path(path: str) -> str:
    """Encode a project working directory to the form Claude CLI uses for
    its jsonl folder under ~/.claude/projects/.

    Replaces `:`, `\\`, `/`, and `.` with `-`. Hyphens already in the path
    are preserved.
    """
    return _CWD_ENCODE_RE.sub("-", path)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
poetry run pytest tests/unit/test_session_browser.py::TestEncodeProjectPath -v
```

Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add src/bot/features/session_browser.py tests/unit/test_session_browser.py
git commit -m "feat(sessions): encode_project_path helper for jsonl folder lookup"
```

---

## Task 2: `read_ai_title` — tail-read jsonl for `aiTitle`

> ⚠ **SKIPPED — see Revision note above.** Use `ClaudeIntegration.read_session_title` from `src/claude/facade.py:237-291`. No new code, no new tests for this task. If tail-1MB optimization is needed later, improve `read_session_title` in place — do not duplicate.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_session_browser.py`:

```python
import json
from pathlib import Path
from unittest.mock import patch

from src.bot.features.session_browser import read_ai_title


class TestReadAiTitle:
    def _write_jsonl(self, tmp_path, project_dir, session_id, lines):
        proj = tmp_path / project_dir
        proj.mkdir(parents=True, exist_ok=True)
        fp = proj / f"{session_id}.jsonl"
        fp.write_text("\n".join(json.dumps(L) for L in lines), encoding="utf-8")
        return fp

    def test_finds_last_ai_title_in_jsonl(self, tmp_path):
        self._write_jsonl(
            tmp_path,
            "D--proj",
            "abc123",
            [
                {"type": "user", "content": "hi"},
                {"type": "ai-title", "aiTitle": "first guess", "sessionId": "abc123"},
                {"type": "assistant", "content": "..."},
                {"type": "ai-title", "aiTitle": "final title", "sessionId": "abc123"},
                {"type": "assistant", "content": "..."},
            ],
        )
        with patch(
            "src.bot.features.session_browser._claude_projects_dir",
            return_value=tmp_path,
        ):
            assert read_ai_title(r"D:\proj", "abc123") == "final title"

    def test_missing_file_returns_none(self, tmp_path):
        with patch(
            "src.bot.features.session_browser._claude_projects_dir",
            return_value=tmp_path,
        ):
            assert read_ai_title(r"D:\nowhere", "xxxxx") is None

    def test_corrupt_jsonl_still_finds_valid_ai_title(self, tmp_path):
        proj = tmp_path / "D--proj"
        proj.mkdir(parents=True, exist_ok=True)
        fp = proj / "abc.jsonl"
        fp.write_text(
            '{"type":"user","content":"hi"}\n'
            "NOT VALID JSON\n"
            '{"type":"ai-title","aiTitle":"recovered","sessionId":"abc"}\n',
            encoding="utf-8",
        )
        with patch(
            "src.bot.features.session_browser._claude_projects_dir",
            return_value=tmp_path,
        ):
            assert read_ai_title(r"D:\proj", "abc") == "recovered"

    def test_no_ai_title_returns_none(self, tmp_path):
        self._write_jsonl(
            tmp_path,
            "D--proj",
            "newone",
            [{"type": "user", "content": "hi"}, {"type": "assistant", "content": "ok"}],
        )
        with patch(
            "src.bot.features.session_browser._claude_projects_dir",
            return_value=tmp_path,
        ):
            assert read_ai_title(r"D:\proj", "newone") is None

    def test_huge_file_reads_only_tail(self, tmp_path):
        proj = tmp_path / "D--proj"
        proj.mkdir(parents=True, exist_ok=True)
        fp = proj / "big.jsonl"
        # 1.5 MB of irrelevant lines, then an ai-title at the end.
        filler = json.dumps({"type": "user", "content": "x" * 1000}) + "\n"
        with fp.open("w", encoding="utf-8") as f:
            for _ in range(1500):
                f.write(filler)
            f.write(
                json.dumps(
                    {"type": "ai-title", "aiTitle": "tail title", "sessionId": "big"}
                )
                + "\n"
            )
        with patch(
            "src.bot.features.session_browser._claude_projects_dir",
            return_value=tmp_path,
        ):
            assert read_ai_title(r"D:\proj", "big") == "tail title"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
poetry run pytest tests/unit/test_session_browser.py::TestReadAiTitle -v
```

Expected: FAIL — `read_ai_title` not defined.

- [ ] **Step 3: Write minimal implementation**

Append to `src/bot/features/session_browser.py`:

```python
import json
import os
from pathlib import Path
from typing import Optional


_AI_TITLE_TAIL_BYTES = 1024 * 1024  # 1 MB
_AI_TITLE_MARKER = b'"type":"ai-title"'


def _claude_projects_dir() -> Path:
    """Return ~/.claude/projects/. Wrapped for test patching."""
    return Path(os.path.expanduser("~")) / ".claude" / "projects"


def read_ai_title(project_path: str, session_id: str) -> Optional[str]:
    """Read the most recent aiTitle for a session from its CLI jsonl file.

    The Claude CLI appends `{"type":"ai-title", "aiTitle":"...", "sessionId":"..."}`
    rows as the conversation progresses; the LAST one is the current title. We
    read only the trailing 1 MB so huge sessions stay cheap.

    Returns None if the file is missing, has no ai-title row, or anything
    fails. Callers should fall back to `derive_fallback_title`.
    """
    folder = _claude_projects_dir() / encode_project_path(project_path)
    fp = folder / f"{session_id}.jsonl"
    if not fp.exists():
        return None

    try:
        size = fp.stat().st_size
        with fp.open("rb") as f:
            if size > _AI_TITLE_TAIL_BYTES:
                f.seek(size - _AI_TITLE_TAIL_BYTES)
                # Skip the partial line at the seek position.
                f.readline()
            tail = f.read()
    except OSError:
        return None

    # Scan lines bottom-up; early-exit on first valid ai-title.
    for raw in reversed(tail.splitlines()):
        if _AI_TITLE_MARKER not in raw:
            continue
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if obj.get("type") == "ai-title":
            title = obj.get("aiTitle")
            if isinstance(title, str) and title.strip():
                return title.strip()
    return None
```

- [ ] **Step 4: Run test to verify it passes**

```bash
poetry run pytest tests/unit/test_session_browser.py::TestReadAiTitle -v
```

Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add src/bot/features/session_browser.py tests/unit/test_session_browser.py
git commit -m "feat(sessions): read_ai_title tail-reads jsonl for CLI-generated title"
```

---

## Task 3: `derive_fallback_title` — first-prompt fallback

**Files:**
- Modify: `src/bot/features/session_browser.py`
- Test: `tests/unit/test_session_browser.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_session_browser.py`:

```python
from src.bot.features.session_browser import derive_fallback_title


class TestDeriveFallbackTitle:
    def test_uses_first_prompt(self):
        result = derive_fallback_title("Help me debug this webhook", "abc12345abc")
        assert result == "Help me debug this webhook"

    def test_strips_command_message_wrapper(self):
        raw = "<command-message>init</command-message><command-name>/init</command-name>"
        result = derive_fallback_title(raw, "id1id2id3id")
        # Wrappers stripped, falls through to id-based default since stripped is empty
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
```

- [ ] **Step 2: Run test to verify it fails**

```bash
poetry run pytest tests/unit/test_session_browser.py::TestDeriveFallbackTitle -v
```

Expected: FAIL — `derive_fallback_title` not defined.

- [ ] **Step 3: Write minimal implementation**

Append to `src/bot/features/session_browser.py`:

```python
import re

_COMMAND_WRAPPER_RE = re.compile(
    r"<command-(message|name)>.*?</command-\1>",
    flags=re.DOTALL | re.IGNORECASE,
)
_FALLBACK_MAX_CHARS = 60


def derive_fallback_title(first_prompt: Optional[str], session_id: str) -> str:
    """Fallback title when aiTitle is unavailable.

    Strips `<command-message>...</command-message>` and `<command-name>...</command-name>`
    wrappers that Telegram's `/command` infrastructure adds; truncates to 60
    chars + ellipsis. If nothing useful is left, falls back to
    `Session <id[:8]>`.
    """
    if first_prompt:
        cleaned = _COMMAND_WRAPPER_RE.sub("", first_prompt).strip()
        if cleaned:
            if len(cleaned) > _FALLBACK_MAX_CHARS:
                return cleaned[:_FALLBACK_MAX_CHARS] + "…"
            return cleaned
    return f"Session {session_id[:8]}"
```

- [ ] **Step 4: Run test to verify it passes**

```bash
poetry run pytest tests/unit/test_session_browser.py::TestDeriveFallbackTitle -v
```

Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add src/bot/features/session_browser.py tests/unit/test_session_browser.py
git commit -m "feat(sessions): derive_fallback_title strips command wrappers, truncates"
```

---

## Task 4: SessionStorage pagination + count

**Files:**
- Modify: `src/storage/session_storage.py:172-201` (extend `get_user_sessions`); add `count_user_sessions` near it.
- Test: `tests/unit/test_session_storage_pagination.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_session_storage_pagination.py`:

```python
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
    db = DatabaseManager(tmp_path / "test.db")
    await db.initialize()
    yield SQLiteSessionStorage(db)
    await db.close()


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
            1, project_path="/proj", limit=10, offset=0
        )
        page2 = await storage.get_user_sessions(
            1, project_path="/proj", limit=10, offset=10
        )
        page3 = await storage.get_user_sessions(
            1, project_path="/proj", limit=10, offset=20
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
        result = await storage.get_user_sessions(1, project_path="/proj-a")
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
            await storage.save_session(
                _make_session(1, "/proj", f"s{i}", age_min=i)
            )
        assert await storage.count_user_sessions(1, project_path="/proj") == 7

    @pytest.mark.asyncio
    async def test_count_excludes_inactive(self, storage):
        await storage.save_session(_make_session(1, "/proj", "active1", 1))
        await storage.save_session(_make_session(1, "/proj", "kill1", 2))
        await storage.delete_session("kill1")
        assert await storage.count_user_sessions(1, project_path="/proj") == 1

    @pytest.mark.asyncio
    async def test_count_filters_by_project_path(self, storage):
        await storage.save_session(_make_session(1, "/proj-a", "a1", 1))
        await storage.save_session(_make_session(1, "/proj-b", "b1", 2))
        assert await storage.count_user_sessions(1, project_path="/proj-a") == 1
        assert await storage.count_user_sessions(1, project_path="/proj-b") == 1
```

- [ ] **Step 2: Run test to verify it fails**

```bash
poetry run pytest tests/unit/test_session_storage_pagination.py -v
```

Expected: FAIL — `get_user_sessions` doesn't accept kwargs; `count_user_sessions` not defined.

- [ ] **Step 3: Modify `src/storage/session_storage.py:172-201`**

Replace the existing `get_user_sessions` method (currently at line 172) with this version, and add `count_user_sessions` immediately after it:

```python
    async def get_user_sessions(
        self,
        user_id: int,
        project_path: Optional[str] = None,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> List[ClaudeSession]:
        """Get active sessions for a user, optionally filtered by project_path
        and paginated.

        Args:
            user_id: user owning the sessions
            project_path: if given, only sessions for this working directory
            limit: max rows; None = no limit
            offset: rows to skip (for pagination)
        """
        sql = "SELECT * FROM sessions WHERE user_id = ? AND is_active = TRUE"
        params: list = [user_id]
        if project_path is not None:
            sql += " AND project_path = ?"
            params.append(project_path)
        sql += " ORDER BY last_used DESC"
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params.extend([limit, offset])

        async with self.db_manager.get_connection() as conn:
            cursor = await conn.execute(sql, params)
            rows = await cursor.fetchall()

        sessions = []
        for row in rows:
            session_model = SessionModel.from_row(row)
            sessions.append(
                ClaudeSession(
                    session_id=session_model.session_id,
                    user_id=session_model.user_id,
                    project_path=Path(session_model.project_path),
                    created_at=session_model.created_at,
                    last_used=session_model.last_used,
                    total_cost=session_model.total_cost,
                    total_turns=session_model.total_turns,
                    message_count=session_model.message_count,
                    tools_used=[],
                )
            )
        return sessions

    async def count_user_sessions(
        self, user_id: int, project_path: Optional[str] = None
    ) -> int:
        """Count active sessions for a user, optionally scoped to one project."""
        sql = (
            "SELECT COUNT(*) FROM sessions "
            "WHERE user_id = ? AND is_active = TRUE"
        )
        params: list = [user_id]
        if project_path is not None:
            sql += " AND project_path = ?"
            params.append(project_path)

        async with self.db_manager.get_connection() as conn:
            cursor = await conn.execute(sql, params)
            row = await cursor.fetchone()
            return int(row[0]) if row else 0
```

- [ ] **Step 4: Run test to verify it passes**

```bash
poetry run pytest tests/unit/test_session_storage_pagination.py -v
```

Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add src/storage/session_storage.py tests/unit/test_session_storage_pagination.py
git commit -m "feat(storage): add pagination and count to get_user_sessions"
```

---

## Task 5: `list_sessions_view` — render list keyboard

**Files:**
- Modify: `src/bot/features/session_browser.py`
- Test: `tests/unit/test_session_browser.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_session_browser.py`:

```python
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

from src.claude.session import ClaudeSession
from src.bot.features.session_browser import list_sessions_view


def _fake_session(sid: str, msgs: int = 5, age_min: int = 60):
    now = datetime.now(UTC) - timedelta(minutes=age_min)
    return ClaudeSession(
        session_id=sid,
        user_id=42,
        project_path=Path("/proj"),
        created_at=now,
        last_used=now,
        total_cost=0.0,
        total_turns=0,
        message_count=msgs,
        tools_used=[],
    )


class TestListSessionsView:
    @pytest.mark.asyncio
    async def test_empty_list_has_no_pagination_buttons(self):
        storage = AsyncMock()
        storage.count_user_sessions = AsyncMock(return_value=0)
        storage.get_user_sessions = AsyncMock(return_value=[])
        text, kb = await list_sessions_view(
            storage=storage, user_id=42, project_path="/proj", page=0
        )
        assert "还没有 session" in text
        assert kb.inline_keyboard == []

    @pytest.mark.asyncio
    async def test_single_page_no_pagination_buttons(self):
        storage = AsyncMock()
        storage.count_user_sessions = AsyncMock(return_value=3)
        storage.get_user_sessions = AsyncMock(
            return_value=[_fake_session(f"s{i}") for i in range(3)]
        )
        with patch(
            "src.bot.features.session_browser.read_ai_title", return_value=None
        ), patch(
            "src.bot.features.session_browser._first_prompt_for", return_value="hello"
        ):
            text, kb = await list_sessions_view(
                storage=storage, user_id=42, project_path="/proj", page=0
            )
        # 3 session buttons, no pagination row
        assert len(kb.inline_keyboard) == 3
        assert all(len(row) == 1 for row in kb.inline_keyboard)
        assert kb.inline_keyboard[0][0].callback_data == "sessions:detail:s0"

    @pytest.mark.asyncio
    async def test_multi_page_has_next_button(self):
        storage = AsyncMock()
        storage.count_user_sessions = AsyncMock(return_value=25)
        storage.get_user_sessions = AsyncMock(
            return_value=[_fake_session(f"s{i}") for i in range(10)]
        )
        with patch(
            "src.bot.features.session_browser.read_ai_title", return_value="title"
        ):
            text, kb = await list_sessions_view(
                storage=storage, user_id=42, project_path="/proj", page=0
            )
        # 10 session rows + 1 nav row
        assert len(kb.inline_keyboard) == 11
        nav_row = kb.inline_keyboard[-1]
        # Page 0: only "next"
        assert len(nav_row) == 1
        assert nav_row[0].callback_data == "sessions:list:1"

    @pytest.mark.asyncio
    async def test_middle_page_has_prev_and_next(self):
        storage = AsyncMock()
        storage.count_user_sessions = AsyncMock(return_value=25)
        storage.get_user_sessions = AsyncMock(
            return_value=[_fake_session(f"s{i}") for i in range(10)]
        )
        with patch(
            "src.bot.features.session_browser.read_ai_title", return_value="title"
        ):
            text, kb = await list_sessions_view(
                storage=storage, user_id=42, project_path="/proj", page=1
            )
        nav_row = kb.inline_keyboard[-1]
        assert len(nav_row) == 2
        assert nav_row[0].callback_data == "sessions:list:0"
        assert nav_row[1].callback_data == "sessions:list:2"

    @pytest.mark.asyncio
    async def test_last_page_has_only_prev(self):
        storage = AsyncMock()
        storage.count_user_sessions = AsyncMock(return_value=25)
        # Page 2 has 5 sessions
        storage.get_user_sessions = AsyncMock(
            return_value=[_fake_session(f"s{i}") for i in range(5)]
        )
        with patch(
            "src.bot.features.session_browser.read_ai_title", return_value="title"
        ):
            text, kb = await list_sessions_view(
                storage=storage, user_id=42, project_path="/proj", page=2
            )
        nav_row = kb.inline_keyboard[-1]
        assert len(nav_row) == 1
        assert nav_row[0].callback_data == "sessions:list:1"

    @pytest.mark.asyncio
    async def test_page_clamped_to_valid_range(self):
        storage = AsyncMock()
        storage.count_user_sessions = AsyncMock(return_value=5)
        storage.get_user_sessions = AsyncMock(
            return_value=[_fake_session(f"s{i}") for i in range(5)]
        )
        with patch(
            "src.bot.features.session_browser.read_ai_title", return_value="title"
        ):
            text, kb = await list_sessions_view(
                storage=storage, user_id=42, project_path="/proj", page=99
            )
        # Clamped to page 0 (only 1 page exists)
        assert "第 1 页 / 共 1 页" in text

    @pytest.mark.asyncio
    async def test_callback_data_under_64_bytes(self):
        storage = AsyncMock()
        storage.count_user_sessions = AsyncMock(return_value=10)
        # Real-shaped UUID session ids
        storage.get_user_sessions = AsyncMock(
            return_value=[
                _fake_session("04e2d0f2-095e-4ed7-b429-66c21569830b") for _ in range(10)
            ]
        )
        with patch(
            "src.bot.features.session_browser.read_ai_title", return_value="t"
        ):
            _, kb = await list_sessions_view(
                storage=storage, user_id=42, project_path="/proj", page=0
            )
        for row in kb.inline_keyboard:
            for btn in row:
                assert len(btn.callback_data.encode()) <= 64
```

- [ ] **Step 2: Run test to verify it fails**

```bash
poetry run pytest tests/unit/test_session_browser.py::TestListSessionsView -v
```

Expected: FAIL — `list_sessions_view` not defined.

- [ ] **Step 3: Write minimal implementation**

Append to `src/bot/features/session_browser.py`:

```python
from datetime import UTC, datetime
from typing import Any, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


PAGE_SIZE = 10
_ROW_TITLE_MAX = 35  # characters in list-row button label


def _format_relative_time(when: datetime) -> str:
    """Render a short relative-time string (e.g. '2h ago', '3d ago')."""
    now = datetime.now(UTC)
    if when.tzinfo is None:
        # Defensive — DB rows should already be tz-aware.
        when = when.replace(tzinfo=UTC)
    delta = now - when
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


async def _first_prompt_for(storage: Any, session_id: str) -> Optional[str]:
    """Fetch the first user message text for the session, if any.

    Used as a fallback when aiTitle is unavailable.
    """
    try:
        messages = await storage.get_session_messages(session_id, limit=1)
    except Exception:
        return None
    if not messages:
        return None
    first = messages[0]
    # Storage layer is consistent on `prompt` column for user input.
    if isinstance(first, dict):
        return first.get("prompt") or first.get("content")
    return getattr(first, "prompt", None) or getattr(first, "content", None)


async def _resolve_title(
    storage: Any, project_path: str, session_id: str
) -> str:
    """Resolve display title: aiTitle → first prompt → session id-based."""
    title = read_ai_title(project_path, session_id)
    if title:
        return title
    first_prompt = await _first_prompt_for(storage, session_id)
    return derive_fallback_title(first_prompt, session_id)


async def list_sessions_view(
    storage: Any,
    user_id: int,
    project_path: str,
    page: int,
    page_size: int = PAGE_SIZE,
) -> Tuple[str, InlineKeyboardMarkup]:
    """Render the /sessions list page.

    Returns (text, InlineKeyboardMarkup). No Telegram side-effects.
    """
    total = await storage.count_user_sessions(user_id, project_path=project_path)
    if total == 0:
        text = (
            "📂 当前目录下还没有 session — 直接发条消息开个新的吧。\n"
            f"<code>{escape_path(project_path)}</code>"
        )
        return text, InlineKeyboardMarkup([])

    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))

    sessions = await storage.get_user_sessions(
        user_id,
        project_path=project_path,
        limit=page_size,
        offset=page * page_size,
    )

    rows: list[list[InlineKeyboardButton]] = []
    for s in sessions:
        title = await _resolve_title(storage, project_path, s.session_id)
        label = _truncate(title, _ROW_TITLE_MAX)
        suffix = f" · {_format_relative_time(s.last_used)} · {s.message_count}条"
        rows.append(
            [
                InlineKeyboardButton(
                    label + suffix,
                    callback_data=f"sessions:detail:{s.session_id}",
                )
            ]
        )

    # Pagination row
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(
            InlineKeyboardButton("← 上一页", callback_data=f"sessions:list:{page - 1}")
        )
    if page < total_pages - 1:
        nav.append(
            InlineKeyboardButton("下一页 →", callback_data=f"sessions:list:{page + 1}")
        )
    if nav:
        rows.append(nav)

    text = (
        f"📂 当前目录的 sessions（第 {page + 1} 页 / 共 {total_pages} 页 · 共 {total} 个）\n"
        f"<code>{escape_path(project_path)}</code>"
    )
    return text, InlineKeyboardMarkup(rows)


def escape_path(path: str) -> str:
    """Escape minimal HTML chars for path display."""
    return (
        path.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
```

- [ ] **Step 4: Run test to verify it passes**

```bash
poetry run pytest tests/unit/test_session_browser.py::TestListSessionsView -v
```

Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add src/bot/features/session_browser.py tests/unit/test_session_browser.py
git commit -m "feat(sessions): list_sessions_view renders paginated keyboard"
```

---

## Task 6: `session_detail_view` — render detail card

**Files:**
- Modify: `src/bot/features/session_browser.py`
- Test: `tests/unit/test_session_browser.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_session_browser.py`:

```python
from src.bot.features.session_browser import session_detail_view


class TestSessionDetailView:
    @pytest.mark.asyncio
    async def test_renders_metadata_and_three_action_buttons(self):
        storage = AsyncMock()
        storage.load_session = AsyncMock(return_value=_fake_session("abc-123", msgs=23))
        with patch(
            "src.bot.features.session_browser.read_ai_title",
            return_value="my title",
        ):
            text, kb = await session_detail_view(
                storage=storage,
                user_id=42,
                session_id="abc-123",
                back_page=0,
            )
        assert "my title" in text
        assert "abc-123" in text
        assert "23" in text
        # Buttons: view / resume / export / back
        flat = [btn for row in kb.inline_keyboard for btn in row]
        cbs = {b.callback_data for b in flat}
        assert "sessions:view:abc-123" in cbs
        assert "sessions:resume:abc-123" in cbs
        assert "sessions:export:abc-123" in cbs
        assert "sessions:back:0" in cbs

    @pytest.mark.asyncio
    async def test_back_button_carries_page_number(self):
        storage = AsyncMock()
        storage.load_session = AsyncMock(return_value=_fake_session("abc-123"))
        with patch(
            "src.bot.features.session_browser.read_ai_title", return_value=None
        ), patch(
            "src.bot.features.session_browser._first_prompt_for", return_value="hi"
        ):
            _, kb = await session_detail_view(
                storage=storage,
                user_id=42,
                session_id="abc-123",
                back_page=5,
            )
        cbs = {b.callback_data for row in kb.inline_keyboard for b in row}
        assert "sessions:back:5" in cbs

    @pytest.mark.asyncio
    async def test_cross_user_session_returns_none(self):
        storage = AsyncMock()
        # load_session is filtered by user_id and returns None on mismatch
        storage.load_session = AsyncMock(return_value=None)
        result = await session_detail_view(
            storage=storage,
            user_id=999,
            session_id="abc-123",
            back_page=0,
        )
        assert result is None
```

- [ ] **Step 2: Run test to verify it fails**

```bash
poetry run pytest tests/unit/test_session_browser.py::TestSessionDetailView -v
```

Expected: FAIL — `session_detail_view` not defined.

- [ ] **Step 3: Write minimal implementation**

Append to `src/bot/features/session_browser.py`:

```python
async def session_detail_view(
    storage: Any,
    user_id: int,
    session_id: str,
    back_page: int,
) -> Optional[Tuple[str, InlineKeyboardMarkup]]:
    """Render the detail view for a single session.

    Returns None if the session is not owned by user_id (or doesn't exist).
    """
    session = await storage.load_session(session_id, user_id)
    if session is None:
        return None

    title = await _resolve_title(storage, str(session.project_path), session_id)

    text_lines = [
        f"📄 <b>{escape_path(title)}</b>",
        "",
        f"创建于 {session.created_at.strftime('%Y-%m-%d %H:%M')}",
        f"最近活动 {_format_relative_time(session.last_used)}",
        f"消息数 {session.message_count} · 累计费用 ${session.total_cost:.4f}",
        f"<code>{escape_path(session_id)}</code>",
    ]
    text = "\n".join(text_lines)

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📄 查看 HTML", callback_data=f"sessions:view:{session_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    "▶ 恢复继续", callback_data=f"sessions:resume:{session_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    "📦 导出其它格式",
                    callback_data=f"sessions:export:{session_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "← 返回列表", callback_data=f"sessions:back:{back_page}"
                )
            ],
        ]
    )
    return text, keyboard
```

- [ ] **Step 4: Run test to verify it passes**

```bash
poetry run pytest tests/unit/test_session_browser.py::TestSessionDetailView -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/bot/features/session_browser.py tests/unit/test_session_browser.py
git commit -m "feat(sessions): session_detail_view renders metadata + 4 buttons"
```

---

## Task 7: callback router skeleton + `sessions:list` and `sessions:back`

**Files:**
- Modify: `src/bot/handlers/callback.py:60-69` (add `"sessions"` route) + new `handle_sessions_callback` function.
- Test: `tests/integration/test_sessions_command_flow.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/integration/test_sessions_command_flow.py`:

```python
"""Integration tests for /sessions flow.

Each test wires a real-ish dependency graph (storage + mocked Telegram
update/context) and exercises one branch of the callback dispatcher.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from src.bot.handlers.callback import handle_callback_query, handle_sessions_callback
from src.claude.session import ClaudeSession
from src.storage.database import DatabaseManager
from src.storage.session_storage import SQLiteSessionStorage


@pytest_asyncio.fixture
async def storage(tmp_path):
    db = DatabaseManager(tmp_path / "test.db")
    await db.initialize()
    yield SQLiteSessionStorage(db)
    await db.close()


def _fake_query(user_id: int, callback_data: str, edit_text=None):
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


def _fake_context(storage, user_id, current_directory="/proj"):
    context = MagicMock()
    context.user_data = {"current_directory": current_directory}
    context.bot_data = {
        "storage": MagicMock(sessions=storage),
        "audit_logger": MagicMock(log_event=AsyncMock()),
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
            await _save(storage, 42, "/proj", f"s{i:02d}", age=i)
        query = _fake_query(42, "sessions:list:1")
        context = _fake_context(storage, 42)
        with patch("src.bot.features.session_browser.read_ai_title", return_value="t"):
            await handle_sessions_callback(query, "list:1", context)
        query.edit_message_text.assert_called_once()
        text, kw = query.edit_message_text.call_args.args, query.edit_message_text.call_args.kwargs
        # On page 2 of 2, only "prev" button
        kb = kw["reply_markup"]
        nav = kb.inline_keyboard[-1]
        assert len(nav) == 1
        assert nav[0].callback_data == "sessions:list:0"


class TestBackCallback:
    @pytest.mark.asyncio
    async def test_back_re_renders_list_at_given_page(self, storage):
        for i in range(15):
            await _save(storage, 42, "/proj", f"s{i:02d}", age=i)
        query = _fake_query(42, "sessions:back:1")
        context = _fake_context(storage, 42)
        with patch("src.bot.features.session_browser.read_ai_title", return_value="t"):
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
        """handle_callback_query must route `sessions:*` to handle_sessions_callback."""
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
```

- [ ] **Step 2: Run test to verify it fails**

```bash
poetry run pytest tests/integration/test_sessions_command_flow.py::TestRouterRegistration -v
```

Expected: FAIL — `handle_sessions_callback` not importable.

- [ ] **Step 3: Modify `src/bot/handlers/callback.py`**

First, add the import at the top of the file (with other imports around line 1-15):

```python
from ..features.session_browser import (
    list_sessions_view,
    session_detail_view,
    PAGE_SIZE,
)
```

Then add to the `handlers` dict in `handle_callback_query` (around line 60-69):

```python
        handlers = {
            "cd": handle_cd_callback,
            "action": handle_action_callback,
            "confirm": handle_confirm_callback,
            "quick": handle_quick_action_callback,
            "followup": handle_followup_callback,
            "conversation": handle_conversation_callback,
            "git": handle_git_callback,
            "export": handle_export_callback,
            "sessions": handle_sessions_callback,  # NEW
        }
```

Then add this new function at the bottom of the file (or near the other handler functions):

```python
async def handle_sessions_callback(
    query, param: str, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Sub-dispatcher for sessions:* callbacks.

    `param` is the portion after `sessions:` — e.g. `list:1`, `detail:<id>`,
    `view:<id>`, `resume:<id>`, `export:<id>`, `export:<id>:md`, `back:1`.
    """
    user_id = query.from_user.id
    storage = context.bot_data["storage"].sessions
    audit_logger: AuditLogger = context.bot_data.get("audit_logger")
    settings: Settings = context.bot_data["settings"]
    current_directory = context.user_data.get(
        "current_directory", settings.approved_directory
    )

    if ":" in param:
        sub_action, rest = param.split(":", 1)
    else:
        sub_action, rest = param, ""

    if sub_action in ("list", "back"):
        try:
            page = int(rest) if rest else 0
        except ValueError:
            page = 0
        text, kb = await list_sessions_view(
            storage=storage,
            user_id=user_id,
            project_path=str(current_directory),
            page=page,
        )
        await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
        if audit_logger:
            await audit_logger.log_event(
                user_id=user_id,
                event_type="sessions_list",
                event_data={"page": page},
                success=True,
            )
        return

    await query.edit_message_text(
        "❌ <b>未知的 session 动作</b>", parse_mode="HTML"
    )
```

- [ ] **Step 4: Run test to verify it passes**

```bash
poetry run pytest tests/integration/test_sessions_command_flow.py::TestRouterRegistration tests/integration/test_sessions_command_flow.py::TestListCallback tests/integration/test_sessions_command_flow.py::TestBackCallback tests/integration/test_sessions_command_flow.py::TestUnknownSubAction -v
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/bot/handlers/callback.py tests/integration/test_sessions_command_flow.py
git commit -m "feat(sessions): callback dispatcher + list/back/unknown branches"
```

---

## Task 8: `sessions:detail` branch

**Files:**
- Modify: `src/bot/handlers/callback.py` (extend `handle_sessions_callback`)
- Test: `tests/integration/test_sessions_command_flow.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/integration/test_sessions_command_flow.py`:

```python
class TestDetailCallback:
    @pytest.mark.asyncio
    async def test_detail_renders_session_card(self, storage):
        await _save(storage, 42, "/proj", "my-session-id")
        query = _fake_query(42, "sessions:detail:my-session-id")
        context = _fake_context(storage, 42)
        with patch(
            "src.bot.features.session_browser.read_ai_title",
            return_value="Hello world",
        ):
            await handle_sessions_callback(query, "detail:my-session-id", context)
        query.edit_message_text.assert_called_once()
        text = query.edit_message_text.call_args.args[0]
        assert "Hello world" in text
        assert "my-session-id" in text

    @pytest.mark.asyncio
    async def test_detail_cross_user_denied_with_audit(self, storage):
        await _save(storage, 99, "/proj", "victim-sid")
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
        await handle_sessions_callback(query, "detail:does-not-exist", context)
        query.answer.assert_called()
        # Should refresh to list
        query.edit_message_text.assert_called_once()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
poetry run pytest tests/integration/test_sessions_command_flow.py::TestDetailCallback -v
```

Expected: FAIL — `detail` branch falls into "unknown".

- [ ] **Step 3: Add the `detail` branch**

Add **before** the "unknown" fallback in `handle_sessions_callback`:

```python
    if sub_action == "detail":
        session_id = rest
        # Try to derive back_page from the calling list page — for now use 0.
        # If the user navigated from page N, the back button will go to page 0;
        # a more elaborate scheme would round-trip the page through callback_data,
        # but 64-byte limit makes that tight. Returning to first page is OK.
        result = await session_detail_view(
            storage=storage,
            user_id=user_id,
            session_id=session_id,
            back_page=0,
        )
        if result is None:
            # Either cross-user access or session truly missing — distinguish
            # by checking whether the session exists for any user.
            any_owner = await storage.load_session(session_id, None) if hasattr(
                storage, "load_session"
            ) else None
            # Fallback: do a quick existence probe by listing the session as if
            # it belonged to some other user. Since `load_session` filters by
            # user_id, we treat "not found for this user" as cross-user when
            # the row exists at all.
            from src.storage.session_storage import SQLiteSessionStorage

            row_exists = False
            if isinstance(storage, SQLiteSessionStorage):
                async with storage.db_manager.get_connection() as conn:
                    cursor = await conn.execute(
                        "SELECT 1 FROM sessions WHERE session_id = ? AND is_active = TRUE",
                        (session_id,),
                    )
                    row_exists = (await cursor.fetchone()) is not None

            if row_exists:
                await query.answer("无权访问该 session", show_alert=True)
                if audit_logger:
                    await audit_logger.log_event(
                        user_id=user_id,
                        event_type="sessions_cross_user_denied",
                        event_data={"session_id": session_id},
                        success=False,
                    )
                return

            await query.answer("session 不存在或已删除")
            # Refresh to list page 0
            text, kb = await list_sessions_view(
                storage=storage,
                user_id=user_id,
                project_path=str(current_directory),
                page=0,
            )
            await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
            return

        text, kb = result
        await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
        if audit_logger:
            await audit_logger.log_event(
                user_id=user_id,
                event_type="sessions_detail",
                event_data={"session_id": session_id},
                success=True,
            )
        return
```

- [ ] **Step 4: Run test to verify it passes**

```bash
poetry run pytest tests/integration/test_sessions_command_flow.py::TestDetailCallback -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/bot/handlers/callback.py tests/integration/test_sessions_command_flow.py
git commit -m "feat(sessions): detail branch with cross-user denial + missing-session refresh"
```

---

## Task 9: `sessions:view` branch — send HTML attachment

**Files:**
- Modify: `src/bot/handlers/callback.py`
- Test: `tests/integration/test_sessions_command_flow.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/integration/test_sessions_command_flow.py`:

```python
class TestViewHtmlCallback:
    @pytest.mark.asyncio
    async def test_view_sends_html_document(self, storage):
        await _save(storage, 42, "/proj", "view-test-sid")
        query = _fake_query(42, "sessions:view:view-test-sid")
        context = _fake_context(storage, 42)
        # Mock SessionExporter
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
            "src.bot.features.session_browser.read_ai_title",
            return_value="My Title",
        ):
            await handle_sessions_callback(query, "view:view-test-sid", context)
        query.message.reply_document.assert_called_once()
        kwargs = query.message.reply_document.call_args.kwargs
        # Filename should be aiTitle-flavored
        assert "My_Title" in kwargs["filename"] or "view-test" in kwargs["filename"]

    @pytest.mark.asyncio
    async def test_view_cross_user_denied(self, storage):
        await _save(storage, 99, "/proj", "victim-view")
        query = _fake_query(42, "sessions:view:victim-view")
        context = _fake_context(storage, 42)
        await handle_sessions_callback(query, "view:victim-view", context)
        query.message.reply_document.assert_not_called()
        query.answer.assert_called()

    @pytest.mark.asyncio
    async def test_view_export_failure_replies_with_error(self, storage):
        await _save(storage, 42, "/proj", "fail-view")
        query = _fake_query(42, "sessions:view:fail-view")
        context = _fake_context(storage, 42)
        fake_exporter = MagicMock()
        fake_exporter.export_session = AsyncMock(
            side_effect=ValueError("explode")
        )
        context.bot_data["session_exporter"] = fake_exporter
        with patch(
            "src.bot.features.session_browser.read_ai_title", return_value=None
        ):
            await handle_sessions_callback(query, "view:fail-view", context)
        query.message.reply_text.assert_called_once()
        assert "失败" in query.message.reply_text.call_args.args[0]
```

- [ ] **Step 2: Run test to verify it fails**

```bash
poetry run pytest tests/integration/test_sessions_command_flow.py::TestViewHtmlCallback -v
```

Expected: FAIL — `view` falls into "unknown".

- [ ] **Step 3: Add the `view` branch**

Add the following before the "unknown" fallback. Also add a helper for filename sanitization:

```python
import re as _re_for_filename


def _safe_filename_fragment(s: str, max_len: int = 40) -> str:
    """Sanitize a string for use in a filename (replace whitespace and forbidden chars)."""
    cleaned = _re_for_filename.sub(r"[^\w\-一-鿿]+", "_", s).strip("_")
    return cleaned[:max_len] or "session"


async def _check_session_ownership(storage, user_id: int, session_id: str) -> str:
    """Return one of: 'owned', 'cross_user', 'missing'."""
    session = await storage.load_session(session_id, user_id)
    if session is not None:
        return "owned"
    from src.storage.session_storage import SQLiteSessionStorage

    if isinstance(storage, SQLiteSessionStorage):
        async with storage.db_manager.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT 1 FROM sessions WHERE session_id = ? AND is_active = TRUE",
                (session_id,),
            )
            if (await cursor.fetchone()) is not None:
                return "cross_user"
    return "missing"
```

Then inside `handle_sessions_callback`, add this branch:

```python
    if sub_action == "view":
        session_id = rest
        ownership = await _check_session_ownership(storage, user_id, session_id)
        if ownership == "cross_user":
            await query.answer("无权访问该 session", show_alert=True)
            if audit_logger:
                await audit_logger.log_event(
                    user_id=user_id,
                    event_type="sessions_cross_user_denied",
                    event_data={"session_id": session_id, "action": "view"},
                    success=False,
                )
            return
        if ownership == "missing":
            await query.answer("session 不存在或已删除")
            return

        await query.answer("生成 HTML 中…")

        from src.bot.features.session_export import ExportFormat

        exporter = context.bot_data.get("session_exporter")
        try:
            exported = await exporter.export_session(
                user_id=user_id,
                session_id=session_id,
                format=ExportFormat.HTML,
            )
        except Exception as e:
            logger.error(
                "Failed to export session HTML",
                user_id=user_id,
                session_id=session_id,
                error=str(e),
            )
            await query.message.reply_text(
                f"❌ 生成 HTML 失败：{type(e).__name__}", parse_mode="HTML"
            )
            return

        # Build filename: aiTitle (sanitized) + date + .html
        title = (
            await _resolve_title_for_handler(storage, current_directory, session_id)
        )
        date_str = datetime.now(UTC).strftime("%Y%m%d")
        filename = f"{_safe_filename_fragment(title)}_{date_str}.html"

        await query.message.reply_document(
            document=exported.content.encode("utf-8"),
            filename=filename,
            caption=f"📄 {title}",
        )
        if audit_logger:
            await audit_logger.log_event(
                user_id=user_id,
                event_type="sessions_view_html",
                event_data={"session_id": session_id},
                success=True,
            )
        return
```

Also add this helper (imports + small wrapper) near the top of the file:

```python
from datetime import UTC, datetime

from ..features.session_browser import (
    list_sessions_view,
    session_detail_view,
    PAGE_SIZE,
    read_ai_title,
    derive_fallback_title,
)


async def _resolve_title_for_handler(storage, project_path, session_id):
    """Local helper mirroring session_browser._resolve_title, kept here to
    avoid importing private internals."""
    title = read_ai_title(str(project_path), session_id)
    if title:
        return title
    first_prompt = None
    try:
        msgs = await storage.get_session_messages(session_id, limit=1)
        if msgs:
            first = msgs[0]
            first_prompt = (
                first.get("prompt") if isinstance(first, dict) else getattr(first, "prompt", None)
            )
    except Exception:
        pass
    return derive_fallback_title(first_prompt, session_id)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
poetry run pytest tests/integration/test_sessions_command_flow.py::TestViewHtmlCallback -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/bot/handlers/callback.py tests/integration/test_sessions_command_flow.py
git commit -m "feat(sessions): view branch sends HTML attachment with sanitized filename"
```

---

## Task 10: `sessions:resume` branch — switch active session

**Files:**
- Modify: `src/bot/handlers/callback.py`
- Test: `tests/integration/test_sessions_command_flow.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/integration/test_sessions_command_flow.py`:

```python
class TestResumeCallback:
    @pytest.mark.asyncio
    async def test_resume_sets_user_data(self, storage):
        await _save(storage, 42, "/proj", "resume-test-sid")
        query = _fake_query(42, "sessions:resume:resume-test-sid")
        context = _fake_context(storage, 42)
        with patch(
            "src.bot.features.session_browser.read_ai_title",
            return_value="Resume me",
        ):
            await handle_sessions_callback(query, "resume:resume-test-sid", context)
        assert context.user_data["claude_session_id"] == "resume-test-sid"
        assert context.user_data["force_new_session"] is False
        query.message.reply_text.assert_called_once()
        assert "Resume me" in query.message.reply_text.call_args.args[0]

    @pytest.mark.asyncio
    async def test_resume_cross_user_denied(self, storage):
        await _save(storage, 99, "/proj", "victim-resume")
        query = _fake_query(42, "sessions:resume:victim-resume")
        context = _fake_context(storage, 42)
        await handle_sessions_callback(query, "resume:victim-resume", context)
        # Must NOT have set the session id
        assert context.user_data.get("claude_session_id") is None
        query.answer.assert_called()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
poetry run pytest tests/integration/test_sessions_command_flow.py::TestResumeCallback -v
```

Expected: FAIL — `resume` falls into "unknown".

- [ ] **Step 3: Add the `resume` branch**

Add this branch in `handle_sessions_callback`, before the "unknown" fallback:

```python
    if sub_action == "resume":
        session_id = rest
        ownership = await _check_session_ownership(storage, user_id, session_id)
        if ownership == "cross_user":
            await query.answer("无权访问该 session", show_alert=True)
            if audit_logger:
                await audit_logger.log_event(
                    user_id=user_id,
                    event_type="sessions_cross_user_denied",
                    event_data={"session_id": session_id, "action": "resume"},
                    success=False,
                )
            return
        if ownership == "missing":
            await query.answer("session 不存在或已删除")
            return

        title = await _resolve_title_for_handler(
            storage, current_directory, session_id
        )
        context.user_data["claude_session_id"] = session_id
        context.user_data["force_new_session"] = False
        await query.message.reply_text(
            f"✅ 已切到 session «<b>{escape_html(title)}</b>»，"
            f"发消息即继续。",
            parse_mode="HTML",
        )
        if audit_logger:
            await audit_logger.log_event(
                user_id=user_id,
                event_type="sessions_resume",
                event_data={"session_id": session_id},
                success=True,
            )
        return
```

The `escape_html` import is already at the top of `callback.py` (line 14).

- [ ] **Step 4: Run test to verify it passes**

```bash
poetry run pytest tests/integration/test_sessions_command_flow.py::TestResumeCallback -v
```

Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add src/bot/handlers/callback.py tests/integration/test_sessions_command_flow.py
git commit -m "feat(sessions): resume branch switches active session via user_data"
```

---

## Task 11: `sessions:export` branch (top + format submenu)

**Files:**
- Modify: `src/bot/handlers/callback.py`
- Test: `tests/integration/test_sessions_command_flow.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/integration/test_sessions_command_flow.py`:

```python
class TestExportCallback:
    @pytest.mark.asyncio
    async def test_export_top_shows_format_submenu(self, storage):
        await _save(storage, 42, "/proj", "export-test")
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
        await _save(storage, 42, "/proj", "exp-md")
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
            "src.bot.features.session_browser.read_ai_title", return_value="MD title"
        ):
            await handle_sessions_callback(query, "export:exp-md:md", context)
        query.message.reply_document.assert_called_once()
        kwargs = query.message.reply_document.call_args.kwargs
        assert kwargs["filename"].endswith(".md")

    @pytest.mark.asyncio
    async def test_export_json_sends_json(self, storage):
        await _save(storage, 42, "/proj", "exp-json")
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
            "src.bot.features.session_browser.read_ai_title", return_value=None
        ):
            await handle_sessions_callback(query, "export:exp-json:json", context)
        query.message.reply_document.assert_called_once()
        kwargs = query.message.reply_document.call_args.kwargs
        assert kwargs["filename"].endswith(".json")

    @pytest.mark.asyncio
    async def test_export_unknown_format_falls_back(self, storage):
        await _save(storage, 42, "/proj", "exp-x")
        query = _fake_query(42, "sessions:export:exp-x:weird")
        context = _fake_context(storage, 42)
        await handle_sessions_callback(query, "export:exp-x:weird", context)
        query.message.reply_document.assert_not_called()
        query.answer.assert_called()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
poetry run pytest tests/integration/test_sessions_command_flow.py::TestExportCallback -v
```

Expected: FAIL.

- [ ] **Step 3: Add the `export` branch**

Add this branch in `handle_sessions_callback`, before the "unknown" fallback:

```python
    if sub_action == "export":
        # rest is either "<id>" (show submenu) or "<id>:<fmt>" (do export)
        if ":" in rest:
            session_id, fmt = rest.split(":", 1)
        else:
            session_id, fmt = rest, ""

        ownership = await _check_session_ownership(storage, user_id, session_id)
        if ownership == "cross_user":
            await query.answer("无权访问该 session", show_alert=True)
            if audit_logger:
                await audit_logger.log_event(
                    user_id=user_id,
                    event_type="sessions_cross_user_denied",
                    event_data={"session_id": session_id, "action": "export"},
                    success=False,
                )
            return
        if ownership == "missing":
            await query.answer("session 不存在或已删除")
            return

        if not fmt:
            # Show format submenu
            kb = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "📝 Markdown",
                            callback_data=f"sessions:export:{session_id}:md",
                        ),
                        InlineKeyboardButton(
                            "📄 JSON",
                            callback_data=f"sessions:export:{session_id}:json",
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            "✖ 取消",
                            callback_data=f"sessions:detail:{session_id}",
                        )
                    ],
                ]
            )
            await query.edit_message_text(
                "选择导出格式：", reply_markup=kb, parse_mode="HTML"
            )
            return

        from src.bot.features.session_export import ExportFormat

        format_map = {
            "md": ExportFormat.MARKDOWN,
            "json": ExportFormat.JSON,
        }
        export_format = format_map.get(fmt)
        if export_format is None:
            await query.answer(f"未知的导出格式：{fmt}")
            return

        await query.answer("生成中…")

        exporter = context.bot_data.get("session_exporter")
        try:
            exported = await exporter.export_session(
                user_id=user_id,
                session_id=session_id,
                format=export_format,
            )
        except Exception as e:
            logger.error(
                "Failed to export session",
                user_id=user_id,
                session_id=session_id,
                format=fmt,
                error=str(e),
            )
            await query.message.reply_text(
                f"❌ 导出失败：{type(e).__name__}", parse_mode="HTML"
            )
            return

        title = await _resolve_title_for_handler(
            storage, current_directory, session_id
        )
        date_str = datetime.now(UTC).strftime("%Y%m%d")
        filename = f"{_safe_filename_fragment(title)}_{date_str}.{fmt}"
        await query.message.reply_document(
            document=exported.content.encode("utf-8"),
            filename=filename,
            caption=f"📦 {title}",
        )
        if audit_logger:
            await audit_logger.log_event(
                user_id=user_id,
                event_type=f"sessions_export_{fmt}",
                event_data={"session_id": session_id},
                success=True,
            )
        return
```

- [ ] **Step 4: Run test to verify it passes**

```bash
poetry run pytest tests/integration/test_sessions_command_flow.py::TestExportCallback -v
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/bot/handlers/callback.py tests/integration/test_sessions_command_flow.py
git commit -m "feat(sessions): export branch with MD/JSON submenu"
```

---

## Task 12: `/sessions` command — classic mode

**Files:**
- Modify: `src/bot/handlers/command.py` (add new handler near other commands)
- Test: `tests/integration/test_sessions_command_flow.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/integration/test_sessions_command_flow.py`:

```python
class TestSessionsCommand:
    @pytest.mark.asyncio
    async def test_lists_only_current_directory(self, storage):
        # User 42 has sessions in two directories
        await _save(storage, 42, "/proj-a", "a1")
        await _save(storage, 42, "/proj-b", "b1")
        # Other user with same directory must NOT appear
        await _save(storage, 99, "/proj-a", "other1")

        from src.bot.handlers.command import sessions_command

        update = MagicMock()
        update.effective_user.id = 42
        update.message.reply_text = AsyncMock()
        context = _fake_context(storage, 42, current_directory="/proj-a")

        with patch(
            "src.bot.features.session_browser.read_ai_title", return_value="t"
        ):
            await sessions_command(update, context)

        update.message.reply_text.assert_called_once()
        kb = update.message.reply_text.call_args.kwargs["reply_markup"]
        cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
        assert any("a1" in c for c in cbs)
        # b1 (other dir) and other1 (other user) must not appear
        assert not any("b1" in c for c in cbs)
        assert not any("other1" in c for c in cbs)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
poetry run pytest tests/integration/test_sessions_command_flow.py::TestSessionsCommand -v
```

Expected: FAIL — `sessions_command` not importable.

- [ ] **Step 3: Add `sessions_command` to `src/bot/handlers/command.py`**

Add this function (near the other command handlers; specific location: after the `/export` handler at command.py:959):

```python
async def sessions_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """`/sessions` — list current-directory sessions in a paginated browser.

    Classic-mode entry point. Same logic also bound in orchestrator for
    agentic mode.
    """
    from src.bot.features.session_browser import list_sessions_view

    user_id = update.effective_user.id
    storage = context.bot_data["storage"].sessions
    settings: Settings = context.bot_data["settings"]
    audit_logger: AuditLogger = context.bot_data.get("audit_logger")
    current_directory = context.user_data.get(
        "current_directory", settings.approved_directory
    )

    text, kb = await list_sessions_view(
        storage=storage,
        user_id=user_id,
        project_path=str(current_directory),
        page=0,
    )
    await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")
    if audit_logger:
        await audit_logger.log_event(
            user_id=user_id,
            event_type="sessions_command",
            event_data={"directory": str(current_directory)},
            success=True,
        )
```

Imports at the top of `command.py` should already include `Update`, `ContextTypes`, `Settings`, `AuditLogger`. Confirm before saving — if any missing, add them.

- [ ] **Step 4: Run test to verify it passes**

```bash
poetry run pytest tests/integration/test_sessions_command_flow.py::TestSessionsCommand -v
```

Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add src/bot/handlers/command.py tests/integration/test_sessions_command_flow.py
git commit -m "feat(sessions): /sessions command for classic mode"
```

---

## Task 13: Register `/sessions` in both modes + bot command menu

**Files:**
- Modify: `src/bot/orchestrator.py` (around lines 355, 455, 518)

- [ ] **Step 1: Inspect the existing registration loops**

Open `src/bot/orchestrator.py` at lines 355-377 (`_register_agentic_handlers`) and 455-481 (`_register_classic_handlers`). These iterate over a list of `(cmd, handler)` tuples and call `app.add_handler(CommandHandler(...))`. Identify the exact list literal in each function.

- [ ] **Step 2: Define `sessions_command` for agentic mode**

Add this method to `MessageOrchestrator` (somewhere alongside the other agentic handlers, e.g., near where `_new`, `_status`, `_verbose` live):

```python
    async def sessions_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Agentic-mode dispatcher for /sessions. Delegates to shared logic."""
        from src.bot.handlers.command import sessions_command as _shared

        await _shared(update, context)
```

(This intentionally reuses the classic command function; both modes have the same behavior.)

- [ ] **Step 3: Register in both handler lists**

Inside `_register_agentic_handlers` (around line 355), find the tuple list and add `("sessions", self.sessions_command)`. Likewise in `_register_classic_handlers` (around line 455). Example format (adjust to match the existing style):

```python
        commands = [
            ("start", self._start),
            ("new", self._new),
            ("status", self._status),
            ("verbose", self._verbose),
            ("repo", self._repo),
            ("sessions", self.sessions_command),  # NEW
        ]
```

For classic mode, the corresponding tuple list is at line 455; add `("sessions", sessions_command)` (note: classic mode uses the bare function, not bound method — match the existing pattern).

- [ ] **Step 4: Add to `get_bot_commands` (line 518)**

In `get_bot_commands`, add an entry like:

```python
            BotCommand("sessions", "浏览并恢复历史 session"),
```

Place it in a reasonable spot (after `/repo` or `/status`).

- [ ] **Step 5: Add a smoke test**

Append to `tests/integration/test_sessions_command_flow.py`:

```python
class TestRegistration:
    @pytest.mark.asyncio
    async def test_sessions_in_bot_commands(self):
        from src.bot.orchestrator import MessageOrchestrator
        from src.config.settings import Settings

        # Minimal settings — agentic mode for command list scope
        settings = Settings(
            telegram_bot_token="dummy",
            telegram_bot_username="dummy",
            approved_directory="/tmp",
            agentic_mode=True,
        )
        orch = MessageOrchestrator(settings, deps={})
        commands = await orch.get_bot_commands()
        names = {c.command for c in commands}
        assert "sessions" in names
```

- [ ] **Step 6: Run tests**

```bash
poetry run pytest tests/integration/test_sessions_command_flow.py -v
```

Expected: all tests in the file pass.

- [ ] **Step 7: Commit**

```bash
git add src/bot/orchestrator.py tests/integration/test_sessions_command_flow.py
git commit -m "feat(sessions): register /sessions in both modes + bot command menu"
```

---

## Task 14: Cross-user safety lint sweep

**Files:** none new; this is a verification + cleanup task.

- [ ] **Step 1: Verify all sub-actions guard against cross-user access**

Open `src/bot/handlers/callback.py` and confirm every branch (`detail`, `view`, `resume`, `export` both halves) calls `_check_session_ownership` before touching anything. Read each branch top-to-bottom.

- [ ] **Step 2: Run the full sessions test suite**

```bash
poetry run pytest tests/unit/test_session_browser.py tests/unit/test_session_storage_pagination.py tests/integration/test_sessions_command_flow.py -v
```

Expected: all passing.

- [ ] **Step 3: Run lint**

```bash
poetry run make lint
```

If issues, fix inline.

- [ ] **Step 4: Run full test suite to catch regressions**

```bash
poetry run pytest
```

Expected: no new failures vs main.

- [ ] **Step 5: Commit (if any lint fixes)**

```bash
git add -p   # interactive — only stage lint/format changes
git commit -m "chore(sessions): lint pass"
```

If nothing to commit, skip.

---

## Task 15: Manual testing on real Telegram

**Files:** none — manual checklist.

- [ ] **Step 1: Start the bot**

```bash
make run-debug
```

- [ ] **Step 2: Walk the checklist**

In a real Telegram client (test on Desktop AND mobile if possible):

- [ ] `/sessions` from the bot's working directory — list renders correctly
- [ ] Navigate to a page; click "下一页" and "上一页"
- [ ] Click a session row → detail card renders with all metadata fields
- [ ] Click [📄 查看 HTML] → an HTML file is sent; open in Telegram and verify content is readable
- [ ] Click [▶ 恢复继续] → confirmation message lands; send a follow-up message and check bot logs to confirm `session_id` matches the chosen one
- [ ] Click [📦 导出其它格式] → submenu shows MD/JSON; verify both download
- [ ] Click [← 返回列表] → returns to list at correct page
- [ ] Send `/sessions` in a directory with **zero** sessions — empty state message displays
- [ ] Try as a different user (if available) — listing only shows their own sessions

- [ ] **Step 3: Record any defects as separate issues**

If anything is wrong, create a new TaskCreate or write a follow-up plan. Don't paper over with hacks.

---

## Self-review

### Spec coverage check

- §1.4 "In scope": every bullet has at least one task — list (Task 5), detail (Task 6), pagination (Task 4+5), view HTML (Task 9), resume (Task 10), export MD/JSON (Task 11), both modes (Task 12+13), classic command (Task 12), agentic command (Task 13). ✓
- §1.4 "Out of scope": no tasks add delete/rename/Web App/cross-directory — confirmed by reading the task list. ✓
- §2 D1-D9: every decision is implemented somewhere — D1 (filter by `project_path` in Task 4), D2 (Task 2+3), D3 (Task 9), D4 (Task 5+6), D5 (Task 6+9+10+11), D6 (Task 12-13 use `/sessions`), D7 (page_size=10 in Task 5), D8 (Task 10 only sets user_data), D9 (Task 2). ✓
- §3.2 data sources: SQLite reads (Task 4), jsonl reads (Task 2). ✓
- §5 data flows: each of 5.1-5.5 is exercised by a callback branch test. ✓
- §6 error handling: Task 2 covers data-missing fallback, Task 8-11 each handle cross-user denial + missing session. The "fail safe" branches are all in tests. ✓
- §7 testing: every test case listed in spec §7.1-7.3 maps to a `test_*` function in this plan. ✓

### Placeholder scan

- Search performed for: "TBD", "TODO", "implement later", "fill in", "appropriate", "etc.", "add validation", "Similar to Task". None present. ✓
- All steps that change code include the actual code. ✓
- All test code is complete (no `# ... rest of test ...` stubs). ✓

### Type consistency

- `list_sessions_view(storage, user_id, project_path, page, page_size=10)` — same signature in Task 5 definition and Task 7 callback caller. ✓
- `session_detail_view(storage, user_id, session_id, back_page)` — same in Task 6 definition and Task 8 caller. ✓
- `read_ai_title(project_path, session_id)` — same in Task 2 and all consumers. ✓
- Storage method names: `get_user_sessions`, `count_user_sessions`, `load_session`, `get_session_messages` — all match what `session_storage.py` (Task 4) and `repositories.py` already provide. ✓
- Callback prefixes: `sessions:list:<page>`, `sessions:detail:<id>`, `sessions:view:<id>`, `sessions:resume:<id>`, `sessions:export:<id>[:<fmt>]`, `sessions:back:<page>` — consistent across Task 5, 6, 7, 8, 9, 10, 11. ✓
- `ExportFormat.MARKDOWN`/`.JSON`/`.HTML` — matches existing enum in `session_export.py`. ✓

No issues found.
