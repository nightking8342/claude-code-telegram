"""Claude Code integration module."""

from .exceptions import (
    ClaudeError,
    ClaudeParsingError,
    ClaudeProcessError,
    ClaudeSessionError,
    ClaudeTimeoutError,
)
from .facade import ClaudeIntegration
from .btw import BtwContextSnapshot, BtwResponse
from .sdk_integration import ClaudeResponse, ClaudeSDKManager, StreamUpdate
from .skills import SkillInfo, discover_skills
from .session import (
    ClaudeSession,
    SessionManager,
    SessionStorage,
)

__all__ = [
    # Exceptions
    "ClaudeError",
    "ClaudeParsingError",
    "ClaudeProcessError",
    "ClaudeSessionError",
    "ClaudeTimeoutError",
    # Main integration
    "ClaudeIntegration",
    "BtwContextSnapshot",
    "BtwResponse",
    # Core components
    "ClaudeSDKManager",
    "ClaudeResponse",
    "StreamUpdate",
    "SessionManager",
    "SessionStorage",
    "ClaudeSession",
    # Skills discovery
    "SkillInfo",
    "discover_skills",
]
