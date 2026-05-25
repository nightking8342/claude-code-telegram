"""Session browser logic for the /sessions Telegram command.

Pure logic only — no Telegram side-effects, no SDK calls.
"""

import re
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional, Tuple

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from src.claude.facade import ClaudeIntegration

logger = structlog.get_logger()

_COMMAND_WRAPPER_RE = re.compile(
    r"<command-(message|name)>.*?</command-\1>",
    flags=re.DOTALL | re.IGNORECASE,
)
_FALLBACK_MAX_CHARS = 60

PAGE_SIZE = 10
_ROW_TITLE_MAX = 35  # characters in list-row button label


def derive_fallback_title(first_prompt: Optional[str], session_id: str) -> str:
    """Fallback title when aiTitle is unavailable.

    Strips ``<command-message>...</command-message>`` and
    ``<command-name>...</command-name>`` wrappers that Telegram's ``/command``
    infrastructure adds; truncates to 60 chars + ellipsis. If nothing useful
    is left, falls back to ``Session <id[:8]>``.
    """
    if first_prompt:
        cleaned = _COMMAND_WRAPPER_RE.sub("", first_prompt).strip()
        if cleaned:
            if len(cleaned) > _FALLBACK_MAX_CHARS:
                return cleaned[:_FALLBACK_MAX_CHARS] + "…"
            return cleaned
    return f"Session {session_id[:8]}"


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


def escape_path(path: str) -> str:
    """Escape minimal HTML chars for path display."""
    return path.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


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


async def _resolve_title(storage: Any, project_path: str, session_id: str) -> str:
    """Resolve display title: CLI aiTitle -> first prompt -> session id-based."""
    title = await ClaudeIntegration.read_session_title(session_id, Path(project_path))
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

    Merges DB sessions (bot-created) with CLI sessions (from JSONL files).
    Returns (text, InlineKeyboardMarkup). No Telegram side-effects.
    """
    # --- DB sessions ---
    db_total = await storage.count_user_sessions(
        user_id, project_path=project_path
    )
    db_sessions: list = []
    if db_total > 0:
        db_sessions = await storage.get_user_sessions(
            user_id,
            project_path=project_path,
            limit=db_total,  # fetch all for merging
            offset=0,
        )

    # --- CLI sessions (JSONL files) ---
    cli_map: dict = {}  # session_id → {message_count, last_used}
    try:
        cli_raw = await ClaudeIntegration.scan_cli_sessions(
            Path(project_path)
        )
        for d in cli_raw:
            cli_map[d["session_id"]] = d
    except Exception:
        logger.debug("CLI session scan failed", exc_info=True)

    # For DB sessions: use max(DB count, CLI count), tag if CLI has more
    for s in db_sessions:
        cli = cli_map.pop(s.session_id, None)
        if cli:
            if cli["message_count"] > s.message_count:
                s.message_count = cli["message_count"]
            s._is_cli = True
        else:
            s._is_cli = False

    # Remaining CLI-only sessions
    cli_sessions = [
        SimpleNamespace(
            session_id=d["session_id"],
            last_used=d["last_used"],
            message_count=d["message_count"],
            _is_cli=True,
        )
        for d in cli_map.values()
    ]

    # --- Merge + sort ---
    all_sessions = db_sessions + cli_sessions
    if not all_sessions:
        text = (
            "📂 当前目录下还没有 session — 直接发条消息开个新的吧。\n"
            f"<code>{escape_path(project_path)}</code>"
        )
        return text, InlineKeyboardMarkup([])

    all_sessions.sort(key=lambda s: s.last_used, reverse=True)
    total = len(all_sessions)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))

    page_sessions = all_sessions[page * page_size : (page + 1) * page_size]

    rows: list[list[InlineKeyboardButton]] = []
    for s in page_sessions:
        title = await _resolve_title(storage, project_path, s.session_id)
        label = _truncate(title, _ROW_TITLE_MAX)
        cli_tag = " · CLI" if getattr(s, "_is_cli", False) else ""
        suffix = (
            f" · {_format_relative_time(s.last_used)}"
            f" · {s.message_count}条{cli_tag}"
        )
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
            InlineKeyboardButton(
                "← 上一页", callback_data=f"sessions:list:{page - 1}"
            )
        )
    if page < total_pages - 1:
        nav.append(
            InlineKeyboardButton(
                "下一页 →", callback_data=f"sessions:list:{page + 1}"
            )
        )
    if nav:
        rows.append(nav)

    text = (
        f"📂 当前目录的 sessions（第 {page + 1} 页 / 共 {total_pages} 页 "
        f"· 共 {total} 个）\n"
        f"<code>{escape_path(project_path)}</code>"
    )
    return text, InlineKeyboardMarkup(rows)


async def session_detail_view(
    storage: Any,
    user_id: int,
    session_id: str,
    back_page: int,
    session_timeout_hours: int = 24,
    project_path: Optional[str] = None,
) -> Optional[Tuple[str, InlineKeyboardMarkup]]:
    """Render the detail view for a single session.

    Returns None if the session is not owned by user_id (or doesn't exist).
    CLI sessions (not in DB) are supported when *project_path* is provided.
    """
    session = await storage.load_session(session_id, user_id)
    is_cli = False

    if session is None and project_path:
        # Check if this is a CLI session
        cli_sessions = await ClaudeIntegration.scan_cli_sessions(
            Path(project_path)
        )
        for cs in cli_sessions:
            if cs["session_id"] == session_id:
                now = datetime.now(UTC)
                session = SimpleNamespace(
                    session_id=session_id,
                    user_id=user_id,
                    project_path=Path(project_path),
                    created_at=cs["created_at"] or now,
                    last_used=cs["last_used"],
                    total_cost=0.0,
                    total_turns=0,
                    message_count=cs["message_count"],
                    tools_used=[],
                    is_expired=lambda _h: False,
                )
                is_cli = True
                break

    if session is None:
        return None

    title = await _resolve_title(storage, str(session.project_path), session_id)
    expired = session.is_expired(session_timeout_hours)

    text_lines = [
        f"📄 <b>{escape_path(title)}</b>",
    ]
    if is_cli:
        text_lines.append("<i>CLI session（未在 bot 数据库中）</i>")
    text_lines.append("")
    text_lines.append(
        f"创建于 {session.created_at.strftime('%Y-%m-%d %H:%M')}"
    )
    text_lines.append(f"最近活动 {_format_relative_time(session.last_used)}")
    if is_cli:
        text_lines.append(f"消息数 {session.message_count}")
    else:
        text_lines.append(
            f"消息数 {session.message_count}"
            f" · 累计费用 ${session.total_cost:.4f}"
        )
    if expired:
        text_lines.append("⏰ 超过自动恢复时限，手动恢复仍可用")
    text_lines.append(f"<code>{escape_path(session_id)}</code>")
    text = "\n".join(text_lines)

    action_rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                "📄 HTML",
                callback_data=f"sessions:view:{session_id}",
            ),
            InlineKeyboardButton(
                "📝 Markdown",
                callback_data=f"sessions:export:{session_id}:md",
            ),
            InlineKeyboardButton(
                "📋 JSON",
                callback_data=f"sessions:export:{session_id}:json",
            ),
        ],
        [
            InlineKeyboardButton(
                "▶ 恢复继续",
                callback_data=f"sessions:resume:{session_id}",
            )
        ],
    ]
    action_rows.append(
        [
            InlineKeyboardButton(
                "← 返回列表",
                callback_data=f"sessions:back:{back_page}",
            )
        ]
    )
    return text, InlineKeyboardMarkup(action_rows)
