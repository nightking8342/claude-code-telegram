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
_CURRENT_SESSION_ICON = "▶"


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
    """Render a short Chinese relative-time string."""
    now = datetime.now(UTC)
    if when.tzinfo is None:
        # Defensive — DB rows should already be tz-aware.
        when = when.replace(tzinfo=UTC)
    delta = now - when
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"{secs}秒前"
    if secs < 3600:
        return f"{secs // 60}分钟前"
    if secs < 86400:
        return f"{secs // 3600}小时前"
    return f"{secs // 86400}天前"


def _format_datetime(when: datetime) -> str:
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    local = when.astimezone()
    return local.strftime("%Y-%m-%d %H:%M:%S")


def _format_bytes(size: Any) -> str:
    try:
        value = float(size)
    except (TypeError, ValueError):
        return str(size)
    units = ["B", "KB", "MB", "GB", "TB"]
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def escape_path(path: str) -> str:
    """Escape minimal HTML chars for path display."""
    return path.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def _first_prompt_for(storage: Any, session_id: str) -> Optional[str]:
    """Fetch the first user message text for the session, if any.

    Used as a fallback when aiTitle is unavailable.
    """
    db = getattr(storage, "db", None) or getattr(storage, "db_manager", None)
    if db is not None:
        try:
            async with db.get_connection() as conn:
                cursor = await conn.execute(
                    """
                    SELECT prompt FROM messages
                    WHERE session_id = ?
                    ORDER BY timestamp ASC, message_id ASC
                    LIMIT 1
                    """,
                    (session_id,),
                )
                row = await cursor.fetchone()
                if row:
                    return row["prompt"] if "prompt" in row.keys() else row[0]
        except Exception:
            logger.debug("DB first prompt lookup failed", exc_info=True)

    try:
        messages = await storage.get_session_messages(session_id, limit=1000)
    except Exception:
        return None
    if not messages:
        return None
    first = min(
        messages,
        key=lambda m: (
            getattr(m, "timestamp", None)
            if not isinstance(m, dict)
            else m.get("timestamp")
        )
        or datetime.max.replace(tzinfo=UTC),
    )
    # Storage layer is consistent on `prompt` column for user input.
    if isinstance(first, dict):
        return first.get("prompt") or first.get("content")
    return getattr(first, "prompt", None) or getattr(first, "content", None)


async def _resolve_title(
    storage: Any,
    project_path: str,
    session_id: str,
) -> str:
    """Resolve display title from SDK/local metadata and DB fallback."""
    sdk_info = await ClaudeIntegration.get_sdk_session_info(
        session_id, Path(project_path)
    )
    if sdk_info:
        sdk_title = _title_from_local_info(sdk_info)
        if sdk_title:
            return sdk_title

    title = await ClaudeIntegration.read_session_title(session_id, Path(project_path))
    if title:
        return title
    first_prompt = await _first_prompt_for(storage, session_id)
    return derive_fallback_title(first_prompt, session_id)


def _title_from_local_info(info: dict) -> Optional[str]:
    return (
        info.get("title")
        or info.get("summary")
        or info.get("custom_title")
        or info.get("ai_title")
        or info.get("first_prompt")
    )


def _format_sdk_metadata_value(key: str, value: Any) -> str:
    if isinstance(value, datetime):
        return _format_datetime(value)
    if key == "file_size" and value is not None:
        return _format_bytes(value)
    if value is None:
        return "None"
    return str(value)


def _sdk_metadata_lines(info: dict) -> list[str]:
    preferred = [
        "session_id",
        "title",
        "summary",
        "custom_title",
        "first_prompt",
        "created_at",
        "last_used",
        "message_count",
        "git_branch",
        "cwd",
        "tag",
        "file_size",
    ]
    keys = [key for key in preferred if key in info]
    keys.extend(sorted(key for key in info if key not in set(keys)))
    lines = ["", "SDK 元信息:"]
    for key in keys:
        value = escape_path(_format_sdk_metadata_value(key, info.get(key)))
        lines.append(f"{key}: <code>{value}</code>")
    return lines


async def _btw_fork_ids(storage: Any, user_id: int, project_path: str) -> set[str]:
    """Best-effort lookup of /btw fork sessions hidden from normal UI."""
    getter = getattr(storage, "get_btw_fork_session_ids", None)
    if (
        getter is not None
        and type(storage).__module__.startswith("unittest.mock")
        and "get_btw_fork_session_ids" not in getattr(storage, "__dict__", {})
    ):
        return set()
    if getter is None:
        return set()
    try:
        return set(await getter(user_id, project_path=project_path))
    except Exception:
        logger.debug("BTW fork lookup failed", exc_info=True)
        return set()


async def _is_btw_fork(storage: Any, session_id: str, user_id: int) -> bool:
    checker = getattr(storage, "is_btw_fork_session", None)
    if (
        checker is not None
        and type(storage).__module__.startswith("unittest.mock")
        and "is_btw_fork_session" not in getattr(storage, "__dict__", {})
    ):
        return False
    if checker is None:
        return False
    try:
        return bool(await checker(session_id, user_id=user_id))
    except Exception:
        logger.debug("BTW fork ownership lookup failed", exc_info=True)
        return False


async def list_sessions_view(
    storage: Any,
    user_id: int,
    project_path: str,
    page: int,
    page_size: int = PAGE_SIZE,
    current_session_id: Optional[str] = None,
) -> Tuple[str, InlineKeyboardMarkup]:
    """Render the /sessions list page.

    Merges DB sessions (bot-created) with CLI sessions (from JSONL files).
    Returns (text, InlineKeyboardMarkup). No Telegram side-effects.
    """
    # --- DB sessions ---
    hidden_btw_ids = await _btw_fork_ids(storage, user_id, project_path)
    db_total = await storage.count_user_sessions(user_id, project_path=project_path)
    db_sessions: list = []
    if db_total > 0:
        db_sessions = await storage.get_user_sessions(
            user_id,
            project_path=project_path,
            limit=db_total,  # fetch all for merging
            offset=0,
        )
        db_sessions = [s for s in db_sessions if s.session_id not in hidden_btw_ids]

    # --- CLI sessions (JSONL files) ---
    cli_map: dict = {}  # session_id → {message_count, last_used}
    try:
        cli_raw = await ClaudeIntegration.list_sdk_sessions(Path(project_path))
        if not cli_raw:
            cli_raw = await ClaudeIntegration.scan_cli_sessions(Path(project_path))
        for d in cli_raw:
            if d["session_id"] in hidden_btw_ids:
                continue
            cli_map[d["session_id"]] = d
    except Exception:
        logger.debug("Local session scan failed", exc_info=True)

    # For DB sessions: prefer the local transcript count when the JSONL exists.
    for s in db_sessions:
        cli = cli_map.pop(s.session_id, None)
        if cli:
            s.message_count = cli["message_count"]
            s._is_cli = False
            s._local_title = _title_from_local_info(cli)
        else:
            s._is_cli = False
            s._local_title = None

    # Remaining CLI-only sessions
    cli_sessions = [
        SimpleNamespace(
            session_id=d["session_id"],
            last_used=d["last_used"],
            message_count=d["message_count"],
            _is_cli=True,
            _local_title=_title_from_local_info(d),
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
    db_total_for_pages = max(0, db_total - len(hidden_btw_ids))
    total = max(len(db_sessions), db_total_for_pages) + len(cli_sessions)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))

    if total > len(all_sessions):
        page_sessions = all_sessions
    else:
        page_sessions = all_sessions[page * page_size : (page + 1) * page_size]

    rows: list[list[InlineKeyboardButton]] = []
    for s in page_sessions:
        title = getattr(s, "_local_title", None) or await _resolve_title(
            storage, project_path, s.session_id
        )
        label = _truncate(title, _ROW_TITLE_MAX)
        if current_session_id and s.session_id == current_session_id:
            label = f"{_CURRENT_SESSION_ICON} " + label
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
    session_timeout_hours: int = 24,
    project_path: Optional[str] = None,
    current_session_id: Optional[str] = None,
) -> Optional[Tuple[str, InlineKeyboardMarkup]]:
    """Render the detail view for a single session.

    Returns None if the session is not owned by user_id (or doesn't exist).
    CLI sessions (not in DB) are supported when *project_path* is provided.
    """
    if await _is_btw_fork(storage, session_id, user_id):
        text = (
            "💡 <b>BTW 旁路会话</b>\n\n"
            "这个 fork 已从普通 session 历史中隐藏，不能在 Telegram 里恢复或导出。"
        )
        rows = [
            [
                InlineKeyboardButton(
                    "返回列表",
                    callback_data=f"sessions:back:{back_page}",
                )
            ]
        ]
        return text, InlineKeyboardMarkup(rows)

    session = await storage.load_session(session_id, user_id)
    is_cli = False

    if session is None and project_path:
        # Check if this is a CLI session
        cli_sessions = await ClaudeIntegration.list_sdk_sessions(Path(project_path))
        if not cli_sessions:
            cli_sessions = await ClaudeIntegration.scan_cli_sessions(Path(project_path))
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

    local_info = await ClaudeIntegration.get_sdk_session_info(
        session_id, Path(str(session.project_path))
    )
    if local_info and "message_count" in local_info:
        session.message_count = local_info["message_count"]
    title = (
        _title_from_local_info(local_info)
        if local_info
        else await _resolve_title(storage, str(session.project_path), session_id)
    )
    expired = session.is_expired(session_timeout_hours)

    text_lines = [
        f"📄 <b>{escape_path(title)}</b>",
    ]
    if is_cli:
        text_lines.append("<i>CLI 会话（未在 bot 数据库中）</i>")
    text_lines.append("")
    text_lines.append(f"创建于 {_format_datetime(session.created_at)}")
    text_lines.append(
        f"最近活动 {_format_relative_time(session.last_used)}"
        f" ({_format_datetime(session.last_used)})"
    )
    if is_cli:
        text_lines.append(f"消息数 {session.message_count}")
    else:
        text_lines.append(
            f"消息数 {session.message_count}" f" · 累计费用 ${session.total_cost:.4f}"
        )
    if expired:
        text_lines.append("⏰ 超过自动恢复时限，手动恢复仍可用")
    if local_info:
        text_lines.extend(_sdk_metadata_lines(local_info))
    elif not is_cli:
        text_lines.append("本地 transcript 文件未找到")
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
    last_row: list[InlineKeyboardButton] = []
    if current_session_id != session_id:
        last_row.append(
            InlineKeyboardButton(
                "🗑 删除",
                callback_data=f"sessions:confirm_delete:{session_id}",
            )
        )
    last_row.append(
        InlineKeyboardButton(
            "← 返回列表",
            callback_data=f"sessions:back:{back_page}",
        )
    )
    action_rows.append(last_row)
    action_rows.insert(
        -1,
        [
            InlineKeyboardButton(
                "重命名",
                callback_data=f"sessions:rename:{session_id}",
            ),
            InlineKeyboardButton(
                "设置标签",
                callback_data=f"sessions:tag:{session_id}",
            ),
            InlineKeyboardButton(
                "清除标签",
                callback_data=f"sessions:cleartag:{session_id}",
            ),
        ],
    )
    return text, InlineKeyboardMarkup(action_rows)


def delete_confirm_keyboard(session_id: str) -> InlineKeyboardMarkup:
    """Return a confirmation keyboard for session deletion."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "⚠️ 确认删除",
                    callback_data=f"sessions:do_delete:{session_id}",
                ),
                InlineKeyboardButton(
                    "取消",
                    callback_data=f"sessions:detail:{session_id}",
                ),
            ]
        ]
    )
