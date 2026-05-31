# AGENTS.md

This file gives Codex repository-specific rules for working on this project.
Follow it in addition to higher-priority system/developer instructions.

## Project Shape

- This is a Python Telegram bot that exposes Claude Code through Telegram.
- Runtime entrypoint: `src.main:run`, installed as `claude-telegram-bot`.
- Main packages:
  - `src/bot/`: Telegram application, handlers, middleware, orchestration.
  - `src/claude/`: Claude SDK facade, SDK client execution, sessions, BTW.
  - `src/storage/`: SQLite storage and repositories.
  - `src/security/`: auth, validation, audit, rate limiting.
  - `src/config/`: Pydantic settings and feature flags.
  - `src/api/`, `src/events/`, `src/scheduler/`, `src/notifications/`: automation platform.
- Tests live under `tests/unit/` and generally mirror `src/`.

## Local Runtime Facts

- Development workspace: `D:\claudebot\claude-code-telegram`.
- Bot state/log directory on this machine: `C:\Users\WHY\.claude-tg-bot`.
- Active local env file is normally `C:\Users\WHY\.claude-tg-bot\.env`, not a repo-root `.env`.
- Current Windows launcher: `C:\Users\WHY\.claude-tg-bot\start-bot.vbs`.
- Installed command path: `C:\Users\WHY\.local\bin\claude-telegram-bot.exe`.
- The bot is installed editable with `uv tool install --force --editable .`; source changes still require bot restart.
- Health endpoint, when enabled: `http://127.0.0.1:8080/health`.

## Engineering Rules

- Prefer existing patterns over new abstractions. This codebase uses async Python, `python-telegram-bot`, Pydantic Settings, `structlog`, and repository-style storage.
- Use `rg`/`rg --files` for search.
- Use `apply_patch` for manual edits.
- Do not rewrite large files with formatters unless the task explicitly calls for it. Some files have broad existing diffs.
- Avoid touching unrelated dirty files. Assume uncommitted changes may belong to the user.
- Keep comments sparse and useful. Do not add narration comments for obvious code.
- Use timezone-aware UTC: `datetime.now(UTC)`, not `datetime.utcnow()`.
- Keep persistent data out of source files. Runtime snapshots such as active BTW state must stay in memory unless a feature explicitly requires metadata persistence.

## Claude SDK Rules

- Main Claude runs should load user and project Claude settings:
  - `setting_sources=["user", "project"]`
- `/btw` side-question runs should remain isolated:
  - `setting_sources=["project"]`
  - `max_turns=1`
  - `tools=[]`
  - `allowed_tools=[]`
  - no MCP servers
  - no `can_use_tool`
  - use `resume=<parent_session_id>` with `fork_session=True` only when a parent session id exists.
- Never let a `/btw` result replace `context.user_data["claude_session_id"]`.
- Never save `/btw` question text or runtime snapshot into the main interaction history.
- BTW runs must use an ephemeral Claude config directory and clean it up after completion; new BTW sessions should not persist in Claude local JSONL or Telegram storage. Keep legacy BTW fork filtering for older recorded fork sessions.

## Telegram Bot Rules

- `/btw`, stop actions, and AUQ callbacks must bypass normal sequential processing so they work while a main task is running.
- Normal text and ordinary commands should remain serialized per user/chat.
- Telegram command menus must be written to both default scope and `BotCommandScopeAllPrivateChats`.
  - A previous official Telegram Claude plugin can leave `all_private_chats` commands behind; default scope alone will not override the menu seen in private chats.
- After changing command registration, restart the bot and verify command scopes with `get_my_commands()` for both default and all-private scope.
- Do not re-enable `telegram@claude-plugins-official` in `C:\Users\WHY\.claude\settings.json` for this project. It can start its own Telegram `getUpdates` poller and conflict with this bot.

## Security And Environment

- `TELEGRAM_BOT_TOKEN` belongs to this bot. Be careful not to leak it into logs, docs, or command output.
- Claude subprocesses inherit environment by default. Before adding user-level plugins/MCP behavior, consider whether sensitive bot env vars could be exposed to child processes.
- Do not print full Telegram tokens. If a command output includes one, summarize without reproducing it.
- Approved directory boundaries matter. File and shell tools must stay within `APPROVED_DIRECTORY` unless the user explicitly changes project configuration.

## Logging And Diagnostics

- Main log: `C:\Users\WHY\.claude-tg-bot\bot.log`.
- Launcher log: `C:\Users\WHY\.claude-tg-bot\launcher.log`.
- Restart helper log may exist at `C:\Users\WHY\.claude-tg-bot\restart-helper.log`.
- Important log events:
  - `/btw received`
  - `/btw progress message sent`
  - `Starting Claude SDK command`
  - `Starting /btw side question`
  - `Polling transport error`
  - `Conflict: terminated by other getUpdates request`
  - `Bot commands set`
- Log timestamps are UTC ISO strings. Convert to local time when comparing with user reports.

## Testing

Use focused tests for the touched area first. Common commands:

```powershell
.\.venv\Scripts\python.exe -m py_compile src\bot\core.py src\claude\sdk_integration.py
.\.venv\Scripts\python.exe -m pytest tests\unit\test_claude\test_sdk_integration.py -q
.\.venv\Scripts\python.exe -m pytest tests\unit\test_orchestrator.py::test_handle_btw_sends_reply tests\unit\test_bot\test_update_processor.py -q
.\.venv\Scripts\python.exe -m pytest tests\unit\test_bot\test_core_rate_limiter.py -q
```

For broader validation:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit -q
```

## Restarting The Local Bot

- Source edits do not affect the running process until restart.
- Prefer the existing launcher after stopping the current main PID from `C:\Users\WHY\.claude-tg-bot\bot.pid`.
- After restart, verify:
  - only one `claude-telegram-bot.exe` process chain exists,
  - `http://127.0.0.1:8080/health` returns `{"status":"ok"}`,
  - recent log has a fresh `Single-instance lock acquired` and `Bot commands set`.

## Recent Project-Specific Pitfalls

- Official Claude Code Telegram plugin conflict:
  - It can be loaded from user Claude settings and start a second Telegram poller.
  - It previously caused `/btw`, stop buttons, AUQ buttons, and `/restart` to appear unresponsive during main Claude runs.
  - The plugin was removed from user Claude config and local plugin cache; do not add it back unless using a separate bot token and state directory.
- Telegram command menu cache/scope:
  - If manual commands work but the menu is wrong, check `all_private_chats` scope before debugging handlers.
- BTW is immediate only if Telegram polling is healthy:
  - If `/btw` does not even send the "answering" message, inspect polling/update ingestion first, not the BTW handler.
