"""Session browser logic for the /sessions Telegram command.

Pure logic only — no Telegram side-effects, no SDK calls.
"""

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Optional, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from src.claude.facade import ClaudeIntegration

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
                    "📄 查看 HTML",
                    callback_data=f"sessions:view:{session_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "▶ 恢复继续",
                    callback_data=f"sessions:resume:{session_id}",
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
                    "← 返回列表",
                    callback_data=f"sessions:back:{back_page}",
                )
            ],
        ]
    )
    return text, keyboard
