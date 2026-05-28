# /btw Side Question Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `/btw` command for context-aware side questions that don't pollute conversation history, using an independent SDK client with session resume.

**Architecture:** Create a new `execute_btw()` method in `ClaudeSDKManager` that spawns an independent `ClaudeSDKClient` with `resume=<session_id>` and `allowed_tools=[]`. Wire it through the facade and orchestrator layers following existing patterns. The /btw command runs concurrently with main tasks without interference.

**Tech Stack:** Python 3.10+, claude-agent-sdk, python-telegram-bot, pytest-asyncio

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `src/claude/sdk_integration.py` | Modify | Add `execute_btw()` — independent SDK client with no tools |
| `src/claude/facade.py` | Modify | Add `run_btw()` — session lookup + delegate to SDK |
| `src/bot/orchestrator.py` | Modify | Add `_handle_btw()`, register command, update menus |
| `tests/unit/test_claude/test_sdk_integration.py` | Modify | Add tests for `execute_btw()` |
| `tests/unit/test_orchestrator.py` | Modify | Add tests for `_handle_btw()` |

---

### Task 1: Add `execute_btw()` to ClaudeSDKManager

**Files:**
- Modify: `src/claude/sdk_integration.py:275-730`
- Test: `tests/unit/test_claude/test_sdk_integration.py`

- [ ] **Step 1: Write the failing test for `execute_btw()`**

```python
# Add to tests/unit/test_claude/test_sdk_integration.py

@pytest.mark.asyncio
async def test_execute_btw_returns_content(sdk_manager, mock_sdk_client):
    """execute_btw should return text from ResultMessage with no tools."""
    mock_result = MagicMock(spec=ResultMessage)
    mock_result.result = "PostgreSQL 15"
    mock_result.total_cost_usd = 0.001
    mock_result.session_id = "test-session-id"
    mock_result.usage = None
    mock_result.model_usage = None

    mock_sdk_client._query.receive_messages = AsyncMock(
        return_value=iter([mock_result])
    )

    response = await sdk_manager.execute_btw(
        question="What database?",
        working_directory=Path("/tmp"),
        session_id="existing-session-id",
    )

    assert response == "PostgreSQL 15"
    # Verify the client was created with no tools
    call_args = mock_sdk_client.call_args
    options = call_args[0][0]
    assert options.allowed_tools == []
    assert options.max_turns == 1
    assert options.resume == "existing-session-id"


@pytest.mark.asyncio
async def test_execute_btw_handles_timeout(sdk_manager, mock_sdk_client):
    """execute_btw should raise ClaudeTimeoutError on timeout."""
    mock_sdk_client._query.receive_messages = AsyncMock(
        side_effect=asyncio.TimeoutError
    )

    with pytest.raises(ClaudeTimeoutError):
        await sdk_manager.execute_btw(
            question="test",
            working_directory=Path("/tmp"),
            session_id="test-session",
        )


@pytest.mark.asyncio
async def test_execute_btw_handles_connection_error(sdk_manager, mock_sdk_client):
    """execute_btw should raise ClaudeProcessError on CLIConnectionError."""
    mock_sdk_client.connect = AsyncMock(side_effect=CLIConnectionError("fail"))

    with pytest.raises(ClaudeProcessError):
        await sdk_manager.execute_btw(
            question="test",
            working_directory=Path("/tmp"),
            session_id="test-session",
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd D:\claudebot\claude-code-telegram && poetry run pytest tests/unit/test_claude/test_sdk_integration.py -k "btw" -v`
Expected: FAIL with `AttributeError: 'ClaudeSDKManager' object has no attribute 'execute_btw'`

- [ ] **Step 3: Implement `execute_btw()` in ClaudeSDKManager**

Add this method to `ClaudeSDKManager` class in `src/claude/sdk_integration.py`, after `execute_command()` (around line 730):

```python
async def execute_btw(
    self,
    question: str,
    working_directory: Path,
    session_id: str,
) -> str:
    """Execute a /btw side question — no tools, single turn, resume context."""
    start_time = asyncio.get_event_loop().time()
    btw_timeout = 30  # seconds

    logger.info(
        "Starting /btw side question",
        working_directory=str(working_directory),
        session_id=session_id,
        question_length=len(question),
    )

    try:
        # Apply provider profile environment overrides
        saved_env: Dict[str, Optional[str]] = {}
        if self.provider_manager:
            saved_env = self.provider_manager.apply_to_environ()

        stderr_lines: List[str] = []

        def _stderr_callback(line: str) -> None:
            stderr_lines.append(line)

        # Build system prompt (same as execute_command)
        base_prompt = (
            f"All file operations must stay within {working_directory}. "
            "Use relative paths."
        )
        claude_md_path = Path(working_directory) / "CLAUDE.md"
        if claude_md_path.exists():
            base_prompt += "\n\n" + claude_md_path.read_text(encoding="utf-8")

        # Resolve effective model
        if self.provider_manager:
            effective_model = self.provider_manager.get_effective_model() or None
        else:
            effective_model = self.config.claude_model or None

        # Build options: no tools, single turn, resume session
        options = ClaudeAgentOptions(
            max_turns=1,
            model=effective_model,
            max_budget_usd=self.config.claude_max_cost_per_request,
            cwd=str(working_directory),
            allowed_tools=[],  # No tools for /btw
            disallowed_tools=[],
            cli_path=self.config.claude_cli_path or None,
            include_partial_messages=False,
            sandbox={
                "enabled": self.config.sandbox_enabled,
                "autoAllowBashIfSandboxed": True,
                "excludedCommands": self.config.sandbox_excluded_commands or [],
            },
            system_prompt=base_prompt,
            setting_sources=["project"],
            stderr=_stderr_callback,
        )
        options.resume = session_id

        messages: List[Message] = []

        async def _run_client() -> None:
            client = ClaudeSDKClient(options)
            try:
                await client.connect()
                await client.query(question)

                async for raw_data in client._query.receive_messages():
                    try:
                        message = parse_message(raw_data)
                    except MessageParseError as e:
                        logger.debug(
                            "Skipping unparseable message in /btw",
                            error=str(e),
                        )
                        continue

                    messages.append(message)

                    if isinstance(message, ResultMessage):
                        break
            finally:
                await client.disconnect()

        # Run with timeout, no retry for /btw
        try:
            await asyncio.wait_for(_run_client(), timeout=btw_timeout)
        except asyncio.TimeoutError:
            raise ClaudeTimeoutError(
                f"/btw timed out after {btw_timeout}s",
                timeout_seconds=btw_timeout,
            )
        except asyncio.CancelledError:
            raise

        duration_ms = int(
            (asyncio.get_event_loop().time() - start_time) * 1000
        )

        # Extract content from ResultMessage
        content = ""
        for message in messages:
            if isinstance(message, ResultMessage):
                result_content = getattr(message, "result", None)
                if result_content is not None:
                    content = str(result_content).strip()
                break

        # Fallback: collect assistant text blocks
        if not content:
            text_parts: List[str] = []
            for msg in messages:
                if isinstance(msg, AssistantMessage):
                    msg_content = getattr(msg, "content", []) or []
                    for block in msg_content:
                        if isinstance(block, TextBlock):
                            text_parts.append(block.text)
            content = "\n".join(text_parts).strip()

        logger.info(
            "/btw completed",
            duration_ms=duration_ms,
            content_length=len(content),
        )

        return content

    except (ClaudeTimeoutError, ClaudeProcessError, ClaudeMCPError):
        raise
    except CLINotFoundError as exc:
        raise ClaudeProcessError(
            f"Claude CLI not found: {exc}",
            details={"stderr": str(exc)},
        ) from exc
    except ProcessError as exc:
        raise ClaudeProcessError(
            f"Claude CLI process error: {exc}",
            details={"stderr": "\n".join(stderr_lines)},
        ) from exc
    except CLIConnectionError as exc:
        raise ClaudeProcessError(
            f"Claude CLI connection error: {exc}",
            details={"stderr": "\n".join(stderr_lines)},
        ) from exc
    except Exception as exc:
        logger.error("/btw unexpected error", error=str(exc))
        raise ClaudeProcessError(
            f"/btw unexpected error: {exc}",
        ) from exc
    finally:
        if self.provider_manager and saved_env:
            self.provider_manager.restore_environ(saved_env)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd D:\claudebot\claude-code-telegram && poetry run pytest tests/unit/test_claude/test_sdk_integration.py -k "btw" -v`
Expected: PASS

- [ ] **Step 5: Run full SDK test suite to check for regressions**

Run: `cd D:\claudebot\claude-code-telegram && poetry run pytest tests/unit/test_claude/test_sdk_integration.py -v`
Expected: All existing tests still pass

- [ ] **Step 6: Commit**

```bash
cd D:\claudebot\claude-code-telegram
git add src/claude/sdk_integration.py tests/unit/test_claude/test_sdk_integration.py
git commit -m "feat(btw): add execute_btw() to ClaudeSDKManager

Independent SDK client with session resume, no tools, single turn.
30s timeout, no retry. Returns text content from ResultMessage."
```

---

### Task 2: Add `run_btw()` to ClaudeIntegration facade

**Files:**
- Modify: `src/claude/facade.py:38-215`
- Test: `tests/unit/test_claude/test_facade.py` (if exists, otherwise create)

- [ ] **Step 1: Write the failing test for `run_btw()`**

```python
# Add to tests/unit/test_claude/test_facade.py (or create if needed)

@pytest.mark.asyncio
async def test_run_btw_delegates_to_sdk(facade, mock_sdk_manager):
    """run_btw should delegate to sdk_manager.execute_btw()"""
    mock_sdk_manager.execute_btw = AsyncMock(return_value="PostgreSQL 15")

    result = await facade.run_btw(
        question="What database?",
        working_directory=Path("/tmp"),
        user_id=12345,
        session_id="test-session-id",
    )

    assert result == "PostgreSQL 15"
    mock_sdk_manager.execute_btw.assert_called_once_with(
        question="What database?",
        working_directory=Path("/tmp"),
        session_id="test-session-id",
    )


@pytest.mark.asyncio
async def test_run_btw_looks_up_session_when_none(facade, mock_sdk_manager, mock_session_manager):
    """run_btw should look up recent session when session_id is empty."""
    mock_session = MagicMock()
    mock_session.session_id = "found-session-id"
    mock_session_manager._get_user_sessions = AsyncMock(return_value=[mock_session])
    facade.session_manager = mock_session_manager
    mock_sdk_manager.execute_btw = AsyncMock(return_value="answer")

    result = await facade.run_btw(
        question="test",
        working_directory=Path("/tmp"),
        user_id=12345,
        session_id="",
    )

    mock_sdk_manager.execute_btw.assert_called_once()
    call_kwargs = mock_sdk_manager.execute_btw.call_args[1]
    assert call_kwargs["session_id"] == "found-session-id"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd D:\claudebot\claude-code-telegram && poetry run pytest tests/unit/test_claude/ -k "btw" -v`
Expected: FAIL with `AttributeError: 'ClaudeIntegration' object has no attribute 'run_btw'`

- [ ] **Step 3: Implement `run_btw()` in ClaudeIntegration**

Add this method to `ClaudeIntegration` class in `src/claude/facade.py`, after `run_command()`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd D:\claudebot\claude-code-telegram && poetry run pytest tests/unit/test_claude/ -k "btw" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd D:\claudebot\claude-code-telegram
git add src/claude/facade.py
git commit -m "feat(btw): add run_btw() to ClaudeIntegration facade

Session lookup fallback + delegate to SDK manager."
```

---

### Task 3: Add `_handle_btw()` and register `/btw` command in orchestrator

**Files:**
- Modify: `src/bot/orchestrator.py:358-420` (handler registration)
- Modify: `src/bot/orchestrator.py:543-602` (bot commands menus)
- Add: `_handle_btw()` method (new method in MessageOrchestrator)

- [ ] **Step 1: Write the failing test for `_handle_btw()`**

```python
# Add to tests/unit/test_orchestrator.py

@pytest.mark.asyncio
async def test_handle_btw_sends_reply(agentic_settings, deps, tmp_dir):
    """_handle_btw should reply with 💡 btw: prefix."""
    orchestrator = MessageOrchestrator(agentic_settings, deps)

    mock_update = MagicMock()
    mock_update.message.text = "/btw What database does this use?"
    mock_update.message.message_id = 100
    mock_update.message.reply_text = AsyncMock()
    mock_update.effective_user.id = 12345

    mock_context = MagicMock()
    mock_context.bot_data = {
        "claude_integration": deps["claude_integration"],
        "storage": deps["storage"],
        "audit_logger": MagicMock(),
    }
    mock_context.user_data = {
        "claude_session_id": "test-session-id",
        "current_directory": str(tmp_dir),
    }

    deps["claude_integration"].run_btw = AsyncMock(return_value="PostgreSQL 15")

    await orchestrator._handle_btw(mock_update, mock_context)

    mock_update.message.reply_text.assert_called_once()
    call_args = mock_update.message.reply_text.call_args
    assert "💡 btw:" in call_args[0][0] or "💡 btw:" in call_args[1].get("text", "")
    assert "PostgreSQL 15" in call_args[0][0] or "PostgreSQL 15" in call_args[1].get("text", "")


@pytest.mark.asyncio
async def test_handle_btw_no_question_shows_usage(agentic_settings, deps):
    """_handle_btw without question text should show usage hint."""
    orchestrator = MessageOrchestrator(agentic_settings, deps)

    mock_update = MagicMock()
    mock_update.message.text = "/btw"
    mock_update.message.reply_text = AsyncMock()

    mock_context = MagicMock()
    mock_context.user_data = {}

    await orchestrator._handle_btw(mock_update, mock_context)

    mock_update.message.reply_text.assert_called_once()
    call_args = mock_update.message.reply_text.call_args
    assert "用法" in call_args[0][0] or "用法" in call_args[1].get("text", "")


@pytest.mark.asyncio
async def test_handle_btw_no_session(agentic_settings, deps):
    """_handle_btw with no session should return guidance message."""
    orchestrator = MessageOrchestrator(agentic_settings, deps)

    mock_update = MagicMock()
    mock_update.message.text = "/btw What is this?"
    mock_update.message.reply_text = AsyncMock()

    mock_context = MagicMock()
    mock_context.bot_data = {
        "claude_integration": deps["claude_integration"],
        "storage": deps["storage"],
    }
    mock_context.user_data = {}  # No session

    deps["claude_integration"].run_btw = AsyncMock(return_value="")

    await orchestrator._handle_btw(mock_update, mock_context)

    mock_update.message.reply_text.assert_called_once()
    call_args = mock_update.message.reply_text.call_args
    assert "会话" in call_args[0][0] or "会话" in call_args[1].get("text", "")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd D:\claudebot\claude-code-telegram && poetry run pytest tests/unit/test_orchestrator.py -k "btw" -v`
Expected: FAIL with `AttributeError: 'MessageOrchestrator' object has no attribute '_handle_btw'`

- [ ] **Step 3: Implement `_handle_btw()` in MessageOrchestrator**

Add this method to `MessageOrchestrator` class in `src/bot/orchestrator.py`. Place it after the existing agentic command handlers (e.g., after `agentic_skill`):

```python
async def _handle_btw(
    self, update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /btw <question> — side question without polluting history."""
    msg = update.effective_message
    if not msg or not msg.text:
        return

    user_id = update.effective_user.id

    # Parse question from command text
    parts = msg.text.split(maxsplit=1)
    question = parts[1].strip() if len(parts) > 1 else ""

    if not question:
        await msg.reply_text(
            "用法: /btw <你的问题>\n"
            "示例: /btw 刚才提到的那个配置文件叫什么？"
        )
        return

    # Resolve session and working directory
    session_id = context.user_data.get("claude_session_id", "")
    working_directory = Path(
        context.user_data.get("current_directory", self.settings.approved_directory)
    )

    claude_integration = context.bot_data.get("claude_integration")
    if not claude_integration:
        await msg.reply_text("\U0001f4a1 btw: 服务不可用。")
        return

    start_time = asyncio.get_event_loop().time()

    try:
        answer = await claude_integration.run_btw(
            question=question,
            working_directory=working_directory,
            user_id=user_id,
            session_id=session_id,
        )

        if not answer:
            await msg.reply_text(
                "\U0001f4a1 btw: 当前没有活跃会话，无法提供上下文相关的回答。\n"
                "请先发送一条普通消息建立会话，然后再用 /btw 提问。",
                reply_to_message_id=msg.message_id,
            )
            return

        # Format response with 💡 btw: prefix
        response_text = f"\U0001f4a1 btw: {answer}"

        # Use ResponseFormatter for long message splitting
        from .utils.formatting import ResponseFormatter

        formatter = ResponseFormatter(self.settings)
        formatted_messages = formatter.format_claude_response(response_text)

        for i, formatted in enumerate(formatted_messages):
            if not formatted.text or not formatted.text.strip():
                continue
            await msg.reply_text(
                formatted.text,
                parse_mode=formatted.parse_mode,
                reply_to_message_id=msg.message_id if i == 0 else None,
            )

        # Audit log
        duration_ms = int((asyncio.get_event_loop().time() - start_time) * 1000)
        audit_logger = context.bot_data.get("audit_logger")
        if audit_logger:
            await audit_logger.log_command(
                user_id=user_id,
                command="btw",
                args=[question[:100]],
                success=True,
            )

        logger.info(
            "/btw completed",
            user_id=user_id,
            duration_ms=duration_ms,
            answer_length=len(answer),
        )

    except Exception as exc:
        duration_ms = int((asyncio.get_event_loop().time() - start_time) * 1000)
        logger.error("/btw failed", user_id=user_id, error=str(exc))

        await msg.reply_text(
            "\U0001f4a1 btw: 查询出错，请稍后重试。",
            reply_to_message_id=msg.message_id,
        )

        audit_logger = context.bot_data.get("audit_logger")
        if audit_logger:
            await audit_logger.log_command(
                user_id=user_id,
                command="btw",
                args=[question[:100]],
                success=False,
            )
```

- [ ] **Step 4: Register `/btw` in `_register_agentic_handlers()`**

In `src/bot/orchestrator.py`, method `_register_agentic_handlers()` (around line 358), add to the `handlers` list:

```python
("btw", self._handle_btw),
```

Add it after `("skill", self.agentic_skill),` (or wherever makes sense in the list).

- [ ] **Step 5: Add `/btw` to `get_bot_commands()`**

In `get_bot_commands()` (line 543), add to the agentic mode commands list:

```python
BotCommand("btw", "Ask a side question (no history)"),
```

In `get_bot_commands_zh()` (line 586), add:

```python
BotCommand("btw", "快速提问（不影响会话历史）"),
```

- [ ] **Step 6: Update existing registration test**

In `tests/unit/test_orchestrator.py`, update `test_agentic_registers_commands` (line 103) to include `"btw"` in the `expected` set:

```python
expected = {"start", "new", "status", "verbose", "plan", "repo",
            "provider", "model", "sessions", "restart", "skill", "btw"}
```

- [ ] **Step 7: Run test to verify it passes**

Run: `cd D:\claudebot\claude-code-telegram && poetry run pytest tests/unit/test_orchestrator.py -k "btw" -v`
Expected: PASS

- [ ] **Step 8: Run full test suite to check for regressions**

Run: `cd D:\claudebot\claude-code-telegram && poetry run pytest tests/ -v`
Expected: All tests pass

- [ ] **Step 8: Commit**

```bash
cd D:\claudebot\claude-code-telegram
git add src/bot/orchestrator.py tests/unit/test_orchestrator.py
git commit -m "feat(btw): add /btw command handler and register in agentic mode

Side question with 💡 btw: prefix, reply-to-message, ResponseFormatter
for long messages, audit logging, error handling."
```

---

### Task 4: Lint and type-check

**Files:**
- No new files; all changes from Tasks 1-3

- [ ] **Step 1: Run linter**

Run: `cd D:\claudebot\claude-code-telegram && poetry run black --check src/ && poetry run isort --check src/ && poetry run flake8 src/`
Expected: No errors (or auto-fix if needed)

- [ ] **Step 2: Run type checker**

Run: `cd D:\claudebot\claude-code-telegram && poetry run mypy src/claude/sdk_integration.py src/claude/facade.py src/bot/orchestrator.py`
Expected: No type errors

- [ ] **Step 3: Run full test suite**

Run: `cd D:\claudebot\claude-code-telegram && poetry run pytest tests/ -v --tb=short`
Expected: All tests pass

- [ ] **Step 4: Commit any lint fixes**

If linting made changes:
```bash
cd D:\claudebot\claude-code-telegram
git add -u
git commit -m "style: lint fixes for /btw feature"
```

Otherwise skip this step.
