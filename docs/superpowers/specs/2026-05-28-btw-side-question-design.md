# /btw Side Question Feature Design

**Date:** 2026-05-28
**Status:** Approved
**Scope:** Add `/btw` command to Telegram bridge for context-aware side questions without polluting conversation history

## Overview

Implement Claude Code CLI's `/btw` functionality in the Telegram bridge. Users send `/btw <question>` to ask a quick side question that:
- Has access to the full current session context
- Does NOT have tool access (pure text Q&A)
- Does NOT add to the conversation history
- Can be used while Claude is busy processing a main task
- Returns a single response, no follow-up turns

## Architecture

### Command Flow

```
Telegram: "/btw 这个项目的数据库用的什么？"
  → MessageOrchestrator.agentic_text()
  → 识别 /btw 前缀
  → 提取问题文本
  → 调用 _handle_btw()
  → ClaudeIntegration.run_btw()
  → ClaudeSDKManager.execute_btw()
  → 回复到 Telegram（引用用户消息 + 💡 btw: 前缀）
```

### Core Mechanism: Independent SDK Client with Session Resume

The key insight: create a **separate** `ClaudeSDKClient` that resumes the same session for context, but with all tools disabled.

```python
# In ClaudeSDKManager.execute_btw()
options = ClaudeAgentOptions(
    allowed_tools=[],           # No tools
    max_turns=1,                # Single response
    resume=session_id,          # Reuse session context
    cwd=str(working_directory),
    include_partial_messages=False,
    # ... same model, system prompt, etc.
)
client = ClaudeSDKClient(options)
await client.connect()
await client.query(question)
# Collect ResultMessage, extract text
```

**Why this is safe:** JSONL session files are append-only. Reading (resume) concurrent with writing (main task) is safe — the SDK reads existing content and the main task appends new content.

### Session ID Resolution

Priority order for finding the session to resume:

1. **Active main task** — if `_active_requests[user_id]` exists, use its `session_id`
2. **Session Manager** — look up user's most recent non-expired session via `SessionManager`
3. **No session** — reply with guidance to establish a session first

## Detailed Design

### 1. Command Registration

In `MessageOrchestrator._register_agentic_handlers()`:
- Register `/btw` command handler
- Add to `get_bot_commands()` for Telegram's command menu

### 2. SDK Layer: `execute_btw()`

New method in `ClaudeSDKManager`:

```python
async def execute_btw(
    self,
    question: str,
    working_directory: Path,
    session_id: str,
) -> str:
```

- Creates `ClaudeAgentOptions` with `allowed_tools=[]`, `max_turns=1`, `resume=session_id`
- Creates independent `ClaudeSDKClient`
- Returns extracted text content from `ResultMessage`
- 30-second timeout (shorter than main tasks)
- No stream callback needed

### 3. Facade Layer: `run_btw()`

New method in `ClaudeIntegration`:

```python
async def run_btw(
    self,
    question: str,
    working_directory: Path,
    user_id: int,
    session_id: str,
) -> str:
```

- Delegates to `sdk_manager.execute_btw()`
- Handles session lookup if needed

### 4. Orchestrator Layer: `_handle_btw()`

New method in `MessageOrchestrator`:

- Parses question from `/btw <question>` message
- Resolves session ID (active request > session manager > error)
- Calls `claude_integration.run_btw()`
- Sends response with `reply_to_message` + `💡 btw:` prefix
- Uses `ResponseFormatter` for long message splitting (auto-splits at 4096 chars)
- Logs audit event (user_id, question length, duration, success/failure)

### 5. Response Display

**Normal response:**
```
（引用用户的 /btw 消息）
💡 btw: 项目使用 PostgreSQL 15，配置在 docker-compose.yml 的 db 服务中。
```

**Long responses:** Auto-split by `ResponseFormatter` into multiple messages, each within Telegram's 4096 char limit.

**No session:**
```
💡 btw: 当前没有活跃会话，无法提供上下文相关的回答。
请先发送一条普通消息建立会话，然后再用 /btw 提问。
```

**No question text:**
```
用法: /btw <你的问题>
示例: /btw 刚才提到的那个配置文件叫什么？
```

## Error Handling

| Scenario | Response |
|----------|----------|
| Timeout (30s) | 💡 btw: 回答超时，请稍后重试。 |
| CLIConnectionError | 💡 btw: Claude 服务连接失败，请稍后重试。 |
| ProcessError | 💡 btw: 查询出错，请稍后重试。 |
| Unknown exception | Log + generic error message |

## Edge Cases

| Scenario | Behavior |
|----------|----------|
| No active session, no history | Reply with guidance to establish session first |
| /btw without question text | Reply with usage hint |
| Main task running | Execute independently, no interference |
| Main task completed (session ended) | Resume works normally |
| Multiple /btw in sequence | Execute concurrently, independent |

## File Changes

| File | Change |
|------|--------|
| `src/claude/sdk_integration.py` | Add `execute_btw()` method |
| `src/claude/facade.py` | Add `run_btw()` method |
| `src/bot/orchestrator.py` | Add `_handle_btw()` + register command |
| `src/config/settings.py` | Add `btw_timeout` config (optional) |

**Estimated:** ~150-200 lines new code, 0 lines deleted.

## Implementation Order

1. `execute_btw()` — SDK layer core method
2. `run_btw()` — Facade layer wrapper
3. `_handle_btw()` — Command handler + response formatting
4. Register command + update command menu

## Non-Goals

- No tool access for /btw (matches CLI behavior)
- No follow-up turns in /btw overlay (single response only)
- No "fork to session" feature (could be added later)
- No /btw history persistence (ephemeral by design)
