"""Shared types for /btw side questions."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class BtwContextSnapshot:
    """In-memory runtime context copied from an active Telegram request."""

    session_id: Optional[str]
    working_directory: Path
    original_prompt: str
    elapsed_seconds: float
    last_status: str
    current_tool: Optional[str] = None
    recent_tools: List[Dict[str, Any]] = field(default_factory=list)
    last_assistant_text: str = ""
    recent_stream_text: str = ""


@dataclass
class BtwResponse:
    """Result of a /btw side question."""

    content: str
    fork_session_id: Optional[str] = None
    used_runtime_snapshot: bool = False
