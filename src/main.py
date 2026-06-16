"""Main entry point for Claude Code Telegram Bot."""

import argparse
import asyncio
import json
import logging
import os
import re
import signal
import sys
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, Optional

import structlog

from src import __version__
from src.bot.core import ClaudeCodeBot
from src.claude import (
    ClaudeIntegration,
    SessionManager,
)
from src.claude.sdk_integration import ClaudeSDKManager
from src.config.features import FeatureFlags
from src.config.providers import ProviderManager
from src.config.settings import Settings
from src.events.bus import EventBus
from src.events.handlers import AgentHandler
from src.events.middleware import EventSecurityMiddleware
from src.exceptions import ConfigurationError
from src.notifications.service import NotificationService
from src.projects import ProjectThreadManager, load_project_registry
from src.scheduler.scheduler import JobScheduler
from src.security.audit import AuditLogger, InMemoryAuditStorage
from src.security.auth import (
    AuthenticationManager,
    InMemoryTokenStorage,
    TokenAuthProvider,
    WhitelistAuthProvider,
)
from src.security.rate_limiter import RateLimiter
from src.security.validators import SecurityValidator
from src.storage.facade import Storage
from src.storage.session_storage import SQLiteSessionStorage
from src.utils.single_instance import SingleInstanceGuard


_TELEGRAM_BOT_URL_RE = re.compile(
    r"(https://api\.telegram\.org/bot)\d{8,10}:[A-Za-z0-9_-]+"
)
_TELEGRAM_TOKEN_RE = re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{20,}\b")


def _sanitize_log_text(value: str) -> str:
    value = _TELEGRAM_BOT_URL_RE.sub(r"\1<telegram-token>", value)
    return _TELEGRAM_TOKEN_RE.sub("<telegram-token>", value)


class SanitizingFormatter(logging.Formatter):
    """Formatter that removes secrets from final log lines."""

    def format(self, record: logging.LogRecord) -> str:
        return _sanitize_log_text(super().format(record))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def setup_logging(debug: bool = False) -> None:
    """Configure structured logging."""
    raw_level = os.getenv("LOG_LEVEL", "DEBUG" if debug else "INFO").upper()
    level = getattr(logging, raw_level, logging.INFO)

    formatter = SanitizingFormatter("%(message)s")
    handlers: list[logging.Handler] = []

    log_to_file = _env_bool("LOG_TO_FILE", sys.platform == "win32")
    if log_to_file:
        default_log_file = Path.home() / ".claude-tg-bot" / "bot.log"
        log_file = Path(os.getenv("LOG_FILE", str(default_log_file))).expanduser()
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=_env_int("LOG_MAX_BYTES", 10 * 1024 * 1024),
            backupCount=_env_int("LOG_BACKUP_COUNT", 5),
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)

    log_to_console = _env_bool("LOG_TO_CONSOLE", debug or not handlers)
    if log_to_console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        handlers.append(console_handler)

    # Configure standard logging
    logging.basicConfig(
        level=level,
        handlers=handlers,
        force=True,
    )

    # Keep third-party HTTP noise out of the main bot log. It is especially
    # noisy during long polling and may include sensitive request URLs.
    for logger_name in ("httpx", "httpcore"):
        logging.getLogger(logger_name).setLevel(logging.WARNING)

    # Configure structlog
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            (
                structlog.processors.JSONRenderer()
                if not debug
                else structlog.dev.ConsoleRenderer()
            ),
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Claude Code Telegram Bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--version", action="version", version=f"Claude Code Telegram Bot {__version__}"
    )

    parser.add_argument("--debug", action="store_true", help="Enable debug logging")

    parser.add_argument("--config-file", type=Path, help="Path to configuration file")

    return parser.parse_args()


async def create_application(config: Settings) -> Dict[str, Any]:
    """Create and configure the application components."""
    logger = structlog.get_logger()
    logger.info("Creating application components")

    features = FeatureFlags(config)

    # Initialize storage system
    storage = Storage(config.database_url)
    await storage.initialize()

    # Create security components
    providers = []

    # Add whitelist provider if users are configured
    if config.allowed_users:
        providers.append(WhitelistAuthProvider(config.allowed_users))

    # Add token provider if enabled
    if config.enable_token_auth:
        token_storage = InMemoryTokenStorage()  # TODO: Use database storage
        providers.append(TokenAuthProvider(config.auth_token_secret, token_storage))

    # Fall back to allowing all users in development mode
    if not providers and config.development_mode:
        logger.warning(
            "No auth providers configured"
            " - creating development-only allow-all provider"
        )
        providers.append(WhitelistAuthProvider([], allow_all_dev=True))
    elif not providers:
        raise ConfigurationError("No authentication providers configured")

    auth_manager = AuthenticationManager(providers)
    security_validator = SecurityValidator(
        config.approved_directory,
        disable_security_patterns=config.disable_security_patterns,
        extra_forbidden_filenames=config.forbidden_filenames,
        extra_dangerous_file_patterns=config.extra_dangerous_file_patterns,
    )
    rate_limiter = RateLimiter(config)

    # Create audit storage and logger
    audit_storage = InMemoryAuditStorage()  # TODO: Use database storage in production
    audit_logger = AuditLogger(audit_storage)

    # Create Claude integration components with persistent storage
    session_storage = SQLiteSessionStorage(storage.db_manager)
    session_manager = SessionManager(config, session_storage)

    # Create provider manager for runtime provider/model switching
    provider_manager = ProviderManager(config)

    # Create Claude SDK manager and integration facade
    logger.info("Using Claude Python SDK integration")
    sdk_manager = ClaudeSDKManager(
        config, security_validator=security_validator, provider_manager=provider_manager
    )

    claude_integration = ClaudeIntegration(
        config=config,
        sdk_manager=sdk_manager,
        session_manager=session_manager,
        provider_manager=provider_manager,
    )

    # --- Event bus and agentic platform components ---
    event_bus = EventBus()

    # Event security middleware
    event_security = EventSecurityMiddleware(
        event_bus=event_bus,
        security_validator=security_validator,
        auth_manager=auth_manager,
    )
    event_security.register()

    # Agent handler — translates events into Claude executions
    agent_handler = AgentHandler(
        event_bus=event_bus,
        claude_integration=claude_integration,
        default_working_directory=config.approved_directory,
        default_user_id=config.allowed_users[0] if config.allowed_users else 0,
    )
    agent_handler.register()

    # Create bot with all dependencies
    dependencies = {
        "auth_manager": auth_manager,
        "security_validator": security_validator,
        "rate_limiter": rate_limiter,
        "audit_logger": audit_logger,
        "claude_integration": claude_integration,
        "storage": storage,
        "event_bus": event_bus,
        "provider_manager": provider_manager,
        "project_registry": None,
        "project_threads_manager": None,
    }

    bot = ClaudeCodeBot(config, dependencies)

    # Notification service and scheduler need the bot's Telegram Bot instance,
    # which is only available after bot.initialize(). We store placeholders
    # and wire them up in run_application() after initialization.

    logger.info("Application components created successfully")

    return {
        "bot": bot,
        "claude_integration": claude_integration,
        "storage": storage,
        "config": config,
        "features": features,
        "event_bus": event_bus,
        "agent_handler": agent_handler,
        "auth_manager": auth_manager,
        "security_validator": security_validator,
    }


async def run_application(app: Dict[str, Any]) -> None:
    """Run the application with graceful shutdown handling."""
    logger = structlog.get_logger()
    bot: ClaudeCodeBot = app["bot"]
    claude_integration: ClaudeIntegration = app["claude_integration"]
    storage: Storage = app["storage"]
    config: Settings = app["config"]
    features: FeatureFlags = app["features"]
    event_bus: EventBus = app["event_bus"]

    # ── Single-instance guard via PID file ───────────────────────
    pid_path = Path.home() / ".claude-tg-bot" / "bot.pid"
    try:
        if pid_path.exists():
            old_pid = int(pid_path.read_text(encoding="utf-8").strip())
            if old_pid != os.getpid():
                # Check if old process is still alive
                import subprocess as _sp

                check = _sp.run(
                    ["tasklist", "/FI", f"PID eq {old_pid}"],
                    capture_output=True,
                    text=True,
                )
                if str(old_pid) in check.stdout:
                    logger.warning(
                        "PID file belongs to another running bot process",
                        old_pid=old_pid,
                    )
    except Exception as exc:
        logger.debug("PID file check failed", error=str(exc))

    # Write current PID
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(os.getpid()), encoding="utf-8")
    # ── End single-instance guard ────────────────────────────────

    notification_service: Optional[NotificationService] = None
    scheduler: Optional[JobScheduler] = None
    project_threads_manager: Optional[ProjectThreadManager] = None

    # Set up signal handlers for graceful shutdown
    shutdown_event = asyncio.Event()

    def signal_handler(signum: int, frame: Any) -> None:
        logger.info("Shutdown signal received", signal=signum)
        shutdown_event.set()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        logger.info("Starting Claude Code Telegram Bot")

        # Initialize the bot first (creates the Telegram Application)
        await bot.initialize()

        if config.enable_project_threads:
            if not config.projects_config_path:
                raise ConfigurationError(
                    "Project thread mode enabled but required settings are missing"
                )
            registry = load_project_registry(
                config_path=config.projects_config_path,
                approved_directory=config.approved_directory,
            )
            project_threads_manager = ProjectThreadManager(
                registry=registry,
                repository=storage.project_threads,
                sync_action_interval_seconds=(
                    config.project_threads_sync_action_interval_seconds
                ),
            )

            bot.deps["project_registry"] = registry
            bot.deps["project_threads_manager"] = project_threads_manager

            if config.project_threads_mode == "group":
                if config.project_threads_chat_id is None:
                    raise ConfigurationError(
                        "Group thread mode requires PROJECT_THREADS_CHAT_ID"
                    )
                sync_result = await project_threads_manager.sync_topics(
                    bot.app.bot,
                    chat_id=config.project_threads_chat_id,
                )
                logger.info(
                    "Project thread startup sync complete",
                    mode=config.project_threads_mode,
                    chat_id=config.project_threads_chat_id,
                    created=sync_result.created,
                    reused=sync_result.reused,
                    renamed=sync_result.renamed,
                    failed=sync_result.failed,
                    deactivated=sync_result.deactivated,
                )

        # Now wire up components that need the Telegram Bot instance
        telegram_bot = bot.app.bot

        # Start event bus
        await event_bus.start()

        # Notification service
        notification_service = NotificationService(
            event_bus=event_bus,
            bot=telegram_bot,
            default_chat_ids=config.notification_chat_ids or [],
        )
        notification_service.register()
        await notification_service.start()

        # Check for restart marker and send "restart complete" message
        marker_path = Path.home() / ".claude-tg-bot" / "restart_marker.json"
        if marker_path.exists():
            try:
                marker = json.loads(marker_path.read_text(encoding="utf-8"))
                age = (
                    datetime.now(UTC)
                    - datetime.fromisoformat(marker["timestamp"])
                ).total_seconds()
                if age < 300:  # only if marker is < 5 minutes old
                    text = "✅ <b>重启完成。</b>\n\n机器人已上线。"

                    # Append session resume info for the root directory
                    try:
                        user_id = marker["user_id"]
                        root_dir = Path(config.approved_directory)
                        existing = (
                            await claude_integration
                            ._find_resumable_session(user_id, root_dir)
                        )
                        if existing:
                            title = (
                                await claude_integration.read_session_title(
                                    existing.session_id, root_dir
                                )
                            )
                            headline = title if title else existing.session_id
                            text += (
                                f"\n\n📎 {headline}"
                                f"\n   {root_dir.name}"
                            )
                    except Exception:
                        pass

                    await telegram_bot.send_message(
                        chat_id=marker["chat_id"],
                        text=text,
                        parse_mode="HTML",
                    )
                    logger.info(
                        "Sent restart-complete notification",
                        chat_id=marker["chat_id"],
                    )
            except Exception as exc:
                logger.warning(
                    "Failed to send restart-complete notification",
                    error=str(exc),
                )
            finally:
                try:
                    marker_path.unlink(missing_ok=True)
                except Exception:
                    pass

        # Collect concurrent tasks
        tasks = []

        # Bot task — use start() which handles its own initialization check
        bot_task = asyncio.create_task(bot.start())
        tasks.append(bot_task)

        # API server (if enabled)
        if features.api_server_enabled:
            from src.api.server import run_api_server

            api_task = asyncio.create_task(
                run_api_server(
                    event_bus,
                    config,
                    storage.db_manager,
                    health_provider=bot.get_health_status,
                )
            )
            tasks.append(api_task)
            logger.info("API server enabled", port=config.api_server_port)

        # Scheduler (if enabled)
        if features.scheduler_enabled:
            scheduler = JobScheduler(
                event_bus=event_bus,
                db_manager=storage.db_manager,
                default_working_directory=config.approved_directory,
            )
            await scheduler.start()
            logger.info("Job scheduler enabled")

        # Shutdown task
        shutdown_task = asyncio.create_task(shutdown_event.wait())
        tasks.append(shutdown_task)

        # Wait for any task to complete or shutdown signal
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

        # Check completed tasks for exceptions
        for task in done:
            if task.cancelled():
                continue
            exc = task.exception()
            if exc is not None:
                logger.error(
                    "Task failed",
                    task=task.get_name(),
                    error=str(exc),
                    error_type=type(exc).__name__,
                )

        # Cancel remaining tasks
        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    except Exception as e:
        logger.error("Application error", error=str(e))
        raise
    finally:
        # Ordered shutdown: scheduler -> API -> notification -> bot -> claude -> storage
        logger.info("Shutting down application")

        try:
            if scheduler:
                await scheduler.stop()
            if notification_service:
                await notification_service.stop()
            await event_bus.stop()
            await bot.stop()
            await claude_integration.shutdown()
            await storage.close()
        except Exception as e:
            logger.error("Error during shutdown", error=str(e))

        # Clean up PID file
        try:
            pid_path = Path.home() / ".claude-tg-bot" / "bot.pid"
            pid_path.unlink(missing_ok=True)
        except Exception:
            pass

        logger.info("Application shutdown complete")


async def main() -> None:
    """Main application entry point."""
    args = parse_args()
    setup_logging(debug=args.debug)

    logger = structlog.get_logger()
    logger.info("Starting Claude Code Telegram Bot", version=__version__)

    try:
        # Load configuration
        from src.config import FeatureFlags, load_config

        config = load_config(config_file=args.config_file)
        features = FeatureFlags(config)

        logger.info(
            "Configuration loaded",
            environment="production" if config.is_production else "development",
            enabled_features=features.get_enabled_features(),
            debug=config.debug,
        )

        state_dir = Path.home() / ".claude-tg-bot"
        instance_guard = SingleInstanceGuard(
            lock_path=state_dir / "bot.lock",
            pid_path=state_dir / "bot.pid",
        )
        if not instance_guard.acquire(wait_seconds=30.0):
            logger.warning(
                "Another Claude Telegram bot instance is already running",
                existing_pid=instance_guard.read_existing_pid(),
            )
            return

        try:
            logger.info("Single-instance lock acquired", pid=os.getpid())
            app = await create_application(config)
            await run_application(app)
        finally:
            instance_guard.release()

    except ConfigurationError as e:
        logger.error("Configuration error", error=str(e))
        sys.exit(1)
    except Exception as e:
        logger.exception("Unexpected error", error=str(e))
        sys.exit(1)


def run() -> None:
    """Synchronous entry point for setuptools."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutdown requested by user")
        sys.exit(0)


if __name__ == "__main__":
    run()
