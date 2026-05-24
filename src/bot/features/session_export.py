"""Session export functionality for exporting chat history in various formats."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum

from src.storage.facade import Storage
from src.utils.constants import MAX_SESSION_LENGTH


class ExportFormat(Enum):
    """Supported export formats."""

    MARKDOWN = "markdown"
    JSON = "json"
    HTML = "html"


@dataclass
class ExportedSession:
    """Exported session data."""

    format: ExportFormat
    content: str
    filename: str
    mime_type: str
    size_bytes: int
    created_at: datetime


class SessionExporter:
    """Handles exporting chat sessions in various formats."""

    def __init__(self, storage: Storage):
        """Initialize exporter with storage dependency.

        Args:
            storage: Storage facade for session data access
        """
        self.storage = storage

    async def export_session(
        self,
        user_id: int,
        session_id: str,
        format: ExportFormat = ExportFormat.MARKDOWN,
    ) -> ExportedSession:
        """Export a session in the specified format.

        Args:
            user_id: User ID
            session_id: Session ID to export
            format: Export format (markdown, json, html)

        Returns:
            ExportedSession with exported content

        Raises:
            ValueError: If session not found or invalid format
        """
        # Get session data
        session_model = await self.storage.sessions.get_session(session_id)
        if not session_model:
            raise ValueError(f"Session {session_id} not found")

        # Get session messages
        message_models = await self.storage.messages.get_session_messages(
            session_id, limit=MAX_SESSION_LENGTH
        )

        # Convert models to dicts for export methods
        session = {
            "id": session_model.session_id,
            "user_id": session_model.user_id,
            "created_at": session_model.created_at,
            "updated_at": session_model.last_used,
        }
        messages = []
        for i, msg in enumerate(message_models):
            if msg.prompt:
                messages.append({
                    "id": i,
                    "role": "user",
                    "content": msg.prompt,
                    "created_at": msg.timestamp,
                })
            if msg.response:
                messages.append({
                    "id": i,
                    "role": "assistant",
                    "content": msg.response,
                    "created_at": msg.timestamp,
                })

        # Export based on format
        if format == ExportFormat.MARKDOWN:
            content = await self._export_markdown(session, messages)
            mime_type = "text/markdown"
            extension = "md"
        elif format == ExportFormat.JSON:
            content = await self._export_json(session, messages)
            mime_type = "application/json"
            extension = "json"
        elif format == ExportFormat.HTML:
            content = await self._export_html(session, messages)
            mime_type = "text/html"
            extension = "html"
        else:
            raise ValueError(f"Unsupported export format: {format}")

        # Create filename
        timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        filename = f"session_{session_id}_{timestamp}.{extension}"

        return ExportedSession(
            format=format,
            content=content,
            filename=filename,
            mime_type=mime_type,
            size_bytes=len(content.encode()),
            created_at=datetime.now(UTC),
        )

    async def _export_markdown(self, session: dict, messages: list) -> str:
        """Export session as Markdown.

        Args:
            session: Session metadata
            messages: List of messages

        Returns:
            Markdown formatted content
        """
        lines = []

        # Header
        lines.append("# Claude Code Session Export")
        lines.append(f"\n**Session ID:** `{session['id']}`")
        lines.append(f"**Created:** {session['created_at']}")
        if session.get("updated_at"):
            lines.append(f"**Last Updated:** {session['updated_at']}")
        lines.append(f"**Message Count:** {len(messages)}")
        lines.append("\n---\n")

        # Messages
        for msg in messages:
            timestamp = msg["created_at"]
            role = "You" if msg["role"] == "user" else "Claude"
            content = msg["content"]

            lines.append(f"### {role} - {timestamp}")
            lines.append(f"\n{content}\n")
            lines.append("---\n")

        return "\n".join(lines)

    async def _export_json(self, session: dict, messages: list) -> str:
        """Export session as JSON.

        Args:
            session: Session metadata
            messages: List of messages

        Returns:
            JSON formatted content
        """
        export_data = {
            "session": {
                "id": session["id"],
                "user_id": session["user_id"],
                "created_at": session["created_at"].isoformat(),
                "updated_at": (
                    session.get("updated_at", "").isoformat()
                    if session.get("updated_at")
                    else None
                ),
                "message_count": len(messages),
            },
            "messages": [
                {
                    "id": msg["id"],
                    "role": msg["role"],
                    "content": msg["content"],
                    "created_at": msg["created_at"].isoformat(),
                }
                for msg in messages
            ],
        }

        return json.dumps(export_data, indent=2, ensure_ascii=False)

    async def _export_html(self, session: dict, messages: list) -> str:
        """Export session as a chat transcript HTML page."""
        import html as html_mod
        import re

        sid = html_mod.escape(str(session["id"]))
        created = html_mod.escape(str(session["created_at"]))
        updated = html_mod.escape(str(session.get("updated_at", "")))
        msg_count = len(messages)

        def _render_content(text: str) -> str:
            """Escape HTML then apply lightweight markdown-ish formatting."""
            t = html_mod.escape(text)
            # Fenced code blocks ```lang\n...\n```
            t = re.sub(
                r"```(\w*)\n(.*?)```",
                lambda m: (
                    f'<pre><code class="lang-{m.group(1)}">'
                    f"{m.group(2)}</code></pre>"
                ),
                t,
                flags=re.DOTALL,
            )
            # Inline code `...`
            t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
            # Bold **...**
            t = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", t)
            # Line breaks
            t = t.replace("\n", "<br>")
            return t

        msg_html_parts: list[str] = []
        for msg in messages:
            role = msg["role"]
            content = _render_content(msg["content"])
            ts = html_mod.escape(str(msg["created_at"]))
            cls = "user" if role == "user" else "assistant"
            label = "You" if role == "user" else "Claude"
            msg_html_parts.append(
                f'<div class="msg {cls}">'
                f'<div class="msg-head"><span class="role">{label}</span>'
                f'<span class="ts">{ts}</span></div>'
                f'<div class="msg-body">{content}</div></div>'
            )

        messages_html = "\n".join(msg_html_parts)

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Session {sid[:8]}</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&display=swap');
*,:after,:before{{box-sizing:border-box;margin:0;padding:0}}
:root{{
  --bg:#1a1b1e;--surface:#222326;--surface2:#2a2b2f;
  --border:#363739;--text:#c9ccd1;--text-dim:#76787b;
  --user-accent:#e0a526;--user-bg:#2a261f;
  --asst-accent:#3d9eaa;--asst-bg:#1e2a2d;
  --code-bg:#18191c;
}}
html{{font-size:15px}}
body{{
  font-family:'IBM Plex Sans',-apple-system,sans-serif;
  background:var(--bg);color:var(--text);
  line-height:1.65;min-height:100vh;
}}
.wrap{{max-width:820px;margin:0 auto;padding:32px 20px 64px}}
/* header */
.hdr{{
  margin-bottom:32px;padding-bottom:24px;
  border-bottom:1px solid var(--border);
}}
.hdr h1{{
  font-family:'IBM Plex Mono',monospace;font-size:1.1rem;font-weight:500;
  color:#e4e5e7;letter-spacing:-.01em;margin-bottom:12px;
}}
.meta{{display:flex;flex-wrap:wrap;gap:8px 20px}}
.meta-item{{
  font-size:.78rem;color:var(--text-dim);
  font-family:'IBM Plex Mono',monospace;
}}
.meta-item span{{color:var(--text)}}
/* messages */
.msg{{margin-bottom:2px;padding:16px 20px;border-radius:6px}}
.msg.user{{background:var(--user-bg)}}
.msg.assistant{{background:var(--asst-bg)}}
.msg-head{{
  display:flex;align-items:center;gap:10px;margin-bottom:8px;
}}
.role{{
  font-size:.75rem;font-weight:600;text-transform:uppercase;
  letter-spacing:.06em;
}}
.msg.user .role{{color:var(--user-accent)}}
.msg.assistant .role{{color:var(--asst-accent)}}
.ts{{font-size:.7rem;color:var(--text-dim);font-family:'IBM Plex Mono',monospace}}
.msg-body{{font-size:.92rem;line-height:1.7}}
.msg-body code{{
  font-family:'IBM Plex Mono',monospace;font-size:.84rem;
  background:var(--code-bg);padding:1px 5px;border-radius:3px;
}}
.msg-body pre{{
  background:var(--code-bg);padding:14px 16px;border-radius:5px;
  overflow-x:auto;margin:10px 0;
}}
.msg-body pre code{{background:none;padding:0;font-size:.82rem}}
.msg-body strong{{font-weight:600;color:#e4e5e7}}
/* footer */
.foot{{
  margin-top:40px;padding-top:20px;
  border-top:1px solid var(--border);
  font-size:.72rem;color:var(--text-dim);
  font-family:'IBM Plex Mono',monospace;
  text-align:center;
}}
</style>
</head>
<body>
<div class="wrap">
  <div class="hdr">
    <h1>Session {sid}</h1>
    <div class="meta">
      <div class="meta-item">Created <span>{created}</span></div>
      {"<div class='meta-item'>Updated <span>" + updated + "</span></div>" if updated else ""}
      <div class="meta-item">Messages <span>{msg_count}</span></div>
    </div>
  </div>
  {messages_html}
  <div class="foot">exported by claude-code-telegram</div>
</div>
</body>
</html>"""
