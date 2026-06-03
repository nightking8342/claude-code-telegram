# CLAUDE.md

本文件为 Claude Code (claude.ai/code) 在本仓库中工作时提供指导。

## 项目概述

Telegram 机器人，提供对 Claude Code 的远程访问。Python 3.10+，Poetry 构建，`python-telegram-bot` + `claude-agent-sdk`。

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

## 目录说明

- `src/` 是本项目的 Python 源码
- `code-source/` 是上游 Claude Code CLI 的 TypeScript 源码快照，仅在需要查阅 CLI 实现细节时访问，不要在常规任务中浏览
- `tests/` 是本项目的测试代码

## 架构要点（Claude 不读代码容易搞错的部分）

### Claude SDK 集成

`ClaudeIntegration`（`src/claude/facade.py`）是门面类，包装 `ClaudeSDKManager`（`src/claude/sdk_integration.py`）。**不要**直接实例化 SDK 客户端。Session ID 来自 Claude 的 `ResultMessage`，不由本地生成。会话按用户+目录维度自动恢复（SQLite 持久化）。

### 中间件顺序

Agentic 模式（默认）请求链：安全校验（group -3）-> 认证（group -2）-> 限流（group -1）-> MessageOrchestrator（group 10）。经典模式（`AGENTIC_MODE=false`）同样的链，路由到 `src/bot/handlers/`。

### 依赖注入

处理器通过 `context.bot_data` 访问依赖（`auth_manager`、`claude_integration`、`storage`、`security_validator`）。

### 安全规则

所有文件操作必须限制在 `APPROVED_DIRECTORY` 内。`SecurityValidator` 拦截 `.env`、`.ssh`、`id_rsa`、`.pem` 访问及 `..`、`;`、`&&`、`$()` 等危险模式。`ToolMonitor` 校验工具调用的白名单和文件路径边界。

### 配置与功能开关

环境变量配置见 `@.env.example`，功能开关见 `@src/config/features.py`，项目 topic 配置见 `@config/projects.example.yaml`。

## 代码风格（与语言默认不同的部分）

- Black **88 字符**行宽（非默认 79），isort（black profile），flake8，mypy strict
- structlog 统一日志（生产 JSON，开发 console）
- 所有函数必须有类型注解（`disallow_untyped_defs = true`）
- `datetime.now(UTC)` — 不要用 `datetime.utcnow()`（已弃用）
- SQLite 适配器通过 `PARSE_DECLTYPES` 自动转换时间列，`from_row()` 必须用 `isinstance(val, str)` 守卫 `fromisoformat()`
- pytest-asyncio，`asyncio_mode = "auto"`

## 文档维护

所有用户可见的变更必须记录在 `CHANGELOG.md`，格式规范见 `.claude/rules/changelog.md`。

## 添加新的 Bot 命令

**Agentic 模式**（`src/bot/orchestrator.py`）：
1. 添加处理函数
2. `_register_agentic_handlers()` 注册
3. `get_bot_commands()` 添加菜单项
4. 审计日志

**经典模式**（`src/bot/handlers/command.py`）：同上，用 `_register_classic_handlers()`。
