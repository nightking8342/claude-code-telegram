# AGENTS.md

本文件是 claude-code-telegram 项目的**唯一事实来源**。Codex 新会话会自动读取本文件；Claude Code 通过 `CLAUDE.md` 引用本文件。除更高优先级的 system/developer 指令外，所有 agent 都应遵守本文件。

## 项目概述

Telegram 机器人，提供对 Claude Code 的远程访问。Python 3.10+，Poetry 构建，`python-telegram-bot` + `claude-agent-sdk`。

- 运行入口：`src.main:run`，安装命令名为 `claude-telegram-bot`。
- 主要包：
  - `src/bot/`：Telegram 应用、handlers、middleware、编排逻辑。
  - `src/claude/`：Claude SDK facade、SDK client 执行、sessions、BTW。
  - `src/storage/`：SQLite 存储和 repositories。
  - `src/security/`：认证、校验、审计、限流。
  - `src/config/`：Pydantic settings 和 feature flags。
  - `src/api/`、`src/events/`、`src/scheduler/`、`src/notifications/`：自动化平台。
- 测试位于 `tests/unit/`，通常与 `src/` 结构对应。

## 目录说明

- `src/` 是本项目的 Python 源码
- `code-source/` 是上游 Claude Code CLI 的 TypeScript 源码快照，仅在需要查阅 CLI 实现细节时访问，不要在常规任务中浏览
- `tests/` 是本项目的测试代码

## 本地运行时路径

- 开发工作区：`D:\claudebot\claude-code-telegram`
- 本机 bot 状态/日志目录：`C:\Users\WHY\.claude-tg-bot`
- 当前本地 env 文件：`C:\Users\WHY\.claude-tg-bot\.env`（不是仓库根目录的 `.env`）
- Windows launcher：`C:\Users\WHY\.claude-tg-bot\start-bot.vbs`
- 已安装命令路径：`C:\Users\WHY\.local\bin\claude-telegram-bot.exe`
- bot 通过 `uv tool install --force --editable .` 以 editable 方式安装；源码改动仍然需要重启 bot 才会生效
- health endpoint 启用时地址为：`http://127.0.0.1:8080/health`

## 常用命令

```bash
make dev              # 全部依赖（含开发），之后 pre-commit install
make test             # 测试 + 覆盖率
make lint             # Black + isort + flake8 + mypy
make format           # 自动格式化
make run-watch        # 监听 src/ 自动重启
# 单个测试
poetry run pytest tests/unit/test_config.py -k test_name -v
# 发布
make bump-patch && make release
```

## 架构要点

### Claude SDK 集成

`ClaudeIntegration`（`src/claude/facade.py`）是门面类，包装 `ClaudeSDKManager`（`src/claude/sdk_integration.py`）。**不要**直接实例化 SDK 客户端。Session ID 来自 Claude 的 `ResultMessage`，不由本地生成。会话按用户+目录维度自动恢复（SQLite 持久化）。

### 中间件顺序

Agentic 模式（默认）请求链：安全校验（group -3）-> 认证（group -2）-> 限流（group -1）-> MessageOrchestrator（group 10）。经典模式（`AGENTIC_MODE=false`）同样的链，路由到 `src/bot/handlers/`。

### 依赖注入

处理器通过 `context.bot_data` 访问依赖（`auth_manager`、`claude_integration`、`storage`、`security_validator`）。

### 配置与功能开关

环境变量配置见 `@.env.example`，功能开关见 `@src/config/features.py`，项目 topic 配置见 `@config/projects.example.yaml`。
新增或修改 ENV 配置项时，必须同步维护 `.env.example`（含注释说明），保持两者一致。

## Claude SDK 规则

- 主 Claude 运行应加载用户和项目 Claude settings：
  - `setting_sources=["user", "project"]`
- 所有交互型 hooks（例如 AUQ、EnterPlanMode、ExitPlanMode，以及未来新增的等待 Telegram 用户输入的 hook）必须显式设置 timeout：
  - bot 侧等待使用 `settings.effective_claude_hook_timeout_seconds`
  - `CLAUDE_HOOK_TIMEOUT_SECONDS` 未配置时继承 `CLAUDE_TIMEOUT_SECONDS`
  - 有效值小于等于 0 时解析为 24 小时
  - SDK `HookMatcher.timeout` 应比 bot 侧等待多 30 秒缓冲，让 bot 先清理 Telegram 按钮并返回 `deny`
  - hook 等待超时代表用户未响应，必须同时中断当前 active request，避免模型重试提问或在未批准时继续执行
  - 不要省略 `HookMatcher.timeout`，否则 Claude Code 会使用上游默认 10 分钟 hook 超时
- `/btw` 旁路问题运行必须保持隔离：
  - `setting_sources=["project"]`
  - `max_turns=1`
  - `tools=[]`
  - `allowed_tools=[]`
  - 不使用 MCP servers
  - 不使用 `can_use_tool`
  - 仅当存在 parent session id 时，才使用 `resume=<parent_session_id>` 且 `fork_session=True`。
- 永远不要让 `/btw` 的结果替换 `context.user_data["claude_session_id"]`。
- 永远不要把 `/btw` 的问题文本或运行时快照保存进主交互历史。
- BTW 运行必须使用临时 Claude config 目录，并在完成后清理；新的 BTW sessions 不应持久化到 Claude 本地 JSONL 或 Telegram storage。继续保留 legacy BTW fork filtering，用于过滤旧记录中的 fork sessions。

## Telegram Bot 规则

- `/btw`、stop actions 和 AUQ callbacks 必须绕过普通顺序处理，这样主任务运行时它们仍能工作。
- 普通文本和普通命令应按 user/chat 保持串行。
- Telegram 命令菜单必须同时写入默认 scope 和 `BotCommandScopeAllPrivateChats`。
  - 之前的官方 Telegram Claude plugin 可能会在 `all_private_chats` 留下命令；只写默认 scope 无法覆盖私聊里看到的菜单。
- 修改命令注册后，重启 bot，并用 `get_my_commands()` 同时验证 default scope 和 all-private scope。
- 不要在本项目中重新启用 `C:\Users\WHY\.claude\settings.json` 里的 `telegram@claude-plugins-official`。它可能启动自己的 Telegram `getUpdates` poller，并与本 bot 冲突。

## 代码风格

- Black **88 字符**行宽（非默认 79），isort（black profile），flake8，mypy strict
- structlog 统一日志（生产 JSON，开发 console）
- 所有函数必须有类型注解（`disallow_untyped_defs = true`）
- `datetime.now(UTC)` — 不要用 `datetime.utcnow()`（已弃用）
- SQLite 适配器通过 `PARSE_DECLTYPES` 自动转换时间列，`from_row()` 必须用 `isinstance(val, str)` 守卫 `fromisoformat()`
- pytest-asyncio，`asyncio_mode = "auto"`

## 安全规则

所有文件操作必须限制在 `APPROVED_DIRECTORY` 内。`SecurityValidator` 拦截 `.env`、`.ssh`、`id_rsa`、`.pem` 访问及 `..`、`;`、`&&`、`$()` 等危险模式。`ToolMonitor` 校验工具调用的白名单和文件路径边界。

- `TELEGRAM_BOT_TOKEN` 属于这个 bot。注意不要把它泄漏到日志、文档或命令输出中。
- Claude subprocess 默认继承环境。添加用户级 plugins/MCP 行为前，要考虑敏感 bot env vars 是否可能暴露给子进程。
- 不要打印完整 Telegram tokens。如果命令输出包含 token，应总结输出而不要复现 token。
- 除非用户明确修改项目配置，否则文件和 shell 工具必须留在 `APPROVED_DIRECTORY` 内。

## 工程纪律

- 优先沿用现有模式，不要轻易引入新抽象。本代码库使用 async Python、`python-telegram-bot`、Pydantic Settings、`structlog` 和 repository 风格存储。
- 搜索时使用 `rg`/`rg --files`。
- 手工编辑使用 `apply_patch`。
- 除非任务明确要求，不要用 formatter 重写大文件。有些文件可能已有大范围现存 diff。
- 避免触碰无关的脏文件。假设未提交改动可能属于用户。
- 注释要少而有用。不要为显而易见的代码添加叙述性注释。
- 不要把持久化数据放进源码文件。除非某个功能明确需要元数据持久化，否则 active BTW state 等运行时快照必须保留在内存中。
- 后续开发中，如果发现某条实现约束、运行时事实、排障经验或跨功能约定会影响未来维护，应主动判断是否需要沉淀到本文件；有必要时同步更新项目规范，避免只停留在临时对话或单次修复里。

## 测试

优先对触及区域运行聚焦测试。常用命令：

```bash
# Poetry 环境
poetry run pytest tests/unit/test_config.py -k test_name -v
poetry run pytest tests/unit -q

# 直接 venv（PowerShell）
.\.venv\Scripts\python.exe -m py_compile src\bot\core.py src\claude\sdk_integration.py
.\.venv\Scripts\python.exe -m pytest tests\unit\test_claude\test_sdk_integration.py -q
.\.venv\Scripts\python.exe -m pytest tests\unit\test_orchestrator.py::test_handle_btw_sends_reply tests\unit\test_bot\test_update_processor.py -q
.\.venv\Scripts\python.exe -m pytest tests\unit\test_bot\test_core_rate_limiter.py -q

# 广泛验证
.\.venv\Scripts\python.exe -m pytest tests\unit -q
```

## Changelog 规范

所有用户可见的变更必须记录在 `CHANGELOG.md`，格式规范见 `.claude/rules/changelog.md`。

## 添加新的 Bot 命令

**Agentic 模式**（`src/bot/orchestrator.py`）：
1. 添加处理函数
2. `_register_agentic_handlers()` 注册
3. `get_bot_commands()` 添加菜单项
4. 审计日志

**经典模式**（`src/bot/handlers/command.py`）：同上，用 `_register_classic_handlers()`。

## 重启本地 Bot

- 源码改动不会影响正在运行的进程，必须重启后才会生效。
- 优先使用现有 launcher；先根据 `C:\Users\WHY\.claude-tg-bot\bot.pid` 停止当前 main PID。
- 重启后验证：
  - 只存在一条 `claude-telegram-bot.exe` 进程链，
  - `http://127.0.0.1:8080/health` 返回 `{"status":"ok"}`，
  - 最新日志中有新的 `Single-instance lock acquired` 和 `Bot commands set`。

## 日志与诊断

- 主日志：`C:\Users\WHY\.claude-tg-bot\bot.log`
- Launcher 日志：`C:\Users\WHY\.claude-tg-bot\launcher.log`
- Restart helper 日志可能位于：`C:\Users\WHY\.claude-tg-bot\restart-helper.log`
- 重要日志事件：
  - `/btw received`
  - `/btw progress message sent`
  - `Starting Claude SDK command`
  - `Starting /btw side question`
  - `Polling transport error`
  - `Conflict: terminated by other getUpdates request`
  - `Bot commands set`
- 日志时间戳是 UTC ISO 字符串。与用户报告对比时要转换成本地时间。

## 已知陷阱

- **官方 Claude Code Telegram plugin 冲突**：
  - 它可能从用户 Claude settings 加载，并启动第二个 Telegram poller。
  - 它之前导致 `/btw`、stop buttons、AUQ buttons 和 `/restart` 在主 Claude 运行期间看起来无响应。
  - 该 plugin 已从用户 Claude config 和本地 plugin cache 中移除；除非使用单独的 bot token 和 state directory，否则不要加回去。
- **Telegram 命令菜单 cache/scope**：
  - 如果手动命令可用但菜单错误，应先检查 `all_private_chats` scope，再调试 handlers。
- **BTW 只有在 Telegram polling 健康时才会即时响应**：
  - 如果 `/btw` 连 "answering" 消息都没有发送，应先检查 polling/update ingestion，而不是先查 BTW handler。
