"""Session browser logic for the /sessions Telegram command.

Pure logic only — no Telegram side-effects, no SDK calls.
"""

import re
from typing import Optional


_COMMAND_WRAPPER_RE = re.compile(
    r"<command-(message|name)>.*?</command-\1>",
    flags=re.DOTALL | re.IGNORECASE,
)
_FALLBACK_MAX_CHARS = 60


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
