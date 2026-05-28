"""High-level Claude Code integration facade.

Provides simple interface for bot handlers.
"""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import structlog

from ..config.settings import Settings
from .sdk_integration import ClaudeResponse, ClaudeSDKManager, StreamUpdate
from .session import SessionManager

logger = structlog.get_logger()


class ClaudeIntegration:
    """Main integration point for Claude Code."""

    def __init__(
        self,
        config: Settings,
        sdk_manager: Optional[ClaudeSDKManager] = None,
        session_manager: Optional[SessionManager] = None,
        provider_manager: Optional[Any] = None,
    ):
        """Initialize Claude integration facade."""
        self.config = config
        self.sdk_manager = sdk_manager or ClaudeSDKManager(
            config, provider_manager=provider_manager
        )
        self.session_manager = session_manager

    async def run_command(
        self,
        prompt: str,
        working_directory: Path,
        user_id: int,
        session_id: Optional[str] = None,
        on_stream: Optional[Callable[[StreamUpdate], None]] = None,
        force_new: bool = False,
        interrupt_event: Optional["asyncio.Event"] = None,
        images: Optional[List[Dict[str, str]]] = None,
        hooks: Optional[Dict[str, Any]] = None,
        permission_mode: Optional[str] = None,
    ) -> ClaudeResponse:
        """Run Claude Code command with full integration."""
        logger.info(
            "Running Claude command",
            user_id=user_id,
            working_directory=str(working_directory),
            session_id=session_id,
            prompt_length=len(prompt),
            force_new=force_new,
        )

        # If no session_id provided, try to find an existing session for this
        # user+directory combination (auto-resume).
        # Skip auto-resume when force_new is set (e.g. after /new command).
        if not session_id and not force_new:
            existing_session = await self._find_resumable_session(
                user_id, working_directory
            )
            if existing_session:
                session_id = existing_session.session_id
                logger.info(
                    "Auto-resuming existing session for project",
                    session_id=session_id,
                    project_path=str(working_directory),
                    user_id=user_id,
                )

        # Get or create session
        session = await self.session_manager.get_or_create_session(
            user_id, working_directory, session_id
        )

        # Execute command
        try:
            # Continue session if we have an existing session with a real ID
            is_new = getattr(session, "is_new_session", False)
            should_continue = not is_new and bool(session.session_id)

            # For new sessions, don't pass session_id to Claude Code
            claude_session_id = session.session_id if should_continue else None

            try:
                response = await self._execute(
                    prompt=prompt,
                    working_directory=working_directory,
                    session_id=claude_session_id,
                    continue_session=should_continue,
                    stream_callback=on_stream,
                    interrupt_event=interrupt_event,
                    images=images,
                    hooks=hooks,
                    permission_mode=permission_mode,
                )
            except Exception as resume_error:
                # If resume failed (e.g., session expired/missing on Claude's side),
                # retry as a fresh session.  The CLI returns a generic exit-code-1
                # when the session is gone, so we catch *any* error during resume.
                if should_continue:
                    logger.warning(
                        "Session resume failed, starting fresh session",
                        failed_session_id=claude_session_id,
                        error=str(resume_error),
                    )
                    # Clean up the stale session
                    await self.session_manager.remove_session(session.session_id)

                    # Create a fresh session and retry
                    session = await self.session_manager.get_or_create_session(
                        user_id, working_directory
                    )
                    response = await self._execute(
                        prompt=prompt,
                        working_directory=working_directory,
                        session_id=None,
                        continue_session=False,
                        stream_callback=on_stream,
                        interrupt_event=interrupt_event,
                        images=images,
                        hooks=hooks,
                        permission_mode=permission_mode,
                    )
                else:
                    raise

            # Update session (assigns real session_id for new sessions)
            await self.session_manager.update_session(session, response)

            # Ensure response has the session's final ID
            response.session_id = session.session_id

            if not response.session_id:
                logger.warning(
                    "No session_id after execution; session cannot be resumed",
                    user_id=user_id,
                )

            logger.info(
                "Claude command completed",
                session_id=response.session_id,
                cost=response.cost,
                duration_ms=response.duration_ms,
                num_turns=response.num_turns,
                is_error=response.is_error,
            )

            return response

        except Exception as e:
            logger.error(
                "Claude command failed",
                error=str(e),
                user_id=user_id,
                session_id=session.session_id,
            )
            raise

    async def run_btw(
        self,
        question: str,
        working_directory: Path,
        user_id: int,
        session_id: str,
    ) -> str:
        """Run a /btw side question. Returns the answer text."""
        logger.info(
            "Running /btw",
            user_id=user_id,
            session_id=session_id,
            question_length=len(question),
        )

        # If no session_id provided, look up the most recent session
        if not session_id and self.session_manager:
            resumable = await self._find_resumable_session(user_id, working_directory)
            if resumable:
                session_id = resumable.session_id
                logger.info(
                    "Found resumable session for /btw",
                    session_id=session_id,
                )

        if not session_id:
            return ""

        return await self.sdk_manager.execute_btw(
            question=question,
            working_directory=working_directory,
            session_id=session_id,
        )

    async def _execute(
        self,
        prompt: str,
        working_directory: Path,
        session_id: Optional[str] = None,
        continue_session: bool = False,
        stream_callback: Optional[Callable] = None,
        interrupt_event: Optional[asyncio.Event] = None,
        images: Optional[List[Dict[str, str]]] = None,
        hooks: Optional[Dict[str, Any]] = None,
        permission_mode: Optional[str] = None,
    ) -> ClaudeResponse:
        """Execute command via SDK."""
        return await self.sdk_manager.execute_command(
            prompt=prompt,
            working_directory=working_directory,
            session_id=session_id,
            continue_session=continue_session,
            stream_callback=stream_callback,
            interrupt_event=interrupt_event,
            images=images,
            hooks=hooks,
            permission_mode=permission_mode,
        )

    async def _find_resumable_session(
        self,
        user_id: int,
        working_directory: Path,
    ) -> Optional["ClaudeSession"]:  # noqa: F821
        """Find the most recent resumable session for a user in a directory.

        Returns the session if one exists that is non-expired and has a real
        (non-temporary) session ID from Claude. Returns None otherwise.
        """

        sessions = await self.session_manager._get_user_sessions(user_id)

        matching_sessions = [
            s
            for s in sessions
            if s.project_path == working_directory
            and bool(s.session_id)
            and not s.is_expired(self.config.session_timeout_hours)
        ]

        if not matching_sessions:
            return None

        return max(matching_sessions, key=lambda s: s.last_used)

    # ------------------------------------------------------------------ #
    #  Transcript title helpers                                           #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _encode_project_path(project_path: Path) -> str:
        """Encode a filesystem path for Claude's transcript directory.

        Claude Code stores transcripts under
        ``~/.claude/projects/{encoded_path}/`` where the encoding replaces
        the drive-colon-followed-by-separator with ``--`` and remaining
        separators with ``-``.

        Examples::

            D:\\claudebot\\project       ->  D--claudebot-project
            /home/user/project          ->  -home-user-project
        """
        return (
            str(project_path)
            .replace(":\\", "--")
            .replace(":/", "--")
            .replace("\\", "-")
            .replace("/", "-")
        )

    @staticmethod
    async def read_session_title(session_id: str, project_path: Path) -> Optional[str]:
        """Read the AI-generated title from Claude's transcript JSONL.

        Scans the file in reverse to find the *last* ``ai-title`` entry
        (titles can be updated during a conversation).  Returns ``None``
        when the file is missing, empty, or contains no title.
        """
        import os

        encoded = ClaudeIntegration._encode_project_path(project_path)
        home = Path(os.path.expanduser("~"))
        jsonl_path = home / ".claude" / "projects" / encoded / f"{session_id}.jsonl"

        if not jsonl_path.is_file():
            return None

        try:
            # Read in reverse to find the last ai-title quickly.
            # For files up to a few MB this is fine; read all lines and
            # scan backwards.  The ai-title entry is usually near the top
            # but may be updated later, so we need the *last* occurrence.
            loop = asyncio.get_running_loop()
            title: Optional[str] = None

            def _read() -> Optional[str]:
                result: Optional[str] = None
                with open(jsonl_path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                            if obj.get("type") == "ai-title":
                                ai_title = obj.get("aiTitle")
                                if ai_title:
                                    result = ai_title
                        except (json.JSONDecodeError, KeyError):
                            continue
                return result

            title = await loop.run_in_executor(None, _read)
            return title
        except Exception:
            logger.debug(
                "Failed to read session title",
                session_id=session_id,
                path=str(jsonl_path),
            )
            return None

    @staticmethod
    async def scan_cli_sessions(project_path: Path) -> list[dict]:
        """Scan Claude CLI transcript files for sessions in *project_path*.

        Returns a list of dicts with keys: ``session_id``, ``ai_title``,
        ``created_at`` (datetime|None), ``message_count``, ``last_used``
        (datetime from file mtime).
        """
        import os

        encoded = ClaudeIntegration._encode_project_path(project_path)
        home = Path(os.path.expanduser("~"))
        proj_dir = home / ".claude" / "projects" / encoded

        if not proj_dir.is_dir():
            return []

        loop = asyncio.get_running_loop()

        def _scan() -> list[dict]:
            results: list[dict] = []
            for jsonl_path in proj_dir.glob("*.jsonl"):
                if jsonl_path.stat().st_size < 10:
                    continue
                session_id = jsonl_path.stem
                ai_title: Optional[str] = None
                created_at = None
                message_count = 0
                is_cli = False
                entrypoint_checked = False
                try:
                    with open(jsonl_path, encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                obj = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            # entrypoint is on the first (system) entry;
                            # skip bot-created sessions (sdk-py / sdk-cli).
                            if not entrypoint_checked:
                                entrypoint_checked = True
                                ep = obj.get("entrypoint")
                                if ep is not None and ep != "cli":
                                    break
                                is_cli = True
                            obj_type = obj.get("type")
                            if obj_type == "ai-title":
                                t = obj.get("aiTitle")
                                if t:
                                    ai_title = t
                            elif obj_type == "user":
                                message_count += 1
                            if created_at is None and "timestamp" in obj:
                                ts = obj["timestamp"]
                                if isinstance(ts, str):
                                    try:
                                        created_at = datetime.fromisoformat(
                                            ts.replace("Z", "+00:00")
                                        )
                                    except ValueError:
                                        pass
                    if not is_cli:
                        continue
                except Exception:
                    logger.debug(
                        "Failed to scan CLI session",
                        path=str(jsonl_path),
                    )
                    continue
                mtime = datetime.fromtimestamp(jsonl_path.stat().st_mtime, tz=UTC)
                results.append(
                    {
                        "session_id": session_id,
                        "ai_title": ai_title,
                        "created_at": created_at,
                        "message_count": message_count,
                        "last_used": mtime,
                    }
                )
            return results

        return await loop.run_in_executor(None, _scan)

    async def continue_session(
        self,
        user_id: int,
        working_directory: Path,
        prompt: Optional[str] = None,
        on_stream: Optional[Callable[[StreamUpdate], None]] = None,
    ) -> Optional[ClaudeResponse]:
        """Continue the most recent session."""
        logger.info(
            "Continuing session",
            user_id=user_id,
            working_directory=str(working_directory),
            has_prompt=bool(prompt),
        )

        # Get user's sessions
        sessions = await self.session_manager._get_user_sessions(user_id)

        # Find most recent session in this directory (exclude sessions without IDs)
        matching_sessions = [
            s
            for s in sessions
            if s.project_path == working_directory and bool(s.session_id)
        ]

        if not matching_sessions:
            logger.info("No matching sessions found", user_id=user_id)
            return None

        # Get most recent
        latest_session = max(matching_sessions, key=lambda s: s.last_used)

        # Continue session with default prompt if none provided
        # Claude CLI requires a prompt, so we use a placeholder
        return await self.run_command(
            prompt=prompt or "Please continue where we left off",
            working_directory=working_directory,
            user_id=user_id,
            session_id=latest_session.session_id,
            on_stream=on_stream,
        )

    async def get_session_info(
        self, session_id: str, user_id: int
    ) -> Optional[Dict[str, Any]]:
        """Get session information (scoped to requesting user)."""
        return await self.session_manager.get_session_info(session_id, user_id)

    async def get_user_sessions(self, user_id: int) -> List[Dict[str, Any]]:
        """Get all sessions for a user."""
        sessions = await self.session_manager._get_user_sessions(user_id)
        return [
            {
                "session_id": s.session_id,
                "project_path": str(s.project_path),
                "created_at": s.created_at.isoformat(),
                "last_used": s.last_used.isoformat(),
                "total_cost": s.total_cost,
                "message_count": s.message_count,
                "tools_used": s.tools_used,
                "expired": s.is_expired(self.config.session_timeout_hours),
            }
            for s in sessions
        ]

    async def cleanup_expired_sessions(self) -> int:
        """Clean up expired sessions."""
        return await self.session_manager.cleanup_expired_sessions()

    async def get_user_summary(self, user_id: int) -> Dict[str, Any]:
        """Get comprehensive user summary."""
        session_summary = await self.session_manager.get_user_session_summary(user_id)

        return {
            "user_id": user_id,
            **session_summary,
        }

    async def shutdown(self) -> None:
        """Shutdown integration and cleanup resources."""
        logger.info("Shutting down Claude integration")

        await self.cleanup_expired_sessions()

        logger.info("Claude integration shutdown complete")
