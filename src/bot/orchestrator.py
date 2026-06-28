"""Message orchestrator — single entry point for all Telegram updates.

Routes messages based on agentic vs classic mode. In agentic mode, provides
a minimal conversational interface (3 commands, no inline keyboards). In
classic mode, delegates to existing full-featured handlers.
"""

import asyncio
import html
import os
import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import structlog
from telegram import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ..claude.btw import BtwContextSnapshot
from ..claude.facade import ClaudeIntegration
from ..claude.sdk_integration import StreamUpdate
from ..config.settings import Settings
from ..projects import PrivateTopicsUnavailableError
from .utils.draft_streamer import DraftStreamer, generate_draft_id
from .utils.html_format import escape_html
from .utils.image_extractor import (
    ImageAttachment,
    should_send_as_photo,
    validate_image_path,
)
from ..config.providers import _VALID_ROLES, _parse_context_suffix

logger = structlog.get_logger()

_BTW_COMMAND_TEXT_RE = re.compile(
    r"^/btw(?:@[A-Za-z0-9_]+)?(?P<body>$|\s.*|[^A-Za-z0-9_].*)",
    re.IGNORECASE,
)

_MEDIA_TYPE_MAP = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}

_HOOK_TIMEOUT_SDK_GRACE_SECONDS = 30

# Patterns that look like secrets/credentials in CLI arguments
_SECRET_PATTERNS: List[re.Pattern[str]] = [
    # API keys / tokens (sk-ant-..., sk-..., ghp_..., gho_..., github_pat_..., xoxb-...)
    re.compile(
        r"(sk-ant-api\d*-[A-Za-z0-9_-]{10})[A-Za-z0-9_-]*"
        r"|(sk-[A-Za-z0-9_-]{20})[A-Za-z0-9_-]*"
        r"|(ghp_[A-Za-z0-9]{5})[A-Za-z0-9]*"
        r"|(gho_[A-Za-z0-9]{5})[A-Za-z0-9]*"
        r"|(github_pat_[A-Za-z0-9_]{5})[A-Za-z0-9_]*"
        r"|(xoxb-[A-Za-z0-9]{5})[A-Za-z0-9-]*"
    ),
    # AWS access keys
    re.compile(r"(AKIA[0-9A-Z]{4})[0-9A-Z]{12}"),
    # Generic long hex/base64 tokens after common flags/env patterns
    re.compile(
        r"((?:--token|--secret|--password|--api-key|--apikey|--auth)"
        r"[= ]+)['\"]?[A-Za-z0-9+/_.:-]{8,}['\"]?"
    ),
    # Inline env assignments like KEY=value
    re.compile(
        r"((?:TOKEN|SECRET|PASSWORD|API_KEY|APIKEY|AUTH_TOKEN|PRIVATE_KEY"
        r"|ACCESS_KEY|CLIENT_SECRET|WEBHOOK_SECRET)"
        r"=)['\"]?[^\s'\"]{8,}['\"]?"
    ),
    # Bearer / Basic auth headers
    re.compile(r"(Bearer )[A-Za-z0-9+/_.:-]{8,}" r"|(Basic )[A-Za-z0-9+/=]{8,}"),
    # Connection strings with credentials  user:pass@host
    re.compile(r"://([^:]+:)[^@]{4,}(@)"),
]


def _redact_secrets(text: str) -> str:
    """Replace likely secrets/credentials with redacted placeholders."""
    result = text
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub(
            lambda m: next((g + "***" for g in m.groups() if g is not None), "***"),
            result,
        )
    return result


# Tool name -> friendly emoji mapping for verbose output
_TOOL_ICONS: Dict[str, str] = {
    "Read": "\U0001f4d6",
    "Write": "\u270f\ufe0f",
    "Edit": "\u270f\ufe0f",
    "MultiEdit": "\u270f\ufe0f",
    "Bash": "\U0001f4bb",
    "Glob": "\U0001f50d",
    "Grep": "\U0001f50d",
    "LS": "\U0001f4c2",
    "Task": "\U0001f9e0",
    "TaskOutput": "\U0001f9e0",
    "WebFetch": "\U0001f310",
    "WebSearch": "\U0001f310",
    "NotebookRead": "\U0001f4d3",
    "NotebookEdit": "\U0001f4d3",
    "TodoRead": "\u2611\ufe0f",
    "TodoWrite": "\u2611\ufe0f",
}


def _tool_icon(name: str) -> str:
    """Return emoji for a tool, with a default wrench."""
    return _TOOL_ICONS.get(name, "\U0001f527")


@dataclass
class ActiveRequest:
    """Tracks an in-flight Claude request so it can be interrupted."""

    user_id: int
    session_id: Optional[str] = None
    working_directory: Optional[Path] = None
    original_prompt: str = ""
    started_at: float = field(default_factory=time.time)
    interrupt_event: asyncio.Event = field(default_factory=asyncio.Event)
    interrupted: bool = False
    progress_msg: Any = None  # telegram Message object
    progress_messages: List[Any] = field(default_factory=list)
    tool_log: List[Dict[str, Any]] = field(default_factory=list)
    last_status: str = "Starting"
    current_tool: Optional[str] = None
    last_assistant_text: str = ""
    recent_stream_text: str = ""
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def __post_init__(self) -> None:
        if self.progress_msg is not None:
            self.progress_messages.append(self.progress_msg)

    async def switch_progress_msg(self, progress_msg: Any) -> None:
        """Point subsequent stream edits at a new Telegram progress message."""
        async with self.lock:
            self.progress_msg = progress_msg
            if progress_msg is not None and progress_msg not in self.progress_messages:
                self.progress_messages.append(progress_msg)

    async def current_progress_msg(self) -> Any:
        async with self.lock:
            return self.progress_msg

    async def all_progress_messages(self) -> List[Any]:
        async with self.lock:
            return list(self.progress_messages)

    async def apply_stream_update(self, update_obj: StreamUpdate) -> None:
        """Update the in-memory /btw runtime snapshot from a stream event."""
        async with self.lock:
            metadata = update_obj.metadata or {}
            session_id = metadata.get("session_id")
            if isinstance(session_id, str) and session_id:
                self.session_id = session_id

            if update_obj.tool_calls:
                for tc in update_obj.tool_calls:
                    name = str(tc.get("name") or "unknown")
                    detail = str(tc.get("detail") or "")
                    self.current_tool = name
                    self.tool_log.append(
                        {"kind": "tool", "name": name, "detail": detail}
                    )
                self.tool_log = self.tool_log[-20:]
                self.last_status = f"Using {self.current_tool}"

            if update_obj.type == "assistant" and update_obj.content:
                text = update_obj.content.strip()
                if text:
                    self.last_assistant_text = text[:1000]
                    first_line = text.split("\n", 1)[0].strip()
                    if first_line:
                        self.tool_log.append(
                            {"kind": "text", "detail": first_line[:160]}
                        )
                        self.tool_log = self.tool_log[-20:]
                    self.last_status = "Thinking"

            if update_obj.type == "stream_delta" and update_obj.content:
                self.recent_stream_text = (
                    self.recent_stream_text + update_obj.content
                )[-2000:]
                if self.current_tool is None:
                    self.last_status = "Responding"

            if update_obj.is_error():
                self.last_status = f"Error: {update_obj.get_error_message()[:160]}"

    async def snapshot(self) -> BtwContextSnapshot:
        """Copy the current runtime state for a /btw side question."""
        async with self.lock:
            return BtwContextSnapshot(
                session_id=self.session_id,
                working_directory=self.working_directory or Path.cwd(),
                original_prompt=self.original_prompt,
                elapsed_seconds=max(0.0, time.time() - self.started_at),
                last_status=self.last_status,
                current_tool=self.current_tool,
                recent_tools=list(self.tool_log[-10:]),
                last_assistant_text=self.last_assistant_text,
                recent_stream_text=self.recent_stream_text,
            )


class MessageOrchestrator:
    """Routes messages based on mode. Single entry point for all Telegram updates."""

    def __init__(self, settings: Settings, deps: Dict[str, Any]):
        self.settings = settings
        self.deps = deps
        self._active_requests: Dict[int, ActiveRequest] = {}
        self._pending_auq: Dict[str, asyncio.Future] = {}
        self._pending_plan: Dict[int, asyncio.Future] = {}  # user_id -> Future
        self._pending_plan_waiting: Dict[int, Dict[str, Any]] = (
            {}
        )  # user_id -> feedback state
        # user_id -> {"tool_use_id": str, "tid_short": str, "question_text": str}
        # Metadata for "Other" free-text answers; the lock-bypass state lives
        # in StopAwareUpdateProcessor.auq_other_waiting (class-level set).
        self._auq_waiting_other: Dict[int, Dict[str, str]] = {}
        self._model_panel_state: Dict[int, Dict[str, Any]] = {}
        self._known_commands: frozenset[str] = frozenset()

    def _inject_deps(self, handler: Callable) -> Callable:  # type: ignore[type-arg]
        """Wrap handler to inject dependencies into context.bot_data."""

        async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            for key, value in self.deps.items():
                context.bot_data[key] = value
            context.bot_data["settings"] = self.settings
            context.user_data.pop("_thread_context", None)

            is_sync_bypass = handler.__name__ == "sync_threads"
            is_start_bypass = handler.__name__ in {"start_command", "agentic_start"}
            message_thread_id = self._extract_message_thread_id(update)
            should_enforce = self.settings.enable_project_threads

            if should_enforce:
                if self.settings.project_threads_mode == "private":
                    should_enforce = not is_sync_bypass and not (
                        is_start_bypass and message_thread_id is None
                    )
                else:
                    should_enforce = not is_sync_bypass

            if should_enforce:
                allowed = await self._apply_thread_routing_context(update, context)
                if not allowed:
                    return

            try:
                await handler(update, context)
            finally:
                if should_enforce:
                    self._persist_thread_state(context)

        return wrapped

    async def _apply_thread_routing_context(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> bool:
        """Enforce strict project-thread routing and load thread-local state."""
        manager = context.bot_data.get("project_threads_manager")
        if manager is None:
            await self._reject_for_thread_mode(
                update,
                "❌ <b>项目话题模式配置错误</b>\n\n" "话题管理器未初始化。",
            )
            return False

        chat = update.effective_chat
        message = update.effective_message
        if not chat or not message:
            return False

        if self.settings.project_threads_mode == "group":
            if chat.id != self.settings.project_threads_chat_id:
                await self._reject_for_thread_mode(
                    update,
                    manager.guidance_message(mode=self.settings.project_threads_mode),
                )
                return False
        else:
            if getattr(chat, "type", "") != "private":
                await self._reject_for_thread_mode(
                    update,
                    manager.guidance_message(mode=self.settings.project_threads_mode),
                )
                return False

        message_thread_id = self._extract_message_thread_id(update)
        if not message_thread_id:
            await self._reject_for_thread_mode(
                update,
                manager.guidance_message(mode=self.settings.project_threads_mode),
            )
            return False

        project = await manager.resolve_project(chat.id, message_thread_id)
        if not project:
            await self._reject_for_thread_mode(
                update,
                manager.guidance_message(mode=self.settings.project_threads_mode),
            )
            return False

        state_key = f"{chat.id}:{message_thread_id}"
        thread_states = context.user_data.setdefault("thread_state", {})
        state = thread_states.get(state_key, {})

        project_root = project.absolute_path
        current_dir_raw = state.get("current_directory")
        current_dir = (
            Path(current_dir_raw).resolve() if current_dir_raw else project_root
        )
        if not self._is_within(current_dir, project_root) or not current_dir.is_dir():
            current_dir = project_root

        restored_session_id = state.get("claude_session_id")
        context.user_data["current_directory"] = current_dir
        context.user_data["claude_session_id"] = restored_session_id
        context.user_data["_thread_context"] = {
            "chat_id": chat.id,
            "message_thread_id": message_thread_id,
            "state_key": state_key,
            "project_slug": project.slug,
            "project_root": str(project_root),
            "project_name": project.name,
        }

        # Show resume notification if restoring a session for this topic
        if restored_session_id:
            claude_integration = context.bot_data.get("claude_integration")
            if claude_integration:
                session_meta = None
                try:
                    session_meta = (
                        await claude_integration.session_manager.get_or_create_session(
                            update.effective_user.id,
                            current_dir,
                            restored_session_id,
                        )
                    )
                    if getattr(session_meta, "is_new_session", False):
                        session_meta = None
                except Exception:
                    pass
                resume_text = await self._build_session_resume_text(
                    restored_session_id,
                    current_dir,
                    claude_integration,
                    session_meta=session_meta,
                )
                await update.message.reply_text(resume_text)

        return True

    def _persist_thread_state(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Persist compatibility keys back into per-thread state."""
        thread_context = context.user_data.get("_thread_context")
        if not thread_context:
            return

        project_root = Path(thread_context["project_root"])
        current_dir = context.user_data.get("current_directory", project_root)
        if not isinstance(current_dir, Path):
            current_dir = Path(str(current_dir))
        current_dir = current_dir.resolve()
        if not self._is_within(current_dir, project_root) or not current_dir.is_dir():
            current_dir = project_root

        thread_states = context.user_data.setdefault("thread_state", {})
        thread_states[thread_context["state_key"]] = {
            "current_directory": str(current_dir),
            "claude_session_id": context.user_data.get("claude_session_id"),
            "project_slug": thread_context["project_slug"],
        }

    @staticmethod
    def _is_within(path: Path, root: Path) -> bool:
        """Return True if path is within root."""
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    @staticmethod
    def _extract_message_thread_id(update: Update) -> Optional[int]:
        """Extract topic/thread id from update message for forum/direct topics."""
        message = update.effective_message
        if not message:
            return None
        message_thread_id = getattr(message, "message_thread_id", None)
        if isinstance(message_thread_id, int) and message_thread_id > 0:
            return message_thread_id
        dm_topic = getattr(message, "direct_messages_topic", None)
        topic_id = getattr(dm_topic, "topic_id", None) if dm_topic else None
        if isinstance(topic_id, int) and topic_id > 0:
            return topic_id
        # Telegram omits message_thread_id for the General topic in forum
        # supergroups; its canonical thread ID is 1.
        chat = update.effective_chat
        if chat and getattr(chat, "is_forum", False):
            return 1
        return None

    async def _reject_for_thread_mode(self, update: Update, message: str) -> None:
        """Send a guidance response when strict thread routing rejects an update."""
        query = update.callback_query
        if query:
            try:
                await query.answer()
            except Exception:
                pass
            if query.message:
                await query.message.reply_text(message, parse_mode="HTML")
            return

        if update.effective_message:
            await update.effective_message.reply_text(message, parse_mode="HTML")

    def register_handlers(self, app: Application) -> None:
        """Register handlers based on mode."""
        if self.settings.agentic_mode:
            self._register_agentic_handlers(app)
        else:
            self._register_classic_handlers(app)

    def _register_agentic_handlers(self, app: Application) -> None:
        """Register agentic handlers: commands + text/file/photo."""
        from .handlers import command

        # Commands
        handlers = [
            ("start", self.agentic_start),
            ("new", self.agentic_new),
            ("status", self.agentic_status),
            ("verbose", self.agentic_verbose),
            ("rich", self.agentic_rich),
            ("plan", self.agentic_plan),
            ("repo", self.agentic_repo),
            ("provider", self.agentic_provider),
            ("model", self.agentic_model),
            ("sessions", command.sessions_command),
            ("restart", command.restart_command),
            ("skill", self.agentic_skill),
            ("btw", self._handle_btw),
        ]
        if self.settings.enable_project_threads:
            handlers.append(("sync_threads", command.sync_threads))

        # Derive known commands dynamically — avoids drift when new commands are added
        self._known_commands: frozenset[str] = frozenset(cmd for cmd, _ in handlers)

        # Regex fallback for /btw. CommandHandler depends on Telegram command
        # entities; this catches plain text forms while staying in group 0.
        app.add_handler(
            MessageHandler(
                filters.TEXT & filters.Regex(_BTW_COMMAND_TEXT_RE),
                self._inject_deps(self._handle_btw),
            )
        )

        for cmd, handler in handlers:
            app.add_handler(CommandHandler(cmd, self._inject_deps(handler)))

        # Text messages -> Claude
        app.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                self._inject_deps(self.agentic_text),
            ),
            group=10,
        )

        # Unknown slash commands -> Claude (passthrough in agentic mode).
        # Registered commands are handled by CommandHandlers in group 0
        # (higher priority). This catches any /command not matched there
        # and forwards it to Claude, while skipping known commands to
        # avoid double-firing.
        app.add_handler(
            MessageHandler(
                filters.COMMAND,
                self._inject_deps(self._handle_unknown_command),
            ),
            group=10,
        )

        # File uploads -> Claude
        app.add_handler(
            MessageHandler(
                filters.Document.ALL, self._inject_deps(self.agentic_document)
            ),
            group=10,
        )

        # Photo uploads -> Claude
        app.add_handler(
            MessageHandler(filters.PHOTO, self._inject_deps(self.agentic_photo)),
            group=10,
        )

        # Voice messages -> transcribe -> Claude
        app.add_handler(
            MessageHandler(filters.VOICE, self._inject_deps(self.agentic_voice)),
            group=10,
        )

        # Stop button callback (must be before cd: handler)
        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(self._handle_stop_callback),
                pattern=r"^stop:",
            )
        )

        # AskUserQuestion button callbacks
        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(self._handle_auq_callback),
                pattern=r"^auq:",
            )
        )

        # Plan mode (EnterPlanMode) button callbacks
        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(self._handle_plan_callback),
                pattern=r"^plan:",
            )
        )

        # Only cd: callbacks (for project selection), scoped by pattern
        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(self._agentic_callback),
                pattern=r"^cd:",
            )
        )

        # Recursive repo browser callbacks
        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(self._handle_repo_browse_cb),
                pattern=r"^repo:",
            )
        )
        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(self._handle_repo_use_cb),
                pattern=r"^repo_use:",
            )
        )
        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(self._handle_repo_back_cb),
                pattern=r"^repo_back:",
            )
        )

        # Provider switch buttons
        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(self._handle_provider_callback),
                pattern=r"^provider:",
            )
        )

        # Model panel button callbacks
        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(self._handle_model_callback),
                pattern=r"^model:",
            )
        )

        # Sessions browser callbacks
        from .handlers import callback

        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(callback.handle_callback_query),
                pattern=r"^sessions:",
            )
        )

        # Verbose level inline keyboard buttons
        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(self.handle_verbose_callback),
                pattern=r"^verbose:",
            )
        )

        logger.info("Agentic handlers registered")

    def _register_classic_handlers(self, app: Application) -> None:
        """Register full classic handler set (moved from core.py)."""
        from .handlers import callback, command, message

        handlers = [
            ("start", command.start_command),
            ("help", command.help_command),
            ("new", command.new_session),
            ("continue", command.continue_session),
            ("end", command.end_session),
            ("ls", command.list_files),
            ("cd", command.change_directory),
            ("pwd", command.print_working_directory),
            ("projects", command.show_projects),
            ("status", command.session_status),
            ("export", command.export_session),
            ("actions", command.quick_actions),
            ("git", command.git_command),
            ("provider", command.provider_command),
            ("model", command.model_command),
            ("sessions", command.sessions_command),
            ("restart", command.restart_command),
        ]
        if self.settings.enable_project_threads:
            handlers.append(("sync_threads", command.sync_threads))

        for cmd, handler in handlers:
            app.add_handler(CommandHandler(cmd, self._inject_deps(handler)))

        app.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                self._inject_deps(message.handle_text_message),
            ),
            group=10,
        )
        app.add_handler(
            MessageHandler(
                filters.Document.ALL, self._inject_deps(message.handle_document)
            ),
            group=10,
        )
        app.add_handler(
            MessageHandler(filters.PHOTO, self._inject_deps(message.handle_photo)),
            group=10,
        )
        app.add_handler(
            MessageHandler(filters.VOICE, self._inject_deps(message.handle_voice)),
            group=10,
        )
        # Provider switch buttons (must be before general callback handler)
        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(command.handle_provider_callback),
                pattern=r"^provider:",
            )
        )

        app.add_handler(
            CallbackQueryHandler(self._inject_deps(callback.handle_callback_query))
        )

        logger.info("Classic handlers registered (13 commands + full handler set)")

    async def get_bot_commands(self) -> list:  # type: ignore[type-arg]
        """Return bot commands appropriate for current mode."""
        if self.settings.agentic_mode:
            commands = [
                BotCommand("start", "Start the bot"),
                BotCommand("new", "Start a fresh session"),
                BotCommand("status", "Show session status"),
                BotCommand("verbose", "Set output verbosity (0/1/2)"),
                BotCommand("rich", "Toggle rich message rendering"),
                BotCommand("plan", "Toggle plan mode (read-only analysis)"),
                BotCommand("repo", "List repos / switch workspace"),
                BotCommand("provider", "List/switch API providers"),
                BotCommand("model", "Show/override model"),
                BotCommand("sessions", "Browse & resume sessions"),
                BotCommand("restart", "Restart the bot"),
                BotCommand("skill", "List or invoke skills"),
                BotCommand("btw", "Ask along the way (no effect on current context)"),
            ]
            if self.settings.enable_project_threads:
                commands.append(BotCommand("sync_threads", "Sync project topics"))
            return commands
        else:
            commands = [
                BotCommand("start", "Start bot and show help"),
                BotCommand("help", "Show available commands"),
                BotCommand("new", "Clear context and start fresh session"),
                BotCommand("continue", "Explicitly continue last session"),
                BotCommand("end", "End current session and clear context"),
                BotCommand("ls", "List files in current directory"),
                BotCommand("cd", "Change directory (resumes project session)"),
                BotCommand("pwd", "Show current directory"),
                BotCommand("projects", "Show all projects"),
                BotCommand("status", "Show session status"),
                BotCommand("export", "Export current session"),
                BotCommand("sessions", "Browse & resume sessions"),
                BotCommand("actions", "Show quick actions"),
                BotCommand("git", "Git repository commands"),
                BotCommand("provider", "List/switch API providers"),
                BotCommand("model", "Show/override model"),
                BotCommand("restart", "Restart the bot"),
            ]
            if self.settings.enable_project_threads:
                commands.append(BotCommand("sync_threads", "Sync project topics"))
            return commands

    async def get_bot_commands_zh(self) -> list:  # type: ignore[type-arg]
        """Return Chinese translations for bot commands."""
        if self.settings.agentic_mode:
            commands = [
                BotCommand("start", "启动机器人"),
                BotCommand("new", "新建会话"),
                BotCommand("status", "查看会话状态"),
                BotCommand("verbose", "设置输出详细度 (0/1/2)"),
                BotCommand("rich", "切换富文本渲染模式"),
                BotCommand("plan", "切换规划模式（只读分析）"),
                BotCommand("repo", "列出/切换项目目录"),
                BotCommand("provider", "列出/切换 API 提供商"),
                BotCommand("model", "查看/切换模型"),
                BotCommand("sessions", "浏览并恢复历史会话"),
                BotCommand("restart", "重启机器人"),
                BotCommand("skill", "列出或调用技能"),
                BotCommand("btw", "顺便问一下（不影响当前上下文）"),
            ]
            if self.settings.enable_project_threads:
                commands.append(BotCommand("sync_threads", "同步项目话题"))
            return commands
        else:
            commands = [
                BotCommand("start", "启动并显示帮助"),
                BotCommand("help", "显示可用命令"),
                BotCommand("new", "清空上下文，新建会话"),
                BotCommand("continue", "继续上一个会话"),
                BotCommand("end", "结束当前会话并清空上下文"),
                BotCommand("ls", "列出当前目录文件"),
                BotCommand("cd", "切换目录（恢复项目会话）"),
                BotCommand("pwd", "显示当前目录"),
                BotCommand("projects", "显示所有项目"),
                BotCommand("status", "查看会话状态"),
                BotCommand("export", "导出当前会话"),
                BotCommand("sessions", "浏览并恢复历史会话"),
                BotCommand("actions", "显示快捷操作"),
                BotCommand("git", "Git 仓库命令"),
                BotCommand("provider", "列出/切换 API 提供商"),
                BotCommand("model", "查看/切换模型"),
                BotCommand("restart", "重启机器人"),
            ]
            if self.settings.enable_project_threads:
                commands.append(BotCommand("sync_threads", "同步项目话题"))
            return commands

    # --- Agentic handlers ---

    async def agentic_start(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Brief welcome, no buttons."""
        user = update.effective_user
        sync_line = ""
        if (
            self.settings.enable_project_threads
            and self.settings.project_threads_mode == "private"
        ):
            if (
                not update.effective_chat
                or getattr(update.effective_chat, "type", "") != "private"
            ):
                await update.message.reply_text(
                    "🚫 <b>私有话题模式</b>\n\n"
                    "请在私聊中使用此机器人，并在私聊中运行 <code>/start</code>。",
                    parse_mode="HTML",
                )
                return
            manager = context.bot_data.get("project_threads_manager")
            if manager:
                try:
                    result = await manager.sync_topics(
                        context.bot,
                        chat_id=update.effective_chat.id,
                    )
                    sync_line = (
                        "\n\n🧵 话题已同步"
                        f"（新建 {result.created}，复用 {result.reused}）。"
                    )
                except PrivateTopicsUnavailableError:
                    await update.message.reply_text(
                        manager.private_topics_unavailable_message(),
                        parse_mode="HTML",
                    )
                    return
                except Exception:
                    sync_line = "\n\n🧵 话题同步失败，运行 /sync_threads 重试。"
        current_dir = context.user_data.get(
            "current_directory", self.settings.approved_directory
        )
        dir_display = f"<code>{current_dir}/</code>"

        safe_name = escape_html(user.first_name)
        await update.message.reply_text(
            f"你好 {safe_name}！我是你的 AI 编程助手。\n"
            f"直接告诉我你的需求，我可以读写和运行代码。\n\n"
            f"工作目录：{dir_display}\n"
            f"命令：/new（重置）· /status"
            f"{sync_line}",
            parse_mode="HTML",
        )

    async def agentic_new(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Reset session, one-line confirmation."""
        context.user_data["claude_session_id"] = None
        context.user_data["session_started"] = True
        context.user_data["force_new_session"] = True

        await update.message.reply_text("会话已重置，请继续。")

    def _build_provider_keyboard(self, pm: Any) -> InlineKeyboardMarkup:
        """Build inline keyboard for provider selection."""
        profiles = pm.list_profiles()
        active_name = pm.get_active_name() or ""
        buttons = []
        for p in profiles:
            label = f"✅ {p.name}" if p.name == active_name else p.name
            buttons.append(
                InlineKeyboardButton(label, callback_data=f"provider:{p.name}")
            )
        return InlineKeyboardMarkup([buttons])  # one row

    async def agentic_provider(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """List or switch API providers."""
        pm = context.bot_data.get("provider_manager")
        if not pm:
            await update.message.reply_text("Provider 管理器不可用。")
            return

        args = update.message.text.split()[1:] if update.message.text else []

        if not args:
            profiles = pm.list_profiles()
            active_name = pm.get_active_name() or "none"
            model = pm.get_effective_model() or "default"
            lines = [f"<b>Provider：</b>{active_name}  ·  <b>模型：</b>{model}"]
            for p in profiles:
                marker = "➡️ " if p.name == active_name else "  "
                lines.append(f"{marker}<code>{p.name}</code>")
            text = "\n".join(lines)
            keyboard = self._build_provider_keyboard(pm)
            await update.message.reply_text(
                text, parse_mode="HTML", reply_markup=keyboard
            )
            return

        # Switch to named profile via text arg
        name = args[0].strip()
        try:
            profile = pm.switch_profile(name)
            model = pm.get_effective_model() or "default"
            await update.message.reply_text(
                f"已切换到 <b>{profile.name}</b>  ·  模型：<code>{model}</code>\n"
                f"下次请求时生效。",
                parse_mode="HTML",
            )
        except KeyError as e:
            await update.message.reply_text(str(e))

    async def _handle_provider_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle inline button press for provider switching."""
        query = update.callback_query
        await query.answer()

        pm = context.bot_data.get("provider_manager")
        if not pm:
            await query.edit_message_text("Provider 管理器不可用。")
            return

        data = query.data  # "provider:<name>"
        name = data.split(":", 1)[1] if ":" in data else ""
        try:
            profile = pm.switch_profile(name)
            model = pm.get_effective_model() or "default"
            await query.edit_message_text(
                f"✅ 已切换到 <b>{profile.name}</b>  ·  模型：<code>{model}</code>\n"
                f"下次请求时生效。",
                parse_mode="HTML",
            )
        except KeyError as e:
            await query.edit_message_text(str(e))

    async def _handle_model_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle model:* callbacks for the model selection panel."""
        query = update.callback_query
        await query.answer()

        user_id = query.from_user.id
        pm = context.bot_data.get("provider_manager")
        if not pm:
            await query.edit_message_text("Provider 管理器不可用。")
            return

        data = query.data  # "model:<action>[:params...]"
        parts = data.split(":", 2)  # ["model", "action", "rest..."]

        if len(parts) < 2:
            return

        action = parts[1]
        rest = parts[2] if len(parts) > 2 else ""

        # Route by action
        if action == "detail":
            if rest:
                await self._show_model_detail(pm, query, context, user_id, model_name=rest)
        elif action == "toggle":
            await self._handle_model_toggle(query, user_id, rest)
        elif action == "1m":
            await self._handle_model_1m_toggle(query, user_id, rest)
        elif action == "save":
            await self._handle_model_save(pm, query, context, user_id, rest)
        elif action == "list" or action == "page":
            page = int(rest) if rest else 0
            await self._show_model_list(query, context, pm, user_id, page=page)
        elif action == "search":
            await self._enter_model_search(query, user_id)
        elif action == "search_cancel":
            await self._show_model_list(query, context, pm, user_id, page=0, search=None)
        elif action == "filter":
            await self._show_model_list(query, context, pm, user_id, page=0, search=rest)
        elif action == "noop":
            pass  # Page indicator button

    async def agentic_model(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Show or override the model (supports per-role configuration)."""
        pm = context.bot_data.get("provider_manager")
        if not pm:
            await update.message.reply_text("Provider 管理器不可用。")
            return

        args = update.message.text.split()[1:] if update.message.text else []

        # Check for -s / --search flag to search directly from command line
        if len(args) >= 2 and args[0] in ("-s", "--search"):
            keyword = " ".join(args[1:])
            await self._show_model_list(update, context, pm, user_id=update.effective_user.id, search=keyword)
            return

        # ── No args: show interactive model list panel ──────────
        if not args:
            await self._show_model_list(update, context, pm, user_id=update.effective_user.id)
            return

        first = args[0].strip().lower()

        # ── /model reset: clear all overrides ───────────────────
        if first == "reset":
            pm.set_default_model(None)
            for role in _VALID_ROLES:
                pm.set_role_model(role, None)
            model = pm.get_effective_model() or "default"
            await update.message.reply_text(
                f"已清除所有覆盖，当前模型：<code>{model}</code>", parse_mode="HTML"
            )
            return

        # ── /model <role> [model|reset]: per-role config ────────
        role = pm.resolve_role(first)
        if role:
            if len(args) < 2:
                await update.message.reply_text(f"用法: /model {first} <模型名|reset>")
                return
            value = args[1].strip()
            if value.lower() == "reset":
                pm.set_role_model(role, None)
                await update.message.reply_text(
                    f"已清除 <b>{role}</b> 角色模型覆盖", parse_mode="HTML"
                )
            else:
                pm.set_role_model(role, value)
                await update.message.reply_text(
                    f"<b>{role}</b> 角色模型设置为: <code>{value}</code>\n"
                    f"下次请求时生效。",
                    parse_mode="HTML",
                )
            return

        # ── /model <name>: set default model override ───────────
        pm.set_default_model(first)
        await update.message.reply_text(
            f"模型覆盖已设置：<code>{first}</code>\n"
            f"Provider：{pm.get_active_name() or 'default'}\n"
            f"下次请求时生效。",
            parse_mode="HTML",
        )

    MODELS_PER_PAGE = 20

    @staticmethod
    def _build_panel_status_header(pm: Any) -> str:
        """Build the status header showing current model assignment per role."""
        active_name = pm.get_active_name() or "unknown"
        default_raw = pm.get_effective_model() or "—"
        default_name, default_win = _parse_context_suffix(default_raw)
        default_label = f"{default_name} [{'1M' if default_win >= 1_000_000 else '200K'}]"

        lines = [
            f"\U0001f4cb 模型列表 — {active_name}",
            "━" * 16,
            f"默认: {default_label}",
        ]

        role_emojis = {"opus": "\U0001f419", "sonnet": "\U0001f7e1", "haiku": "\U0001f7e2"}
        roles = pm.get_role_models()
        for role in _VALID_ROLES:
            rm = roles.get(role)
            if rm:
                name, win = _parse_context_suffix(rm)
                win_label = f"[{'1M' if win >= 1_000_000 else '200K'}]"
                lines.append(f"{role_emojis[role]} {role}: {name} {win_label}")
            else:
                lines.append(f"{role_emojis[role]} {role}: —")

        return "\n".join(lines)

    def _build_model_list_markup(
        self,
        models: List[str],
        page: int,
        search: Optional[str] = None,
    ) -> InlineKeyboardMarkup:
        """Build inline keyboard for model list view."""
        if search:
            models = [m for m in models if search.lower() in m.lower()]
            start, end = 0, self.MODELS_PER_PAGE
        else:
            total_pages = max(1, (len(models) + self.MODELS_PER_PAGE - 1) // self.MODELS_PER_PAGE)
            page = max(0, min(page, total_pages - 1))
            start = page * self.MODELS_PER_PAGE
            end = min(start + self.MODELS_PER_PAGE, len(models))

        row: List[InlineKeyboardButton] = []
        for m in models[start:end]:
            label = m if len(m) <= 40 else m[:37] + "..."
            row.append(
                InlineKeyboardButton(label, callback_data=f"model:detail:{m}")
            )
        keyboard = self._chunk_buttons(row, 2)

        bottom_row: List[InlineKeyboardButton] = []
        bottom_row.append(InlineKeyboardButton("\U0001f50d 搜索", callback_data="model:search"))

        if search:
            bottom_row.append(InlineKeyboardButton("❌ 取消搜索", callback_data="model:search_cancel"))
        else:
            total_pages = max(1, (len(models) + self.MODELS_PER_PAGE - 1) // self.MODELS_PER_PAGE)
            if page > 0:
                bottom_row.append(InlineKeyboardButton("⬅", callback_data=f"model:page:{page - 1}"))
            bottom_row.append(InlineKeyboardButton(
                f"{page + 1}/{total_pages}",
                callback_data="model:noop"
            ))
            if page < total_pages - 1:
                bottom_row.append(InlineKeyboardButton("➡", callback_data=f"model:page:{page + 1}"))

        keyboard.append(bottom_row)
        return InlineKeyboardMarkup(keyboard)

    async def _show_model_list(
        self,
        update_or_query: Union[Update, CallbackQuery],
        context: ContextTypes.DEFAULT_TYPE,
        pm: Any,
        user_id: int,
        page: int = 0,
        search: Optional[str] = None,
    ) -> None:
        """Render the model list panel. Uses edit_message if source is callback query."""
        try:
            models = await pm.fetch_models()
        except Exception:
            logger.warning("fetch_models failed in _show_model_list", exc_info=True)
            models = []

        header = self._build_panel_status_header(pm)

        is_callback = hasattr(update_or_query, "data")
        if not models:
            text = f"{header}\n\n⚠️ 无法拉取模型列表，请检查 Provider 连接。"
            markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("\U0001f504 重试", callback_data="model:list:0")
            ]])
        else:
            text = header
            markup = self._build_model_list_markup(models, page, search)

        if is_callback:
            query = update_or_query  # type: ignore
            chat_id = query.message.chat_id
            message_id = query.message.message_id
        else:
            msg = update_or_query.message  # type: ignore
            chat_id = msg.chat_id
            message_id = None

        self._model_panel_state[user_id] = {
            "page": page,
            "search": search,
            "chat_id": chat_id,
            "message_id": message_id,
        }

        if is_callback and message_id:
            query = update_or_query  # type: ignore
            try:
                await query.edit_message_text(text, reply_markup=markup)
            except Exception:
                await query.answer("面板已过期，请重新 /model", show_alert=True)
        else:
            sent = await update_or_query.message.reply_text(text, reply_markup=markup)  # type: ignore
            self._model_panel_state[user_id]["message_id"] = sent.message_id
            self._model_panel_state[user_id]["chat_id"] = sent.chat_id

    async def _enter_model_search(
        self, query: CallbackQuery, user_id: int
    ) -> None:
        """Enter search mode: prompt user to type a search keyword."""
        text = (
            "📋 模型列表  🔍 搜索中...\n"
            "━━━━━━━━━━━━━━━━\n"
            "请在聊天框中输入搜索关键词（如 deepseek），\n"
            "我会过滤匹配的模型。\n\n"
            "（回复此消息的关键词会被拦截为搜索词）\n"
            "━━━━━━━━━━━━━━━━"
        )
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ 取消搜索", callback_data="model:search_cancel"),
        ]])

        self._model_panel_state[user_id] = {
            "search": "",  # non-None empty string = in search mode, awaiting input
            "chat_id": query.message.chat_id,
            "message_id": query.message.message_id,
        }

        try:
            await query.edit_message_text(text, reply_markup=markup)
        except Exception:
            await query.answer("面板已过期，请重新 /model", show_alert=True)

    @staticmethod
    def _display_width(text: str) -> int:
        """Estimate display width treating CJK / emoji / fullwidth as 2."""
        w = 0
        for ch in text:
            eaw = unicodedata.east_asian_width(ch)
            w += 2 if eaw in ("W", "F") else 1
        return w

    # ------------------------------------------------------------------ #
    #  Session resume notification                                        #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _relative_time(dt: Optional[datetime]) -> str:
        """Return a human-readable relative time string."""
        if dt is None:
            return ""
        now = datetime.now(UTC)
        # Ensure dt is timezone-aware
        if dt.tzinfo is None:
            from datetime import timezone

            dt = dt.replace(tzinfo=timezone.utc)
        delta = now - dt
        seconds = int(delta.total_seconds())
        if seconds < 60:
            return "刚刚"
        if seconds < 3600:
            return f"{seconds // 60}分钟前"
        if seconds < 86400:
            return f"{seconds // 3600}小时前"
        return f"{seconds // 86400}天前"

    async def _build_session_resume_text(
        self,
        session_id: str,
        project_path: Path,
        claude_integration: Any,
        session_meta: Any = None,
    ) -> str:
        """Build a notification text for session resume.

        Args:
            session_id: The Claude session UUID.
            project_path: The working directory of the session.
            claude_integration: The ClaudeIntegration facade instance.
            session_meta: Optional ClaudeSession with last_used, message_count.
        """
        sdk_info = await ClaudeIntegration.get_sdk_session_info(
            session_id, Path(project_path)
        )
        title = None
        if sdk_info:
            title = (
                sdk_info.get("title")
                or sdk_info.get("summary")
                or sdk_info.get("custom_title")
                or sdk_info.get("first_prompt")
            )
        if not title:
            title = await claude_integration.read_session_title(
                session_id, project_path
            )
        dir_name = project_path.name or str(project_path)

        meta_parts: List[str] = []
        last_used = (
            sdk_info.get("last_used")
            if sdk_info and sdk_info.get("last_used") is not None
            else getattr(session_meta, "last_used", None)
        )
        rel = self._relative_time(last_used)
        if rel:
            meta_parts.append(rel)
        msg_count = (
            sdk_info.get("message_count")
            if sdk_info and sdk_info.get("message_count") is not None
            else getattr(session_meta, "message_count", 0)
        )
        if msg_count:
            meta_parts.append(f"{msg_count}条")

        if title:
            line2_parts = [session_id, dir_name] + meta_parts
            return f"📎 {title}\n   {' · '.join(line2_parts)}"
        # No title — session_id is the headline
        line2_parts = [dir_name] + meta_parts
        return f"📎 {session_id}\n   {' · '.join(line2_parts)}"

    @staticmethod
    async def _git_info(repo_path: str) -> Tuple[str, int, int]:
        """Return (branch, staged_count, modified_count) for a git repo."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                "branch",
                "--show-current",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=repo_path,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            branch = stdout.decode().strip() or "HEAD"

            proc2 = await asyncio.create_subprocess_exec(
                "git",
                "diff",
                "--cached",
                "--numstat",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=repo_path,
            )
            out2, _ = await asyncio.wait_for(proc2.communicate(), timeout=5)
            staged = len([line for line in out2.decode().strip().split("\n") if line])

            proc3 = await asyncio.create_subprocess_exec(
                "git",
                "diff",
                "--numstat",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=repo_path,
            )
            out3, _ = await asyncio.wait_for(proc3.communicate(), timeout=5)
            modified = len([line for line in out3.decode().strip().split("\n") if line])

            return branch, staged, modified
        except Exception:
            return "", 0, 0

    async def agentic_status(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Statusline-style two-line status display."""
        try:
            current_dir = context.user_data.get(
                "current_directory", self.settings.approved_directory
            )
            dir_name = os.path.basename(str(current_dir)) or str(current_dir)

            # Model name
            pm = context.bot_data.get("provider_manager")
            model_name = ""
            if pm:
                from ..config.providers import _parse_context_suffix

                raw = pm.get_effective_model() or ""
                model_name, _ = _parse_context_suffix(raw)
                model_name = model_name.replace("claude-", "")

            # Git info — only show real branches
            branch, staged, modified = await self._git_info(str(current_dir))
            if branch == "HEAD":
                branch = ""

            # Line 1: model · directory · git
            parts = []
            if model_name:
                parts.append(f"🧠 {model_name}")
            parts.append(f"📂 {dir_name}")
            if branch:
                git_parts = [f"🌿 {branch}"]
                if staged:
                    git_parts.append(f"+{staged}")
                if modified:
                    git_parts.append(f"~{modified}")
                parts.append(" ".join(git_parts))
            line1 = "  ".join(parts)

            # Line 2: progress bar · context percentage · token count
            usage = context.user_data.get("last_usage") or {}

            def _int(d: dict, *keys: str) -> int:
                for k in keys:
                    v = d.get(k)
                    if isinstance(v, (int, float)) and v >= 0:
                        return int(v)
                return 0

            ctx_tok = _int(usage, "input_tokens", "prompt_tokens")
            ctx_window = pm.get_context_window() if pm else 200_000
            ctx_pct = min(100, ctx_tok * 100 // ctx_window)
            tok_str = f"{ctx_tok // 1000}k" if ctx_tok >= 1000 else str(ctx_tok)

            bar_len = 15
            filled = ctx_pct * bar_len // 100
            bar = "▓" * filled + "░" * (bar_len - filled)
            line2 = f"{bar}  {ctx_pct}%  {tok_str}"

            rich_enabled = self._is_rich_messages_enabled(context)
            if rich_enabled:
                # Rich Messages: render as a markdown table
                md = (
                    "# 📊 状态\n\n"
                    "| 项目 | 值 |\n"
                    "|------|----|\n"
                    f"| 模型 | `{model_name or '—'}` |\n"
                    f"| 目录 | `{dir_name}` |\n"
                    f"| Git | `{branch or '—'}`"
                    + (f" `+{staged}`" if staged else "")
                    + (f" `~{modified}`" if modified else "")
                    + " |\n"
                    f"| 上下文 | `{line2}` |\n"
                )
                try:
                    from .utils.telegram_rich import send_rich_message

                    await send_rich_message(
                        context.bot,
                        update.message.chat_id,
                        md,
                        reply_parameters={
                            "message_id": update.message.message_id
                        },
                    )
                except Exception:
                    rich_enabled = False

            if not rich_enabled:
                await update.message.reply_text(
                    f"```\n{line1}\n{line2}\n```", parse_mode="Markdown"
                )
        except Exception as e:
            logger.error("agentic_status_error", error=str(e))
            await update.message.reply_text(f"状态查询出错：{e}")

    def _get_verbose_level(self, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Return effective verbose level: per-user override or global default."""
        user_override = context.user_data.get("verbose_level")
        if user_override is not None:
            return int(user_override)
        return self.settings.verbose_level

    async def agentic_verbose(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Set output verbosity: /verbose [0|1|2]."""
        args = update.message.text.split()[1:] if update.message.text else []
        if not args:
            current = self._get_verbose_level(context)
            labels = {0: "静默", 1: "正常", 2: "详细"}
            buttons = []
            for lvl, label in labels.items():
                marker = "✅ " if lvl == current else ""
                buttons.append(
                    InlineKeyboardButton(
                        f"{marker}{label}（{lvl}）",
                        callback_data=f"verbose:{lvl}",
                    )
                )
            keyboard = InlineKeyboardMarkup([buttons])
            await update.message.reply_text(
                f"当前输出详细度：<b>{current}</b>（{labels.get(current, '?')}）",
                parse_mode="HTML",
                reply_markup=keyboard,
            )
            return

        try:
            level = int(args[0])
            if level not in (0, 1, 2):
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "请使用：/verbose 0、/verbose 1 或 /verbose 2"
            )
            return

        context.user_data["verbose_level"] = level
        labels = {0: "静默", 1: "正常", 2: "详细"}
        await update.message.reply_text(
            f"输出详细度已设为 <b>{level}</b>（{labels[level]}）",
            parse_mode="HTML",
        )

    async def handle_verbose_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle inline keyboard callback for /verbose level selection."""
        query = update.callback_query
        await query.answer()
        try:
            level = int(query.data.split(":")[1])
        except (IndexError, ValueError):
            return
        if level not in (0, 1, 2):
            return
        context.user_data["verbose_level"] = level
        labels = {0: "静默", 1: "正常", 2: "详细"}
        await query.edit_message_text(
            f"输出详细度已设为 <b>{level}</b>（{labels[level]}）",
            parse_mode="HTML",
            reply_markup=None,
        )

    async def agentic_rich(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Toggle rich messages: /rich [on|off]."""
        args = update.message.text.split()[1:] if update.message.text else []
        if not args:
            current = self._is_rich_messages_enabled(context)
            status = "✅ 开启" if current else "❌ 关闭"
            source = "用户设置" if "rich_messages_enabled" in context.user_data else "全局默认"
            await update.message.reply_text(
                f"Rich Messages：<b>{status}</b>（{source}）\n\n"
                "用法：<code>/rich on</code> 或 <code>/rich off</code>\n"
                "Rich Messages 使用 Telegram Bot API 10.1 原生渲染 Markdown\n"
                "（表格、列表、标题、代码块等）",
                parse_mode="HTML",
            )
            return

        arg = args[0].lower()
        if arg in ("on", "1", "true", "开启"):
            context.user_data["rich_messages_enabled"] = True
            await update.message.reply_text(
                "✅ Rich Messages 已<b>开启</b>（仅对当前会话生效）",
                parse_mode="HTML",
            )
        elif arg in ("off", "0", "false", "关闭"):
            context.user_data["rich_messages_enabled"] = False
            await update.message.reply_text(
                "❌ Rich Messages 已<b>关闭</b>（仅对当前会话生效）",
                parse_mode="HTML",
            )
        else:
            await update.message.reply_text(
                "用法：<code>/rich on</code> 或 <code>/rich off</code>"
            )

    def _is_rich_messages_enabled(
        self, context: ContextTypes.DEFAULT_TYPE
    ) -> bool:
        """Return effective rich messages setting: per-user override or global default."""
        user_override = context.user_data.get("rich_messages_enabled")
        if user_override is not None:
            return bool(user_override)
        return self.settings.enable_rich_messages

    async def agentic_plan(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Toggle plan mode. /plan [prompt] enters plan mode and sends prompt."""
        # Extract text after /plan command
        raw = (update.message.text or "").strip()
        parts = raw.split(None, 1)
        prompt_text = parts[1] if len(parts) > 1 else None

        current = context.user_data.get("permission_mode")
        if current == "plan":
            # Already in plan mode — if there's a prompt, just send it
            if prompt_text:
                await self.agentic_text(update, context, text=prompt_text)
                return
            # No prompt — exit plan mode
            context.user_data.pop("permission_mode", None)
            await update.message.reply_text(
                "已退出规划模式，恢复正常权限。",
                parse_mode="HTML",
            )
            return

        # Enter plan mode
        context.user_data["permission_mode"] = "plan"

        if prompt_text:
            # Enter plan mode + send prompt to Claude
            await self.agentic_text(update, context, text=prompt_text)
        else:
            # Just toggle on
            await update.message.reply_text(
                "已进入 <b>规划模式</b>。\n\n"
                "• 可以读取文件、分析代码\n"
                "• 不能编辑文件、不能执行命令\n"
                "• 发送 <code>/plan</code> 退出后可执行修改\n\n"
                "💡 Claude 也会在需要时自动请求进入规划模式。",
                parse_mode="HTML",
            )

    async def agentic_skill(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """List available skills or invoke one. /skill [name]"""
        raw = (update.message.text or "").strip()
        parts = raw.split(None, 1)

        if len(parts) <= 1:
            # No argument — list all discovered skills
            from src.claude.skills import discover_skills

            working_dir = context.user_data.get(
                "current_directory", self.settings.approved_directory
            )
            skills = discover_skills(
                working_directory=Path(working_dir) if working_dir else None,
                setting_sources=self.settings.claude_setting_sources,
            )
            if not skills:
                await update.message.reply_text(
                    "未发现已安装的 Skills。\n\n"
                    "可通过 CLI 安装：<code>claude plugin install &lt;name&gt;</code>",
                    parse_mode="HTML",
                )
                return

            # Always use HTML for /skill — <code> tags enable tap-to-copy.
            # Rich Messages' backtick code blocks do NOT support tap-to-copy,
            # and RichTextBotCommand only works for registered bot commands,
            # not arbitrary /skill <name> invocations.
            lines = [f"<b>📋 可用 Skills · 共 {len(skills)} 个</b>\n"]
            for s in skills:
                lines.append(f"🔹 <code>/skill {s.name}</code>")
                if s.description:
                    lines.append(
                        f"　　<i>{html.escape(s.description)}</i>"
                    )
            lines.append("\n<i>点击指令即可复制到剪贴板。</i>")
            await update.message.reply_text(
                "\n".join(lines), parse_mode="HTML"
            )
        else:
            # Has argument — forward to Claude as skill invocation
            skill_text = parts[1]  # "smart-search-cli 今日新闻 top1"
            await self.agentic_text(update, context, text=f"/{skill_text}")

    async def _handle_btw(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /btw <question> as an immediate side question."""
        msg = update.effective_message
        if not msg or not msg.text:
            return

        user_id = update.effective_user.id
        logger.info(
            "/btw received",
            user_id=user_id,
            message_length=len(msg.text),
            has_active_request=user_id in self._active_requests,
        )
        command_match = _BTW_COMMAND_TEXT_RE.match(msg.text.strip())
        raw_question = command_match.group("body") if command_match else ""
        question = raw_question.strip().lstrip(":：,，-").strip()

        if not question:
            logger.info("/btw missing question", user_id=user_id)
            await msg.reply_text(
                "Usage: /btw <your question>\n"
                "Example: /btw what config file did you just mention?"
            )
            return

        active_request = self._active_requests.get(user_id)
        runtime_snapshot = (
            await active_request.snapshot() if active_request is not None else None
        )
        if runtime_snapshot is not None:
            # During a brand-new main request the SDK may not have emitted its
            # session id yet. In that case /btw must answer from the runtime
            # snapshot only, not accidentally fork the previous chat session.
            session_id = runtime_snapshot.session_id
        else:
            session_id = context.user_data.get("claude_session_id", "")
        working_directory = Path(
            runtime_snapshot.working_directory
            if runtime_snapshot
            else context.user_data.get(
                "current_directory", self.settings.approved_directory
            )
        )

        claude_integration = context.bot_data.get("claude_integration")
        if not claude_integration:
            logger.warning("/btw missing Claude integration", user_id=user_id)
            await msg.reply_text("btw: Claude integration is not available.")
            return

        progress_msg = await msg.reply_text(
            "btw: \u56de\u7b54\u4e2d...",
            reply_to_message_id=msg.message_id,
        )
        logger.info("/btw progress message sent", user_id=user_id)
        start_time = asyncio.get_event_loop().time()

        try:
            answer = await claude_integration.run_btw(
                question=question,
                working_directory=working_directory,
                user_id=user_id,
                session_id=session_id,
                runtime_snapshot=runtime_snapshot,
            )
            answer_text = getattr(answer, "content", answer)
            answer_text = str(answer_text or "").strip()

            if not answer_text:
                await progress_msg.edit_text(
                    "btw: no active or resumable Claude session is available yet.\n"
                    "Send a normal message first, then use /btw while it is running."
                )
                return

            fork_session_id = getattr(answer, "fork_session_id", None)

            response_text = f"btw: {answer_text}"

            from .utils.formatting import ResponseFormatter

            formatter = ResponseFormatter(self.settings)
            formatted_messages = formatter.format_claude_response(response_text)

            first_response = True
            for formatted in formatted_messages:
                if not formatted.text or not formatted.text.strip():
                    continue
                if first_response:
                    await progress_msg.edit_text(
                        formatted.text,
                        parse_mode=formatted.parse_mode or None,
                    )
                    first_response = False
                else:
                    await msg.reply_text(
                        formatted.text,
                        parse_mode=formatted.parse_mode or None,
                    )

            duration_ms = int((asyncio.get_event_loop().time() - start_time) * 1000)
            audit_logger = context.bot_data.get("audit_logger")
            if audit_logger:
                await audit_logger.log_command(
                    user_id=user_id,
                    command="btw",
                    args=[
                        "runtime_snapshot" if runtime_snapshot else "history_session"
                    ],
                    success=True,
                )

            logger.info(
                "/btw completed",
                user_id=user_id,
                duration_ms=duration_ms,
                answer_length=len(answer_text),
                used_runtime_snapshot=bool(runtime_snapshot),
                fork_session_id=fork_session_id,
            )

        except Exception as exc:
            duration_ms = int((asyncio.get_event_loop().time() - start_time) * 1000)
            logger.error(
                "/btw failed", user_id=user_id, duration_ms=duration_ms, error=str(exc)
            )

            await progress_msg.edit_text(
                "btw: query failed, please try again later.",
            )

            audit_logger = context.bot_data.get("audit_logger")
            if audit_logger:
                await audit_logger.log_command(
                    user_id=user_id,
                    command="btw",
                    args=[
                        "runtime_snapshot" if runtime_snapshot else "history_session"
                    ],
                    success=False,
                )

    def _format_verbose_progress(
        self,
        activity_log: List[Dict[str, Any]],
        verbose_level: int,
        start_time: float,
    ) -> str:
        """Build the progress message text based on activity so far."""
        if not activity_log:
            return "处理中..."

        elapsed = time.time() - start_time
        lines: List[str] = [f"处理中...（{elapsed:.0f}s）\n"]

        for entry in activity_log[-15:]:  # Show last 15 entries max
            kind = entry.get("kind", "tool")
            if kind == "text":
                # Claude's intermediate reasoning/commentary
                snippet = entry.get("detail", "")
                if verbose_level >= 2:
                    lines.append(f"\U0001f4ac {snippet}")
                else:
                    # Level 1: one short line
                    lines.append(f"\U0001f4ac {snippet[:80]}")
            else:
                # Tool call
                icon = _tool_icon(entry["name"])
                if verbose_level >= 2 and entry.get("detail"):
                    lines.append(f"{icon} {entry['name']}: {entry['detail']}")
                else:
                    lines.append(f"{icon} {entry['name']}")

        if len(activity_log) > 15:
            lines.insert(1, f"...（还有 {len(activity_log) - 15} 条更早的记录）\n")

        return "\n".join(lines)

    @staticmethod
    def _summarize_tool_input(tool_name: str, tool_input: Dict[str, Any]) -> str:
        """Return a short summary of tool input for verbose level 2."""
        if not tool_input:
            return ""
        if tool_name in ("Read", "Write", "Edit", "MultiEdit"):
            path = tool_input.get("file_path") or tool_input.get("path", "")
            if path:
                # Show just the filename, not the full path
                return path.rsplit("/", 1)[-1]
        if tool_name in ("Glob", "Grep"):
            pattern = tool_input.get("pattern", "")
            if pattern:
                return pattern[:60]
        if tool_name == "Bash":
            cmd = tool_input.get("command", "")
            if cmd:
                return _redact_secrets(cmd[:100])[:80]
        if tool_name in ("WebFetch", "WebSearch"):
            return (tool_input.get("url", "") or tool_input.get("query", ""))[:60]
        if tool_name == "Task":
            desc = tool_input.get("description", "")
            if desc:
                return desc[:60]
        # Generic: show first key's value
        for v in tool_input.values():
            if isinstance(v, str) and v:
                return v[:60]
        return ""

    @staticmethod
    def _start_typing_heartbeat(
        chat: Any,
        interval: float = 2.0,
    ) -> "asyncio.Task[None]":
        """Start a background typing indicator task.

        Sends typing every *interval* seconds, independently of
        stream events. Cancel the returned task in a ``finally``
        block.
        """

        async def _heartbeat() -> None:
            try:
                while True:
                    await asyncio.sleep(interval)
                    try:
                        await chat.send_action("typing")
                    except Exception:
                        pass
            except asyncio.CancelledError:
                pass

        return asyncio.create_task(_heartbeat())

    def _make_stream_callback(
        self,
        verbose_level: int,
        progress_msg: Any,
        tool_log: List[Dict[str, Any]],
        start_time: float,
        reply_markup: Optional[InlineKeyboardMarkup] = None,
        mcp_images: Optional[List[ImageAttachment]] = None,
        approved_directory: Optional[Path] = None,
        draft_streamer: Optional[DraftStreamer] = None,
        interrupt_event: Optional[asyncio.Event] = None,
        active_request: Optional[ActiveRequest] = None,
    ) -> Optional[Callable[[StreamUpdate], Any]]:
        """Create a stream callback for verbose progress updates.

        When *mcp_images* is provided, the callback also intercepts
        ``send_image_to_user`` tool calls and collects validated
        :class:`ImageAttachment` objects for later Telegram delivery.

        When *draft_streamer* is provided, tool activity and assistant
        text are streamed to the user in real time via
        ``sendMessageDraft``.

        Returns None when verbose_level is 0 **and** no MCP image
        collection or draft streaming is requested.
        Typing indicators are handled by a separate heartbeat task.
        """
        need_mcp_intercept = mcp_images is not None and approved_directory is not None

        need_runtime_snapshot = active_request is not None
        if (
            verbose_level == 0
            and not need_mcp_intercept
            and draft_streamer is None
            and not need_runtime_snapshot
        ):
            return None

        last_edit_time = [0.0]  # mutable container for closure

        async def _on_stream(update_obj: StreamUpdate) -> None:
            # Stop all streaming activity after interrupt
            if interrupt_event is not None and interrupt_event.is_set():
                return

            if active_request is not None:
                await active_request.apply_stream_update(update_obj)

            # Intercept send_image_to_user MCP tool calls.
            # The SDK namespaces MCP tools as "mcp__<server>__<tool>",
            # so match both the bare name and the namespaced variant.
            if update_obj.tool_calls and need_mcp_intercept:
                for tc in update_obj.tool_calls:
                    tc_name = tc.get("name", "")
                    if tc_name == "send_image_to_user" or tc_name.endswith(
                        "__send_image_to_user"
                    ):
                        tc_input = tc.get("input", {})
                        file_path = tc_input.get("file_path", "")
                        caption = tc_input.get("caption", "")
                        img = validate_image_path(
                            file_path, approved_directory, caption
                        )
                        if img:
                            mcp_images.append(img)

            # Capture tool calls
            if update_obj.tool_calls:
                for tc in update_obj.tool_calls:
                    name = tc.get("name", "unknown")
                    detail = self._summarize_tool_input(name, tc.get("input", {}))
                    if verbose_level >= 1:
                        tool_log.append(
                            {"kind": "tool", "name": name, "detail": detail}
                        )
                    if draft_streamer:
                        icon = _tool_icon(name)
                        line = (
                            f"{icon} {name}: {detail}" if detail else f"{icon} {name}"
                        )
                        await draft_streamer.append_tool(line)

            # Capture assistant text (reasoning / commentary)
            if update_obj.type == "assistant" and update_obj.content:
                text = update_obj.content.strip()
                if text:
                    first_line = text.split("\n", 1)[0].strip()
                    if first_line:
                        if verbose_level >= 1:
                            tool_log.append(
                                {"kind": "text", "detail": first_line[:120]}
                            )
                        if draft_streamer:
                            await draft_streamer.append_tool(
                                f"\U0001f4ac {first_line[:120]}"
                            )

            # Stream text to user via draft (prefer token deltas;
            # skip full assistant messages to avoid double-appending)
            if draft_streamer and update_obj.content:
                if update_obj.type == "stream_delta":
                    await draft_streamer.append_text(update_obj.content)

            # Throttle progress message edits to avoid Telegram rate limits
            if not draft_streamer and verbose_level >= 1:
                now = time.time()
                if (now - last_edit_time[0]) >= 2.0 and tool_log:
                    last_edit_time[0] = now
                    new_text = self._format_verbose_progress(
                        tool_log, verbose_level, start_time
                    )
                    try:
                        current_progress_msg = (
                            await active_request.current_progress_msg()
                            if active_request is not None
                            else progress_msg
                        )
                        await current_progress_msg.edit_text(
                            new_text, reply_markup=reply_markup
                        )
                    except Exception:
                        pass

        return _on_stream

    async def _send_rich_message_with_fallback(
        self,
        update: Update,
        message: "FormattedMessage",
        reply_to_message_id: Optional[int] = None,
    ) -> None:
        """Send a rich message, falling back to HTML on failure."""
        if not message.rich_markdown:
            await update.message.reply_text(
                message.text,
                parse_mode=message.parse_mode or None,
                reply_markup=None,
                reply_to_message_id=reply_to_message_id,
            )
            return

        try:
            from .utils.telegram_rich import send_rich_message

            reply_params = None
            if reply_to_message_id:
                reply_params = {"message_id": reply_to_message_id}
            await send_rich_message(
                update.get_bot(),
                update.message.chat_id,
                message.rich_markdown,
                reply_parameters=reply_params,
            )
        except Exception as rich_err:
            logger.warning(
                "Rich message send failed, falling back to HTML",
                error=str(rich_err),
            )
            await update.message.reply_text(
                message.text,
                parse_mode=message.parse_mode or None,
                reply_markup=None,
                reply_to_message_id=reply_to_message_id,
            )

    async def _send_images(
        self,
        update: Update,
        images: List[ImageAttachment],
        reply_to_message_id: Optional[int] = None,
        caption: Optional[str] = None,
        caption_parse_mode: Optional[str] = None,
    ) -> bool:
        """Send extracted images as a media group (album) or documents.

        If *caption* is provided and fits (≤1024 chars), it is attached to the
        photo / first album item so text + images appear as one message.

        Returns True if the caption was successfully embedded in the photo message.
        """
        photos: List[ImageAttachment] = []
        documents: List[ImageAttachment] = []
        for img in images:
            if should_send_as_photo(img.path):
                photos.append(img)
            else:
                documents.append(img)

        # Telegram caption limit
        use_caption = bool(
            caption and len(caption) <= 1024 and photos and not documents
        )
        caption_sent = False

        # Send raster photos as a single album (Telegram groups 2-10 items)
        if photos:
            try:
                if len(photos) == 1:
                    with open(photos[0].path, "rb") as f:
                        await update.message.reply_photo(
                            photo=f,
                            reply_to_message_id=reply_to_message_id,
                            caption=caption if use_caption else None,
                            parse_mode=caption_parse_mode if use_caption else None,
                        )
                    caption_sent = use_caption
                else:
                    media = []
                    file_handles = []
                    for idx, img in enumerate(photos[:10]):
                        fh = open(img.path, "rb")  # noqa: SIM115
                        file_handles.append(fh)
                        media.append(
                            InputMediaPhoto(
                                media=fh,
                                caption=caption if use_caption and idx == 0 else None,
                                parse_mode=(
                                    caption_parse_mode
                                    if use_caption and idx == 0
                                    else None
                                ),
                            )
                        )
                    try:
                        await update.message.chat.send_media_group(
                            media=media,
                            reply_to_message_id=reply_to_message_id,
                        )
                        caption_sent = use_caption
                    finally:
                        for fh in file_handles:
                            fh.close()
            except Exception as e:
                logger.warning("Failed to send photo album", error=str(e))

        # Send SVGs / large files as documents (one by one — can't mix in album)
        for img in documents:
            try:
                with open(img.path, "rb") as f:
                    await update.message.reply_document(
                        document=f,
                        filename=img.path.name,
                        reply_to_message_id=reply_to_message_id,
                    )
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.warning(
                    "Failed to send document image",
                    path=str(img.path),
                    error=str(e),
                )

        return caption_sent

    async def agentic_text(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        text: Optional[str] = None,
    ) -> None:
        """Direct Claude passthrough. Simple progress. No suggestions."""
        user_id = update.effective_user.id
        message_text = text if text is not None else update.message.text

        from .features.session_edit import handle_pending_session_edit

        if text is None and await handle_pending_session_edit(update, context):
            return

        # Check if user is in model search mode
        search_state = self._model_panel_state.get(user_id)
        if search_state and search_state.get("search") is not None:
            keyword = message_text.strip()
            if not keyword:
                return  # empty message, stay in search mode
            pm = context.bot_data.get("provider_manager")
            if pm:
                search_state["search"] = keyword
                self._model_panel_state[user_id] = search_state
                try:
                    models = await pm.fetch_models()
                except Exception:
                    models = []
                if models:
                    text = self._build_panel_status_header(pm)
                    markup = self._build_model_list_markup(models, page=0, search=keyword)
                    try:
                        await context.bot.edit_message_text(
                            chat_id=search_state["chat_id"],
                            message_id=search_state["message_id"],
                            text=text,
                            reply_markup=markup,
                        )
                    except Exception:
                        pass  # panel expired, ignore
            await update.message.delete()  # hide the search keyword from chat
            return

        # Check if user is answering an "Other" question from AskUserQuestion
        waiting = self._auq_waiting_other.pop(user_id, None)
        if waiting:
            # Remove from update processor's bypass set
            from .update_processor import StopAwareUpdateProcessor

            StopAwareUpdateProcessor.auq_other_waiting.discard(user_id)

            tool_use_id = waiting["tool_use_id"]
            future = self._pending_auq.get(tool_use_id)
            if future and not future.done():
                future.set_result({"selected": [message_text]})
                # Edit the prompt message to show the answer
                tid_short = waiting["tid_short"]
                auq_meta = getattr(self, "_auq_messages", {}).get(tid_short)
                if auq_meta:
                    q_text = auq_meta["question_text"]
                    hdr = auq_meta.get("header", "")
                    hdr_line = f"<b>{escape_html(hdr)}</b>\n" if hdr else ""
                    opts = auq_meta.get("options", [])
                    lines = [
                        f"🤔 {hdr_line}<b>Claude 想问你：</b>\n"
                        f"{escape_html(q_text)}\n"
                    ]
                    for i, opt in enumerate(opts[:4]):
                        lbl = opt.get("label", f"选项 {i + 1}")
                        desc = opt.get("description", "")
                        entry = f"☐ {escape_html(lbl)}"
                        if desc:
                            entry += f"\n  {escape_html(desc)}"
                        lines.append(entry)
                    lines.append(f"\n📝 <b>自由输入：{escape_html(message_text)}</b>")
                    try:
                        await auq_meta["msg"].edit_text(
                            "\n".join(lines),
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass
            return

        # Check if user is providing plan feedback from ExitPlanMode
        plan_waiting = self._pending_plan_waiting.pop(user_id, None)
        if plan_waiting:
            from .update_processor import StopAwareUpdateProcessor

            StopAwareUpdateProcessor.plan_feedback_waiting.discard(user_id)
            future = plan_waiting["future"]
            if future and not future.done():
                active_request = self._active_requests.get(user_id)
                if active_request is not None:
                    stop_kb = InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "停止", callback_data=f"stop:{user_id}"
                                )
                            ]
                        ]
                    )
                    try:
                        progress_msg = await update.message.reply_text(
                            "✏️ 已收到修改意见，正在让 Claude 调整计划...",
                            reply_to_message_id=update.message.message_id,
                            reply_markup=stop_kb,
                        )
                        await active_request.switch_progress_msg(progress_msg)
                    except Exception as exc:
                        logger.debug(
                            "Failed to create plan feedback progress message",
                            user_id=user_id,
                            error=str(exc),
                        )
                future.set_result({"action": "feedback", "feedback": message_text})
                query = plan_waiting.get("query")
                if query:
                    try:
                        await query.edit_message_text(
                            f"✏️ 已收到修改意见：{escape_html(message_text)}",
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass
                if active_request is None:
                    await update.message.reply_text(
                        "✅ 已将修改意见发送给 Claude，正在调整方案..."
                    )
            return

        logger.info(
            "Agentic text message",
            user_id=user_id,
            message_length=len(message_text),
        )

        # Rate limit check
        rate_limiter = context.bot_data.get("rate_limiter")
        if rate_limiter:
            allowed, limit_message = await rate_limiter.check_rate_limit(user_id, 0.001)
            if not allowed:
                await update.message.reply_text(f"⏱️ {limit_message}")
                return

        chat = update.message.chat
        await chat.send_action("typing")

        verbose_level = self._get_verbose_level(context)

        current_dir = context.user_data.get(
            "current_directory", self.settings.approved_directory
        )
        session_id = context.user_data.get("claude_session_id")
        start_time = time.time()

        # Create Stop button and interrupt event
        interrupt_event = asyncio.Event()
        stop_kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("停止", callback_data=f"stop:{user_id}")]]
        )
        progress_msg = await update.message.reply_text(
            "处理中...", reply_markup=stop_kb
        )

        # Register active request for stop callback
        active_request = ActiveRequest(
            user_id=user_id,
            session_id=session_id,
            working_directory=Path(current_dir),
            original_prompt=message_text,
            started_at=start_time,
            interrupt_event=interrupt_event,
            progress_msg=progress_msg,
        )
        self._active_requests[user_id] = active_request

        claude_integration = context.bot_data.get("claude_integration")
        if not claude_integration:
            self._active_requests.pop(user_id, None)
            await progress_msg.edit_text(
                "Claude 集成不可用，请检查配置。",
                reply_markup=None,
            )
            return

        # Check if /new was used — skip auto-resume for this first message.
        # Flag is only cleared after a successful run so retries keep the intent.
        force_new = bool(context.user_data.get("force_new_session"))

        # --- Verbose progress tracking via stream callback ---
        tool_log: List[Dict[str, Any]] = []
        mcp_images: List[ImageAttachment] = []

        # Stream drafts (private chats only)
        rich_enabled = self._is_rich_messages_enabled(context)
        draft_streamer: Optional[DraftStreamer] = None
        if self.settings.enable_stream_drafts and chat.type == "private":
            draft_streamer = DraftStreamer(
                bot=context.bot,
                chat_id=chat.id,
                draft_id=generate_draft_id(),
                message_thread_id=update.message.message_thread_id,
                throttle_interval=self.settings.stream_draft_interval,
                use_rich_messages=rich_enabled,
            )

        on_stream = self._make_stream_callback(
            verbose_level,
            progress_msg,
            tool_log,
            start_time,
            reply_markup=stop_kb,
            mcp_images=mcp_images,
            approved_directory=self.settings.approved_directory,
            draft_streamer=draft_streamer,
            interrupt_event=interrupt_event,
            active_request=active_request,
        )

        # Independent typing heartbeat — stays alive even with no stream events
        heartbeat = self._start_typing_heartbeat(chat)

        success = True
        # Build AskUserQuestion hook for this request
        auq_hooks = self._build_auq_hook(
            bot=context.bot, chat_id=chat.id, user_id=user_id
        )
        plan_hooks = self._build_plan_mode_hook(
            bot=context.bot, chat_id=chat.id, user_id=user_id
        )
        merged_hooks: Dict[str, Any] = {}
        for key in set(list(auq_hooks.keys()) + list(plan_hooks.keys())):
            merged_hooks[key] = auq_hooks.get(key, []) + plan_hooks.get(key, [])
        try:
            claude_response = await claude_integration.run_command(
                prompt=message_text,
                working_directory=current_dir,
                user_id=user_id,
                session_id=session_id,
                on_stream=on_stream,
                force_new=force_new,
                interrupt_event=interrupt_event,
                hooks=merged_hooks,
                permission_mode=context.user_data.get("permission_mode"),
            )

            # New session created successfully — clear the one-shot flag
            if force_new:
                context.user_data["force_new_session"] = False

            context.user_data["claude_session_id"] = claude_response.session_id
            context.user_data["last_usage"] = claude_response.usage
            context.user_data["last_model_usage"] = getattr(
                claude_response, "model_usage", None
            )

            # Track directory changes
            from .handlers.message import _update_working_directory_from_claude_response

            _update_working_directory_from_claude_response(
                claude_response, context, self.settings, user_id
            )

            # Store interaction
            storage = context.bot_data.get("storage")
            if storage:
                try:
                    await storage.save_claude_interaction(
                        user_id=user_id,
                        session_id=claude_response.session_id,
                        prompt=message_text,
                        response=claude_response,
                        ip_address=None,
                    )
                except Exception as e:
                    logger.warning("Failed to log interaction", error=str(e))

            # Format response (no reply_markup — strip keyboards)
            from .utils.formatting import ResponseFormatter

            formatter = ResponseFormatter(self.settings)
            # Apply per-user rich messages override
            formatter.use_rich_messages = rich_enabled
            formatter.max_message_length = (
                32768 if rich_enabled else formatter.max_message_length
            )

            response_content = claude_response.content
            if claude_response.interrupted:
                response_content = (response_content or "") + "\n\n_（用户已中断）_"

            formatted_messages = formatter.format_claude_response(response_content)

        except Exception as e:
            success = False
            logger.error("Claude integration failed", error=str(e), user_id=user_id)
            from .handlers.message import _format_error_message
            from .utils.formatting import FormattedMessage

            formatted_messages = [
                FormattedMessage(_format_error_message(e), parse_mode="HTML")
            ]
        finally:
            heartbeat.cancel()
            self._active_requests.pop(user_id, None)
            if draft_streamer:
                try:
                    await draft_streamer.flush()
                except Exception:
                    logger.debug("Draft flush failed in finally block", user_id=user_id)

        for msg in await active_request.all_progress_messages():
            try:
                await msg.delete()
            except Exception:
                logger.debug("Failed to delete progress message, ignoring")

        # Use MCP-collected images (from send_image_to_user tool calls)
        images: List[ImageAttachment] = mcp_images

        # Try to combine text + images in one message when possible
        caption_sent = False
        if images and len(formatted_messages) == 1:
            msg = formatted_messages[0]
            if msg.text and len(msg.text) <= 1024:
                try:
                    caption_sent = await self._send_images(
                        update,
                        images,
                        reply_to_message_id=update.message.message_id,
                        caption=msg.text,
                        caption_parse_mode=msg.parse_mode,
                    )
                except Exception as img_err:
                    logger.warning("Image+caption send failed", error=str(img_err))

        # Send text messages (skip if caption was already embedded in photos)
        if not caption_sent:
            for i, message in enumerate(formatted_messages):
                if not message.text or not message.text.strip():
                    continue
                try:
                    if rich_enabled:
                        await self._send_rich_message_with_fallback(
                            update,
                            message,
                            reply_to_message_id=(
                                update.message.message_id if i == 0 else None
                            ),
                        )
                    else:
                        await update.message.reply_text(
                            message.text,
                            parse_mode=message.parse_mode,
                            reply_markup=None,  # No keyboards in agentic mode
                            reply_to_message_id=(
                                update.message.message_id if i == 0 else None
                            ),
                        )
                    if i < len(formatted_messages) - 1:
                        await asyncio.sleep(0.5)
                except Exception as send_err:
                    logger.warning(
                        "发送 HTML 响应失败，尝试纯文本重发",
                        error=str(send_err),
                        message_index=i,
                    )
                    try:
                        await update.message.reply_text(
                            message.text,
                            reply_markup=None,
                            reply_to_message_id=(
                                update.message.message_id if i == 0 else None
                            ),
                        )
                    except Exception as plain_err:
                        await update.message.reply_text(
                            f"响应发送失败"
                            f"（Telegram 错误：{str(plain_err)[:150]}），"
                            f"请重试。",
                            reply_to_message_id=(
                                update.message.message_id if i == 0 else None
                            ),
                        )

            # Send images separately if caption wasn't used
            if images:
                try:
                    await self._send_images(
                        update,
                        images,
                        reply_to_message_id=update.message.message_id,
                    )
                except Exception as img_err:
                    logger.warning("Image send failed", error=str(img_err))

        # Audit log
        audit_logger = context.bot_data.get("audit_logger")
        if audit_logger:
            await audit_logger.log_command(
                user_id=user_id,
                command="text_message",
                args=[message_text[:100]],
                success=success,
            )

    async def agentic_document(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Process file upload -> Claude, minimal chrome."""
        user_id = update.effective_user.id
        document = update.message.document

        logger.info(
            "Agentic document upload",
            user_id=user_id,
            filename=document.file_name,
        )

        # Security validation
        security_validator = context.bot_data.get("security_validator")
        if security_validator:
            valid, error = security_validator.validate_filename(document.file_name)
            if not valid:
                await update.message.reply_text(f"文件被拒绝：{error}")
                return

        # Size check
        max_size = 10 * 1024 * 1024
        if document.file_size > max_size:
            await update.message.reply_text(
                f"文件过大（{document.file_size / 1024 / 1024:.1f}MB），上限 10MB。"
            )
            return

        chat = update.message.chat
        await chat.send_action("typing")
        progress_msg = await update.message.reply_text("处理中...")

        # Try enhanced file handler, fall back to basic
        features = context.bot_data.get("features")
        file_handler = features.get_file_handler() if features else None
        prompt: Optional[str] = None

        if file_handler:
            try:
                processed_file = await file_handler.handle_document_upload(
                    document,
                    user_id,
                    update.message.caption or "请审查此文件：",
                )
                prompt = processed_file.prompt
            except Exception:
                file_handler = None

        if not file_handler:
            file = await document.get_file()
            file_bytes = await file.download_as_bytearray()
            caption = update.message.caption or "请审查此文件："

            # Determine save directory from config
            cfg = context.bot_data.get("config")
            save_dir = Path(
                cfg.approved_directory
                if hasattr(cfg, "approved_directory")
                else "."
            ) / ".tmp" / "uploads"
            save_dir.mkdir(parents=True, exist_ok=True)

            is_text = True
            content = ""
            try:
                content = file_bytes.decode("utf-8")
            except UnicodeDecodeError:
                is_text = False

            # Small text file → embed in prompt
            if is_text and len(content) <= 50000:
                prompt = (
                    f"{caption}\n\n**File:** `{document.file_name}`\n\n"
                    f"```\n{content}\n```"
                )
            else:
                # Large text or binary → save to disk, let Claude read via tools
                save_path = save_dir / document.file_name
                save_path.write_bytes(bytes(file_bytes))
                kind = "text" if is_text else "binary"
                size_kb = len(file_bytes) / 1024
                prompt = (
                    f"{caption}\n\n**File:** `{document.file_name}` "
                    f"({kind}, {size_kb:.0f}KB)\n"
                    f"Saved to: `{save_path}`"
                )

        # Process with Claude
        claude_integration = context.bot_data.get("claude_integration")
        if not claude_integration:
            await progress_msg.edit_text("Claude 集成不可用，请检查配置。")
            return

        current_dir = context.user_data.get(
            "current_directory", self.settings.approved_directory
        )
        session_id = context.user_data.get("claude_session_id")

        # Check if /new was used — skip auto-resume for this first message.
        # Flag is only cleared after a successful run so retries keep the intent.
        force_new = bool(context.user_data.get("force_new_session"))

        verbose_level = self._get_verbose_level(context)
        tool_log: List[Dict[str, Any]] = []
        mcp_images_doc: List[ImageAttachment] = []
        on_stream = self._make_stream_callback(
            verbose_level,
            progress_msg,
            tool_log,
            time.time(),
            mcp_images=mcp_images_doc,
            approved_directory=self.settings.approved_directory,
        )

        heartbeat = self._start_typing_heartbeat(chat)
        # Build AskUserQuestion hook for this request
        auq_hooks = self._build_auq_hook(
            bot=context.bot, chat_id=chat.id, user_id=user_id
        )
        plan_hooks = self._build_plan_mode_hook(
            bot=context.bot, chat_id=chat.id, user_id=user_id
        )
        merged_hooks: Dict[str, Any] = {}
        for key in set(list(auq_hooks.keys()) + list(plan_hooks.keys())):
            merged_hooks[key] = auq_hooks.get(key, []) + plan_hooks.get(key, [])
        try:
            claude_response = await claude_integration.run_command(
                prompt=prompt,
                working_directory=current_dir,
                user_id=user_id,
                session_id=session_id,
                on_stream=on_stream,
                force_new=force_new,
                hooks=merged_hooks,
                permission_mode=context.user_data.get("permission_mode"),
            )

            if force_new:
                context.user_data["force_new_session"] = False

            context.user_data["claude_session_id"] = claude_response.session_id

            from .handlers.message import _update_working_directory_from_claude_response

            _update_working_directory_from_claude_response(
                claude_response, context, self.settings, user_id
            )

            from .utils.formatting import ResponseFormatter

            formatter = ResponseFormatter(self.settings)
            formatted_messages = formatter.format_claude_response(
                claude_response.content
            )

            try:
                await progress_msg.delete()
            except Exception:
                logger.debug("Failed to delete progress message, ignoring")

            # Use MCP-collected images (from send_image_to_user tool calls)
            images: List[ImageAttachment] = mcp_images_doc

            caption_sent = False
            if images and len(formatted_messages) == 1:
                msg = formatted_messages[0]
                if msg.text and len(msg.text) <= 1024:
                    try:
                        caption_sent = await self._send_images(
                            update,
                            images,
                            reply_to_message_id=update.message.message_id,
                            caption=msg.text,
                            caption_parse_mode=msg.parse_mode,
                        )
                    except Exception as img_err:
                        logger.warning("Image+caption send failed", error=str(img_err))

            if not caption_sent:
                for i, message in enumerate(formatted_messages):
                    await update.message.reply_text(
                        message.text,
                        parse_mode=message.parse_mode,
                        reply_markup=None,
                        reply_to_message_id=(
                            update.message.message_id if i == 0 else None
                        ),
                    )
                    if i < len(formatted_messages) - 1:
                        await asyncio.sleep(0.5)

                if images:
                    try:
                        await self._send_images(
                            update,
                            images,
                            reply_to_message_id=update.message.message_id,
                        )
                    except Exception as img_err:
                        logger.warning("Image send failed", error=str(img_err))

        except Exception as e:
            from .handlers.message import _format_error_message

            await progress_msg.edit_text(_format_error_message(e), parse_mode="HTML")
            logger.error("Claude file processing failed", error=str(e), user_id=user_id)
        finally:
            heartbeat.cancel()

    async def agentic_photo(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Process photo -> Claude, minimal chrome."""
        user_id = update.effective_user.id

        features = context.bot_data.get("features")
        image_handler = features.get_image_handler() if features else None

        if not image_handler:
            await update.message.reply_text("图片处理功能不可用。")
            return

        chat = update.message.chat
        await chat.send_action("typing")
        progress_msg = await update.message.reply_text("处理中...")

        try:
            photo = update.message.photo[-1]
            processed_image = await image_handler.process_image(
                photo, update.message.caption
            )
            fmt = processed_image.metadata.get("format", "png")
            images = [
                {
                    "data": processed_image.base64_data,
                    "media_type": _MEDIA_TYPE_MAP.get(fmt, "image/png"),
                }
            ]

            await self._handle_agentic_media_message(
                update=update,
                context=context,
                prompt=processed_image.prompt,
                progress_msg=progress_msg,
                user_id=user_id,
                chat=chat,
                images=images,
            )

        except Exception as e:
            from .handlers.message import _format_error_message

            await progress_msg.edit_text(_format_error_message(e), parse_mode="HTML")
            logger.error(
                "Claude photo processing failed", error=str(e), user_id=user_id
            )

    async def agentic_voice(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Transcribe voice message -> Claude, minimal chrome."""
        user_id = update.effective_user.id

        features = context.bot_data.get("features")
        voice_handler = features.get_voice_handler() if features else None

        if not voice_handler:
            await update.message.reply_text(self._voice_unavailable_message())
            return

        chat = update.message.chat
        await chat.send_action("typing")
        progress_msg = await update.message.reply_text("转录中...")

        try:
            voice = update.message.voice
            processed_voice = await voice_handler.process_voice_message(
                voice, update.message.caption
            )

            await progress_msg.edit_text("处理中...")
            await self._handle_agentic_media_message(
                update=update,
                context=context,
                prompt=processed_voice.prompt,
                progress_msg=progress_msg,
                user_id=user_id,
                chat=chat,
            )

        except Exception as e:
            from .handlers.message import _format_error_message

            await progress_msg.edit_text(_format_error_message(e), parse_mode="HTML")
            logger.error(
                "Claude voice processing failed", error=str(e), user_id=user_id
            )

    async def _handle_agentic_media_message(
        self,
        *,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        prompt: str,
        progress_msg: Any,
        user_id: int,
        chat: Any,
        images: Optional[List[Dict[str, str]]] = None,
    ) -> None:
        """Run a media-derived prompt through Claude and send responses."""
        claude_integration = context.bot_data.get("claude_integration")
        if not claude_integration:
            await progress_msg.edit_text("Claude 集成不可用，请检查配置。")
            return

        current_dir = context.user_data.get(
            "current_directory", self.settings.approved_directory
        )
        session_id = context.user_data.get("claude_session_id")
        force_new = bool(context.user_data.get("force_new_session"))

        verbose_level = self._get_verbose_level(context)
        tool_log: List[Dict[str, Any]] = []
        mcp_images_media: List[ImageAttachment] = []
        on_stream = self._make_stream_callback(
            verbose_level,
            progress_msg,
            tool_log,
            time.time(),
            mcp_images=mcp_images_media,
            approved_directory=self.settings.approved_directory,
        )

        heartbeat = self._start_typing_heartbeat(chat)
        # Build AskUserQuestion hook for this request
        auq_hooks = self._build_auq_hook(
            bot=context.bot, chat_id=chat.id, user_id=user_id
        )
        plan_hooks = self._build_plan_mode_hook(
            bot=context.bot, chat_id=chat.id, user_id=user_id
        )
        merged_hooks: Dict[str, Any] = {}
        for key in set(list(auq_hooks.keys()) + list(plan_hooks.keys())):
            merged_hooks[key] = auq_hooks.get(key, []) + plan_hooks.get(key, [])
        try:
            claude_response = await claude_integration.run_command(
                prompt=prompt,
                working_directory=current_dir,
                user_id=user_id,
                session_id=session_id,
                on_stream=on_stream,
                force_new=force_new,
                images=images,
                hooks=merged_hooks,
                permission_mode=context.user_data.get("permission_mode"),
            )
        finally:
            heartbeat.cancel()

        if force_new:
            context.user_data["force_new_session"] = False

        context.user_data["claude_session_id"] = claude_response.session_id

        from .handlers.message import _update_working_directory_from_claude_response

        _update_working_directory_from_claude_response(
            claude_response, context, self.settings, user_id
        )

        from .utils.formatting import ResponseFormatter

        formatter = ResponseFormatter(self.settings)
        formatted_messages = formatter.format_claude_response(claude_response.content)

        try:
            await progress_msg.delete()
        except Exception:
            logger.debug("Failed to delete progress message, ignoring")

        # Use MCP-collected images (from send_image_to_user tool calls).
        images: List[ImageAttachment] = mcp_images_media

        caption_sent = False
        if images and len(formatted_messages) == 1:
            msg = formatted_messages[0]
            if msg.text and len(msg.text) <= 1024:
                try:
                    caption_sent = await self._send_images(
                        update,
                        images,
                        reply_to_message_id=update.message.message_id,
                        caption=msg.text,
                        caption_parse_mode=msg.parse_mode,
                    )
                except Exception as img_err:
                    logger.warning("Image+caption send failed", error=str(img_err))

        if not caption_sent:
            for i, message in enumerate(formatted_messages):
                if not message.text or not message.text.strip():
                    continue
                await update.message.reply_text(
                    message.text,
                    parse_mode=message.parse_mode,
                    reply_markup=None,
                    reply_to_message_id=(update.message.message_id if i == 0 else None),
                )
                if i < len(formatted_messages) - 1:
                    await asyncio.sleep(0.5)

            if images:
                try:
                    await self._send_images(
                        update,
                        images,
                        reply_to_message_id=update.message.message_id,
                    )
                except Exception as img_err:
                    logger.warning("Image send failed", error=str(img_err))

    async def _handle_unknown_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Forward unknown slash commands to Claude in agentic mode.

        Known commands are handled by their own CommandHandlers (group 0);
        this handler fires for *every* COMMAND message in group 10 but
        returns immediately when the command is registered, preventing
        double execution.
        """
        msg = update.effective_message
        if not msg or not msg.text:
            return
        cmd = msg.text.split()[0].lstrip("/").split("@")[0].lower()
        if cmd in self._known_commands:
            return  # let the registered CommandHandler take care of it
        # Forward unrecognised /commands to Claude as natural language
        await self.agentic_text(update, context)

    def _voice_unavailable_message(self) -> str:
        """Return provider-aware guidance when voice feature is unavailable."""
        if self.settings.voice_provider == "local":
            return (
                "语音处理功能不可用。"
                "请确保 whisper.cpp 已安装且模型文件存在，"
                "检查 WHISPER_CPP_BINARY_PATH 和 WHISPER_CPP_MODEL_PATH 配置。"
            )
        return (
            "语音处理功能不可用。"
            f"请设置 {self.settings.voice_provider_api_key_env} "
            f"（用于 {self.settings.voice_provider_display_name}）并安装语音依赖："
            'pip install "claude-code-telegram[voice]"'
        )

    async def agentic_repo(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Browse repos in workspace with recursive navigation.

        /repo              -- browse top-level directories
        /repo <path>       -- switch to that path directly (supports deep paths)
        """
        args = update.message.text.split()[1:] if update.message.text else []
        base = self.settings.approved_directory

        if args:
            # /repo <path> -- direct switch, supports deep paths
            target_path = (base / args[0]).resolve()
            if not self._is_within(target_path, base) or not target_path.is_dir():
                await update.message.reply_text(
                    f"目录不存在：" f"<code>{escape_html(args[0])}</code>",
                    parse_mode="HTML",
                )
                return
            await self._repo_switch(update, context, target_path, from_command=True)
            return

        # /repo -- browse top-level
        await self._repo_browse(update, context, base)

    # ------------------------------------------------------------------ #
    #  /repo recursive browsing helpers                                    #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _repo_list_entries(browse_path: Path) -> List[Path]:
        """Return sorted non-hidden subdirectories of browse_path."""
        try:
            return sorted(
                [
                    d
                    for d in browse_path.iterdir()
                    if d.is_dir() and not d.name.startswith(".")
                ],
                key=lambda d: d.name,
            )
        except OSError:
            return []

    async def _repo_browse(
        self,
        source: Any,
        context: ContextTypes.DEFAULT_TYPE,
        browse_path: Path,
        *,
        is_edit: bool = False,
    ) -> None:
        """Show directory listing with browse/use/back buttons.

        Args:
            source: Update (from /repo command) or CallbackQuery (button).
            context: PTB context.
            browse_path: Absolute path to list.
            is_edit: True to edit existing message, False to send new.
        """
        base = self.settings.approved_directory
        entries = self._repo_list_entries(browse_path)

        rel = browse_path.relative_to(base)
        rel_str = str(rel).replace(chr(92), "/") if str(rel) != "." else ""
        display_path = rel_str if rel_str else "workspace"

        lines_list: List[str] = [
            f"\U0001f4c2 <b>浏览: " f"{escape_html(display_path)}/</b>\n"
        ]
        if not entries:
            lines_list.append("<i>（空目录）</i>")
        else:
            for d in entries:
                is_git = (d / ".git").is_dir()
                icon = "\U0001f4e6" if is_git else "\U0001f4c1"
                lines_list.append(f"{icon} <code>{escape_html(d.name)}/</code>")
        text = "\n".join(lines_list)

        # Build directory buttons
        buttons: List[InlineKeyboardButton] = []
        for d in entries:
            child_rel = f"{rel_str}/{d.name}" if rel_str else d.name
            cb = f"repo:{child_rel}"
            if len(cb.encode("utf-8")) > 64:
                cb = f"repo_use:{child_rel}"
            buttons.append(InlineKeyboardButton(d.name, callback_data=cb))

        # Action row: "use this path" + optional "back"
        action_row: List[InlineKeyboardButton] = [
            InlineKeyboardButton(
                "\U0001f4c2 使用此路径",
                callback_data=f"repo_use:{rel_str}",
            )
        ]
        if rel_str:
            parent = str(rel.parent).replace(chr(92), "/") if str(rel) != "." else ""
            action_row.append(
                InlineKeyboardButton(
                    "⬅️ 返回",
                    callback_data=f"repo_back:{parent}",
                )
            )

        reply_markup = InlineKeyboardMarkup(
            self._chunk_buttons(buttons, 2) + [action_row]
        )

        if is_edit:
            await source.edit_message_text(
                text, parse_mode="HTML", reply_markup=reply_markup
            )
        else:
            await source.message.reply_text(
                text, parse_mode="HTML", reply_markup=reply_markup
            )

    async def _repo_switch(
        self,
        source: Any,
        context: ContextTypes.DEFAULT_TYPE,
        target_path: Path,
        *,
        from_command: bool = False,
    ) -> None:
        """Switch to target_path, find resumable session, show result."""
        base = self.settings.approved_directory
        display = str(target_path.relative_to(base)).replace(chr(92), "/")
        context.user_data["current_directory"] = target_path

        # Try to find a resumable session
        claude_integration = context.bot_data.get("claude_integration")
        session_id: Optional[str] = None
        existing_session = None
        user_id = (
            source.from_user.id
            if hasattr(source, "from_user")
            else source.effective_user.id
        )
        if claude_integration:
            existing_session = await claude_integration._find_resumable_session(
                user_id, target_path
            )
            if existing_session:
                session_id = existing_session.session_id
        context.user_data["claude_session_id"] = session_id

        is_git = (target_path / ".git").is_dir()
        git_badge = " (git)" if is_git else ""
        switch_msg = f"已切换到 " f"<code>{escape_html(display)}/</code>{git_badge}"

        if session_id and claude_integration:
            resume_text = await self._build_session_resume_text(
                session_id,
                target_path,
                claude_integration,
                session_meta=existing_session,
            )
            full_msg = f"{switch_msg}\n\n{resume_text}"
        else:
            full_msg = switch_msg

        if from_command:
            await source.message.reply_text(full_msg, parse_mode="HTML")
        else:
            await source.edit_message_text(full_msg, parse_mode="HTML")

        # Audit log
        audit_logger = context.bot_data.get("audit_logger")
        if audit_logger:
            await audit_logger.log_command(
                user_id=user_id,
                command="repo",
                args=[display],
                success=True,
            )

    # -- repo callback handlers ---------------------------------------- #

    async def _handle_repo_browse_cb(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle repo:<path> callbacks -- show subdirectory listing."""
        query = update.callback_query
        await query.answer()

        raw = query.data[len("repo:") :]
        base = self.settings.approved_directory
        target = (base / raw).resolve() if raw else base

        if not self._is_within(target, base) or not target.is_dir():
            await query.edit_message_text(
                f"目录不存在：" f"<code>{escape_html(raw)}</code>",
                parse_mode="HTML",
            )
            return

        # Leaf directory (no subdirectories) -- switch directly
        if not self._repo_list_entries(target):
            await self._repo_switch(query, context, target)
            return

        await self._repo_browse(query, context, target, is_edit=True)

    async def _handle_repo_use_cb(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle repo_use:<path> callbacks -- switch to that directory."""
        query = update.callback_query
        await query.answer()

        raw = query.data[len("repo_use:") :]
        base = self.settings.approved_directory
        target = (base / raw).resolve() if raw else base

        if not self._is_within(target, base) or not target.is_dir():
            await query.edit_message_text(
                f"目录不存在：" f"<code>{escape_html(raw)}</code>",
                parse_mode="HTML",
            )
            return

        await self._repo_switch(query, context, target)

    async def _handle_repo_back_cb(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle repo_back:<path> callbacks -- navigate back to parent."""
        query = update.callback_query
        await query.answer()

        raw = query.data[len("repo_back:") :]
        base = self.settings.approved_directory
        target = (base / raw).resolve() if raw else base

        if not self._is_within(target, base):
            target = base

        await self._repo_browse(query, context, target, is_edit=True)

    @staticmethod
    def _chunk_buttons(
        buttons: List[InlineKeyboardButton], per_row: int
    ) -> List[List[InlineKeyboardButton]]:
        """Split button list into rows of ``per_row`` buttons."""
        return [buttons[i : i + per_row] for i in range(0, len(buttons), per_row)]

    async def _handle_stop_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle stop: callbacks — interrupt a running Claude request."""
        query = update.callback_query
        target_user_id = int(query.data.split(":", 1)[1])

        # Only the requesting user can stop their own request
        if query.from_user.id != target_user_id:
            await query.answer("只有发起请求的用户才能停止。", show_alert=True)
            return

        active = self._active_requests.get(target_user_id)
        if not active:
            await query.answer("已完成。", show_alert=False)
            return
        if active.interrupted:
            await query.answer("正在停止...", show_alert=False)
            return

        active.interrupt_event.set()
        active.interrupted = True
        await query.answer("正在停止...", show_alert=False)

        try:
            await active.progress_msg.edit_text("正在停止...", reply_markup=None)
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    #  AskUserQuestion → Telegram inline keyboard                         #
    # ------------------------------------------------------------------ #

    def _build_auq_hook(self, bot: Any, chat_id: int, user_id: int) -> Dict[str, Any]:
        """Build a PreToolUse hook dict for AskUserQuestion.

        Returns a dict suitable for passing as ``hooks`` to
        ``ClaudeIntegration.run_command()``.
        """
        from claude_agent_sdk import HookMatcher  # type: ignore[import-untyped]

        orchestrator_ref = self  # capture for closure
        hook_timeout = self._effective_hook_timeout_seconds()
        matcher_timeout = hook_timeout + _HOOK_TIMEOUT_SDK_GRACE_SECONDS

        async def _auq_hook(
            hook_input: Any, stdin: Any = None, context: Any = None
        ) -> dict:
            tool_input = hook_input.get("tool_input", {})
            tool_use_id = hook_input.get("tool_use_id", "unknown")
            questions = tool_input.get("questions", [])

            if not questions:
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": (
                            "No questions provided in AskUserQuestion call"
                        ),
                    }
                }

            # Take the first question (AskUserQuestion sends one at a time)
            q = questions[0]
            question_text = q.get("question", "")
            options = q.get("options", [])
            multi_select = q.get("multiSelect", False)
            header = q.get("header", "")

            if not options:
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": (
                            "No options provided in AskUserQuestion call"
                        ),
                    }
                }

            # Truncate tool_use_id for Telegram's 64-byte callback_data limit
            tid_short = tool_use_id[:16]

            # Create Future for hook↔callback communication
            future: asyncio.Future = asyncio.get_event_loop().create_future()
            orchestrator_ref._pending_auq[tool_use_id] = future

            try:
                # Send Telegram message with buttons
                await orchestrator_ref._send_auq_message(
                    bot=bot,
                    chat_id=chat_id,
                    user_id=user_id,
                    question_text=question_text,
                    options=options,
                    multi_select=multi_select,
                    header=header,
                    tid_short=tid_short,
                    tool_use_id=tool_use_id,
                )

                result = await asyncio.wait_for(future, timeout=hook_timeout)

                selected = result.get("selected", [])
                answer_str = ", ".join(f"'{s}'" for s in selected)

                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": (
                            f"User selected via Telegram: {answer_str}"
                        ),
                        "additionalContext": (
                            "The AskUserQuestion tool was answered via Telegram. "
                            f"Question: {question_text}. Selected answer(s): "
                            f"{answer_str}."
                        ),
                    }
                }
            except asyncio.CancelledError:
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": ("AskUserQuestion was cancelled"),
                    }
                }
            except asyncio.TimeoutError:
                await orchestrator_ref._expire_auq_message(tid_short, tool_use_id)
                orchestrator_ref._interrupt_active_request_after_hook_timeout(
                    user_id,
                    "AskUserQuestion timed out waiting for Telegram answer",
                )
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": (
                            "AskUserQuestion timed out waiting for Telegram answer"
                        ),
                    }
                }
            except Exception as exc:
                logger.error("AskUserQuestion hook error", error=str(exc))
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": (
                            f"Error processing question: {exc}"
                        ),
                    }
                }
            finally:
                orchestrator_ref._pending_auq.pop(tool_use_id, None)

        return {
            "PreToolUse": [
                HookMatcher(
                    matcher="AskUserQuestion",
                    hooks=[_auq_hook],
                    timeout=matcher_timeout,
                )
            ]
        }

    @staticmethod
    def _read_latest_plan_file() -> Optional[str]:
        """Read the most recently modified plan file from ~/.claude/plans/.

        Returns the file content as a string, or None if no plan files found.
        """
        plans_dir = Path.home() / ".claude" / "plans"
        if not plans_dir.is_dir():
            return None
        try:
            plan_files = sorted(
                plans_dir.glob("*.md"),
                key=lambda f: f.stat().st_mtime,
                reverse=True,
            )
            if not plan_files:
                return None
            content = plan_files[0].read_text(encoding="utf-8").strip()
            return content if content else None
        except OSError:
            return None

    def _build_plan_mode_hook(
        self, bot: Any, chat_id: int, user_id: int
    ) -> Dict[str, Any]:
        """Build PreToolUse hooks for EnterPlanMode / ExitPlanMode.

        EnterPlanMode: sends Telegram buttons for user approval.
        ExitPlanMode: sends plan content + buttons for user approval.
        """
        from claude_agent_sdk import HookMatcher  # type: ignore[import-untyped]

        orchestrator_ref = self
        hook_timeout = self._effective_hook_timeout_seconds()
        matcher_timeout = hook_timeout + _HOOK_TIMEOUT_SDK_GRACE_SECONDS

        def _load_plan_content(hook_input: Any) -> Optional[str]:
            """Extract plan text from hook input or read from file."""
            # Always prefer reading the latest plan file from ~/.claude/plans/
            content = orchestrator_ref._read_latest_plan_file()
            if content:
                return content

            # Fallback: check if tool_input has inline plan content
            tool_input = (
                hook_input.get("tool_input", {}) if isinstance(hook_input, dict) else {}
            )
            plan = tool_input.get("planContent") or tool_input.get("plan")
            if plan:
                plan_str = str(plan).strip()
                plan_path = Path(plan_str)
                if plan_path.is_file():
                    try:
                        return plan_path.read_text(encoding="utf-8").strip()
                    except OSError:
                        pass
                # Only return as inline content if it's long enough
                # (short strings are likely file paths, not content)
                if len(plan_str) > 200:
                    return plan_str

            return None

        async def _enter_plan_hook(
            hook_input: Any, stdin: Any = None, hook_context: Any = None
        ) -> dict:
            future: asyncio.Future = asyncio.get_event_loop().create_future()
            orchestrator_ref._pending_plan[user_id] = future

            try:
                kb = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "✅ 进入规划",
                                callback_data=f"plan:{user_id}:enter_approve",
                            ),
                            InlineKeyboardButton(
                                "❌ 取消",
                                callback_data=f"plan:{user_id}:enter_deny",
                            ),
                        ]
                    ]
                )
                prompt_msg = await bot.send_message(
                    chat_id=chat_id,
                    text=(
                        "📋 <b>Claude 请求进入规划模式</b>\n\n"
                        "• 只读分析，不修改文件\n"
                        "• 适合先看方案再决定是否执行"
                    ),
                    parse_mode="HTML",
                    reply_markup=kb,
                )

                result = await asyncio.wait_for(future, timeout=hook_timeout)
                action = result.get("action", "deny")

                if action == "approve":
                    return {
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "allow",
                        }
                    }
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": "用户拒绝进入规划模式",
                    }
                }
            except asyncio.CancelledError:
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": "EnterPlanMode 已取消",
                    }
                }
            except asyncio.TimeoutError:
                await orchestrator_ref._expire_plan_message(
                    prompt_msg,
                    "⏰ 进入规划模式请求已超时，未批准。",
                )
                orchestrator_ref._interrupt_active_request_after_hook_timeout(
                    user_id,
                    "EnterPlanMode timed out waiting for Telegram approval",
                )
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": "EnterPlanMode 等待 Telegram 确认超时",
                    }
                }
            except Exception as exc:
                logger.error("EnterPlanMode hook error", error=str(exc))
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": f"处理出错：{exc}",
                    }
                }
            finally:
                orchestrator_ref._pending_plan.pop(user_id, None)

        async def _exit_plan_hook(
            hook_input: Any, stdin: Any = None, hook_context: Any = None
        ) -> dict:
            future = asyncio.get_event_loop().create_future()
            orchestrator_ref._pending_plan[user_id] = future
            prompt_msg = None
            try:
                kb = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "✅ 允许执行",
                                callback_data=f"plan:{user_id}:exit_approve",
                            ),
                            InlineKeyboardButton(
                                "❌ 不执行",
                                callback_data=f"plan:{user_id}:exit_deny",
                            ),
                        ],
                        [
                            InlineKeyboardButton(
                                "✏️ 告诉 Claude 如何修改",
                                callback_data=f"plan:{user_id}:exit_feedback",
                            ),
                        ],
                    ]
                )

                # Try to load plan content for display
                plan_text = _load_plan_content(hook_input)

                if plan_text:
                    # Send plan as a Markdown document via BytesIO (no temp file)
                    import io

                    plan_bytes = plan_text.encode("utf-8")

                    # Derive filename from plan title (first # heading)
                    doc_name = "plan.md"
                    for line in plan_text.splitlines():
                        line = line.strip()
                        if line.startswith("# "):
                            title = line[2:].strip()
                            safe = re.sub(r"[^\w\s一-鿿-]", "", title)
                            safe = re.sub(r"\s+", "-", safe).strip("-")
                            if safe:
                                doc_name = f"{safe[:50]}.md"
                            break

                    prompt_msg = await bot.send_document(
                        chat_id=chat_id,
                        document=io.BytesIO(plan_bytes),
                        filename=doc_name,
                        caption="📋 Claude 请求退出规划模式，请审阅方案。",
                        reply_markup=kb,
                    )
                else:
                    prompt_msg = await bot.send_message(
                        chat_id=chat_id,
                        text=(
                            "📋 <b>Claude 请求退出规划模式</b>\n\n"
                            "方案已完成，准备开始执行。"
                        ),
                        parse_mode="HTML",
                        reply_markup=kb,
                    )
                result = await asyncio.wait_for(future, timeout=hook_timeout)
                action = result.get("action", "deny")
                if action == "approve":
                    return {
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "allow",
                        }
                    }
                if action == "feedback":
                    return {
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "deny",
                            "permissionDecisionReason": "用户要求修改方案",
                            "additionalContext": (
                                f"用户对方案的修改意见：{result['feedback']}"
                            ),
                        }
                    }
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "allow",
                        "additionalContext": (
                            "用户不想执行这个方案，请不要执行任何修改。"
                            "等待用户的进一步指示。"
                        ),
                    }
                }
            except asyncio.CancelledError:
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": "ExitPlanMode 已取消",
                    }
                }
            except asyncio.TimeoutError:
                await orchestrator_ref._expire_plan_message(
                    prompt_msg,
                    "⏰ 退出规划模式请求已超时，未批准执行计划。",
                )
                orchestrator_ref._interrupt_active_request_after_hook_timeout(
                    user_id,
                    "ExitPlanMode timed out waiting for Telegram approval",
                )
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": "ExitPlanMode 等待 Telegram 确认超时",
                    }
                }
            except Exception as exc:
                logger.error("ExitPlanMode hook error", error=str(exc))
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": f"处理出错：{exc}",
                    }
                }
            finally:
                orchestrator_ref._pending_plan.pop(user_id, None)

        return {
            "PreToolUse": [
                HookMatcher(
                    matcher="EnterPlanMode",
                    hooks=[_enter_plan_hook],
                    timeout=matcher_timeout,
                ),
                HookMatcher(
                    matcher="ExitPlanMode",
                    hooks=[_exit_plan_hook],
                    timeout=matcher_timeout,
                ),
            ]
        }

    def _effective_hook_timeout_seconds(self) -> float:
        timeout = getattr(self.settings, "effective_claude_hook_timeout_seconds", None)
        if isinstance(timeout, (int, float)) and timeout > 0:
            return float(timeout)
        return 24 * 60 * 60

    def _interrupt_active_request_after_hook_timeout(
        self, user_id: int, reason: str
    ) -> None:
        active_request = self._active_requests.get(user_id)
        if active_request is None:
            return
        active_request.interrupted = True
        asyncio.get_running_loop().call_soon(active_request.interrupt_event.set)
        logger.info(
            "Hook timeout interrupted active request",
            user_id=user_id,
            reason=reason,
        )

    async def _expire_plan_message(self, msg: Any, text: str) -> None:
        if msg is None:
            return
        try:
            await msg.edit_caption(caption=text, reply_markup=None)
            return
        except Exception:
            pass
        try:
            await msg.edit_text(text, reply_markup=None)
            return
        except Exception:
            pass
        try:
            await msg.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

    async def _handle_plan_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle EnterPlanMode / ExitPlanMode button callbacks."""
        query = update.callback_query
        if not query:
            return
        await query.answer()

        data = query.data  # "plan:{user_id}:{action}"
        parts = data.split(":")
        if len(parts) != 3:
            return
        _, uid_str, action = parts
        try:
            target_uid = int(uid_str)
        except ValueError:
            return

        if query.from_user.id != target_uid:
            await query.answer("只有发起请求的用户才能操作。")
            return

        future = self._pending_plan.get(target_uid)
        if not future or future.done():
            return

        # EnterPlanMode actions
        if action == "enter_approve":
            future.set_result({"action": "approve"})
            try:
                await query.edit_message_text(
                    "✅ 已进入规划模式。",
                    parse_mode="HTML",
                )
            except Exception:
                pass
        elif action == "enter_deny":
            future.set_result({"action": "deny"})
            try:
                await query.edit_message_text(
                    "❌ 已取消进入规划模式。",
                    parse_mode="HTML",
                )
            except Exception:
                pass
        # ExitPlanMode actions — edit caption + remove buttons,
        # keep the document attachment intact.
        elif action == "exit_approve":
            active_request = self._active_requests.get(target_uid)
            if active_request is not None and query.message is not None:
                stop_kb = InlineKeyboardMarkup(
                    [[InlineKeyboardButton("停止", callback_data=f"stop:{target_uid}")]]
                )
                try:
                    progress_msg = await query.message.reply_text(
                        "✅ 已允许执行最终计划，正在执行...",
                        reply_to_message_id=query.message.message_id,
                        reply_markup=stop_kb,
                    )
                    await active_request.switch_progress_msg(progress_msg)
                except Exception as exc:
                    logger.debug(
                        "Failed to create plan execution progress message",
                        user_id=target_uid,
                        error=str(exc),
                    )
            future.set_result({"action": "approve"})
            try:
                await query.edit_message_caption(
                    caption="✅ 已允许，Claude 开始执行…",
                )
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
        elif action == "exit_deny":
            future.set_result({"action": "deny"})
            try:
                await query.edit_message_caption(
                    caption="❌ 已拒绝，不执行方案。",
                )
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
        elif action == "exit_feedback":
            # Register user for free-text feedback input
            from .update_processor import StopAwareUpdateProcessor

            self._pending_plan_waiting[target_uid] = {
                "future": future,
                "query": query,
            }
            StopAwareUpdateProcessor.plan_feedback_waiting.add(target_uid)
            try:
                await query.edit_message_caption(
                    caption="✏️ 等待你的修改意见…",
                )
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass

    async def _send_auq_message(
        self,
        bot: Any,
        chat_id: int,
        user_id: int,
        question_text: str,
        options: List[Dict[str, Any]],
        multi_select: bool,
        header: str,
        tid_short: str,
        tool_use_id: str,
    ) -> None:
        """Send AskUserQuestion as Telegram inline keyboard."""
        header_line = f"<b>{escape_html(header)}</b>\n" if header else ""
        mode_hint = "\n<i>可多选，选完点「确认选择」</i>" if multi_select else ""
        text = (
            f"🤔 {header_line}<b>Claude 想问你：</b>\n"
            f"{escape_html(question_text)}{mode_hint}"
        )

        buttons: List[List[InlineKeyboardButton]] = []
        if multi_select:
            # Track selected state in user_data keyed by tool_use_id
            # Initialize all as unselected
            self._auq_multi_state: Dict[str, List[bool]] = getattr(
                self, "_auq_multi_state", {}
            )
            self._auq_multi_state[tool_use_id] = [False] * len(options)

            row: List[InlineKeyboardButton] = []
            for idx, opt in enumerate(options[:4]):
                label = opt.get("label", f"选项 {idx + 1}")
                btn_text = f"☐ {label}"
                row.append(
                    InlineKeyboardButton(
                        btn_text, callback_data=f"auq:{tid_short}:{idx}"
                    )
                )
                # 2 buttons per row for multi-select (wider buttons)
                if len(row) == 2:
                    buttons.append(row)
                    row = []
            if row:
                buttons.append(row)
            # Confirm button
            buttons.append(
                [
                    InlineKeyboardButton(
                        "✅ 确认选择",
                        callback_data=f"auq:{tid_short}:confirm",
                    )
                ]
            )
        else:
            for idx, opt in enumerate(options[:4]):
                label = opt.get("label", f"选项 {idx + 1}")
                desc = opt.get("description", "")
                btn_text = f"{label}" + (f" — {desc}" if desc else "")
                # Truncate button text to ~50 chars for readability
                if len(btn_text) > 50:
                    btn_text = btn_text[:47] + "..."
                buttons.append(
                    [
                        InlineKeyboardButton(
                            btn_text,
                            callback_data=f"auq:{tid_short}:{idx}",
                        )
                    ]
                )
            # "Other" button for free-text input
            buttons.append(
                [
                    InlineKeyboardButton(
                        "📝 其他（自由输入）",
                        callback_data=f"auq:{tid_short}:other",
                    )
                ]
            )

        reply_markup = InlineKeyboardMarkup(buttons)

        # Store message reference for later editing
        msg = await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )

        # Store msg and metadata for callback handler
        self._auq_messages: Dict[str, Dict[str, Any]] = getattr(
            self, "_auq_messages", {}
        )
        self._auq_messages[tid_short] = {
            "msg": msg,
            "chat_id": chat_id,
            "user_id": user_id,
            "options": options,
            "multi_select": multi_select,
            "question_text": question_text,
            "header": header,
            "tool_use_id": tool_use_id,
        }

    async def _expire_auq_message(self, tid_short: str, tool_use_id: str) -> None:
        meta = getattr(self, "_auq_messages", {}).pop(tid_short, None)
        getattr(self, "_auq_multi_state", {}).pop(tool_use_id, None)

        waiting_user_ids = [
            user_id
            for user_id, waiting in self._auq_waiting_other.items()
            if waiting.get("tool_use_id") == tool_use_id
        ]
        for user_id in waiting_user_ids:
            self._auq_waiting_other.pop(user_id, None)
            try:
                from .update_processor import StopAwareUpdateProcessor

                StopAwareUpdateProcessor.auq_other_waiting.discard(user_id)
            except Exception:
                pass

        if not meta:
            return

        header = meta.get("header", "")
        header_line = f"<b>{escape_html(header)}</b>\n" if header else ""
        text = (
            f"🤔 {header_line}<b>Claude 想问你：</b>\n"
            f"{escape_html(meta.get('question_text', ''))}\n\n"
            "⏰ 此问题已超时，未选择任何选项。"
        )
        msg = meta.get("msg")
        try:
            await msg.edit_text(text, parse_mode="HTML", reply_markup=None)
        except Exception:
            pass

    async def _handle_auq_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle auq: callbacks — user answered AskUserQuestion."""
        query = update.callback_query
        data = query.data  # "auq:{tid_short}:{idx_or_confirm}"

        parts = data.split(":", 2)
        if len(parts) != 3:
            await query.answer("无效的回调数据。", show_alert=True)
            return

        _, tid_short, action = parts

        # Noop callback (result display buttons) — ignore
        if action == "noop":
            await query.answer()
            return

        # Look up stored metadata
        auq_meta = getattr(self, "_auq_messages", {}).get(tid_short)
        if not auq_meta:
            await query.answer("此问题已过期。", show_alert=False)
            return

        # Only the original user can answer
        if query.from_user.id != auq_meta["user_id"]:
            await query.answer("只有原始用户才能回答此问题。", show_alert=True)
            return

        tool_use_id = auq_meta["tool_use_id"]
        future = self._pending_auq.get(tool_use_id)
        if not future or future.done():
            await query.answer("已回答。", show_alert=False)
            return

        options = auq_meta["options"]
        multi_select = auq_meta["multi_select"]

        # "Other" free-text input
        if action == "other":
            # Mark user as waiting for free-text input
            self._auq_waiting_other[query.from_user.id] = {
                "tool_use_id": tool_use_id,
                "tid_short": tid_short,
                "question_text": auq_meta["question_text"],
            }
            # Also register in the update processor so the text reply
            # bypasses the sequential lock (avoids deadlock with Claude hook).
            from .update_processor import StopAwareUpdateProcessor

            StopAwareUpdateProcessor.auq_other_waiting.add(query.from_user.id)
            # Edit message: keep question, show options, add free-text prompt
            q_text = auq_meta["question_text"]
            hdr = auq_meta.get("header", "")
            hdr_line = f"<b>{escape_html(hdr)}</b>\n" if hdr else ""
            lines = [
                f"🤔 {hdr_line}<b>Claude 想问你：</b>\n" f"{escape_html(q_text)}\n"
            ]
            for i, opt in enumerate(options[:4]):
                lbl = opt.get("label", f"选项 {i + 1}")
                desc = opt.get("description", "")
                entry = f"☐ {escape_html(lbl)}"
                if desc:
                    entry += f"\n  {escape_html(desc)}"
                lines.append(entry)
            lines.append("\n📝 <b>请输入你的回答（直接发送文字即可）：</b>")
            try:
                await query.edit_message_text(
                    "\n".join(lines),
                    parse_mode="HTML",
                    reply_markup=None,
                )
            except Exception:
                pass
            await query.answer()
            return

        if multi_select:
            # Toggle or confirm
            if action == "confirm":
                # Gather selected options
                states = getattr(self, "_auq_multi_state", {}).get(tool_use_id, [])
                selected = [
                    options[i].get("label", f"选项 {i + 1}")
                    for i in range(min(len(options), 4))
                    if i < len(states) and states[i]
                ]
                if not selected:
                    await query.answer("请至少选择一个选项。", show_alert=True)
                    return

                # Resolve future
                future.set_result({"selected": selected})

                # Edit message: keep question, add answer line + selected buttons
                question_text = auq_meta["question_text"]
                header = auq_meta.get("header", "")
                header_line = f"<b>{escape_html(header)}</b>\n" if header else ""
                # Build option list with checkmarks inline
                lines = [
                    f"🤔 {header_line}<b>Claude 想问你：</b>\n"
                    f"{escape_html(question_text)}\n"
                ]
                for i, opt in enumerate(options[:4]):
                    label = opt.get("label", f"选项 {i + 1}")
                    desc = opt.get("description", "")
                    mark = "✅" if label in selected else "☐"
                    entry = f"{mark} {escape_html(label)}"
                    if desc:
                        entry += f"\n  {escape_html(desc)}"
                    lines.append(entry)
                new_text = "\n".join(lines)
                try:
                    await query.edit_message_text(
                        new_text,
                        parse_mode="HTML",
                        reply_markup=None,
                    )
                except Exception:
                    pass
                await query.answer()

                # Cleanup
                getattr(self, "_auq_multi_state", {}).pop(tool_use_id, None)
            else:
                # Toggle a single option
                idx = int(action)
                if idx < 0 or idx >= min(len(options), 4):
                    await query.answer("无效选项。", show_alert=True)
                    return

                states = getattr(self, "_auq_multi_state", {}).get(tool_use_id, [])
                if idx < len(states):
                    states[idx] = not states[idx]

                # Rebuild keyboard with updated checkmarks
                buttons: List[List[InlineKeyboardButton]] = []
                row: List[InlineKeyboardButton] = []
                for i, opt in enumerate(options[:4]):
                    label = opt.get("label", f"选项 {i + 1}")
                    checked = "✅" if i < len(states) and states[i] else "☐"
                    row.append(
                        InlineKeyboardButton(
                            f"{checked} {label}",
                            callback_data=f"auq:{tid_short}:{i}",
                        )
                    )
                    if len(row) == 2:
                        buttons.append(row)
                        row = []
                if row:
                    buttons.append(row)
                buttons.append(
                    [
                        InlineKeyboardButton(
                            "✅ 确认选择",
                            callback_data=f"auq:{tid_short}:confirm",
                        )
                    ]
                )

                try:
                    await query.edit_message_reply_markup(InlineKeyboardMarkup(buttons))
                except Exception:
                    pass
                await query.answer()
        else:
            # Single select — immediate answer
            idx = int(action)
            if idx < 0 or idx >= min(len(options), 4):
                await query.answer("无效选项。", show_alert=True)
                return

            selected_label = options[idx].get("label", f"选项 {idx + 1}")
            logger.info(
                "AUQ single-select: resolving future",
                tool_use_id=tool_use_id,
                selected=selected_label,
                future_done=future.done(),
            )
            future.set_result({"selected": [selected_label]})
            logger.info(
                "AUQ single-select: future resolved",
                future_done=future.done(),
            )

            # Edit message: keep question + show all options with marks
            q_text = auq_meta["question_text"]
            hdr = auq_meta.get("header", "")
            hdr_line = f"<b>{escape_html(hdr)}</b>\n" if hdr else ""
            lines = [
                f"🤔 {hdr_line}<b>Claude 想问你：</b>\n" f"{escape_html(q_text)}\n"
            ]
            for i, opt in enumerate(options[:4]):
                lbl = opt.get("label", f"选项 {i + 1}")
                desc = opt.get("description", "")
                mark = "✅" if i == idx else "☐"
                entry = f"{mark} {escape_html(lbl)}"
                if desc:
                    entry += f"\n  {escape_html(desc)}"
                lines.append(entry)
            try:
                await query.edit_message_text(
                    "\n".join(lines),
                    parse_mode="HTML",
                    reply_markup=None,
                )
            except Exception:
                pass
            await query.answer()

    async def _agentic_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle cd: callbacks — switch directory and resume session if available."""
        query = update.callback_query
        await query.answer()

        data = query.data
        _, project_name = data.split(":", 1)

        base = self.settings.approved_directory
        new_path = base / project_name

        if not new_path.is_dir():
            await query.edit_message_text(
                f"目录不存在：<code>{escape_html(project_name)}</code>",
                parse_mode="HTML",
            )
            return

        context.user_data["current_directory"] = new_path

        # Look for a resumable session instead of always clearing
        claude_integration = context.bot_data.get("claude_integration")
        session_id = None
        existing_session = None
        if claude_integration:
            existing_session = await claude_integration._find_resumable_session(
                query.from_user.id, new_path
            )
            if existing_session:
                session_id = existing_session.session_id
        context.user_data["claude_session_id"] = session_id

        is_git = (new_path / ".git").is_dir()
        git_badge = " (git)" if is_git else ""

        switch_msg = (
            f"已切换到 <code>{escape_html(project_name)}/</code>" f"{git_badge}"
        )

        if session_id and claude_integration:
            resume_text = await self._build_session_resume_text(
                session_id,
                new_path,
                claude_integration,
                session_meta=existing_session,
            )
            await query.edit_message_text(
                f"{switch_msg}\n\n{resume_text}",
                parse_mode="HTML",
            )
        else:
            await query.edit_message_text(switch_msg, parse_mode="HTML")

        # Audit log
        audit_logger = context.bot_data.get("audit_logger")
        if audit_logger:
            await audit_logger.log_command(
                user_id=query.from_user.id,
                command="cd",
                args=[project_name],
                success=True,
            )
