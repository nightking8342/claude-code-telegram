# 变更日志

本文件记录项目的所有重要变更。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)，
项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [未发布]

### 新增
- **会话重命名与标签**：`/sessions` 详情视图新增「重命名」「设置标签」「清除标签」，自定义标题与标签通过 claude-agent-sdk 持久化

### 变更
- **`claude_setting_sources` 配置项**：新增 `CLAUDE_SETTING_SOURCES` 环境变量，统一控制 SDK 和 skill 发现的文件系统来源（`"user"`, `"project"`, `"local"`），取代 sdk_integration.py 中的硬编码
- **Skill 列表跟随 setting_sources**：`/skill` 命令展示的 skill 列表现在根据 `setting_sources` 过滤，排除当前不可用的技能
- **项目级 skill 发现**：`/skill` 命令新增项目级 `.claude/skills/` 和 `.claude/commands/` 扫描，从工作目录向上遍历到 git root
- **`/skill` 列表可读性**：列表改为每条指令独占一行 + 🔹 锚点、描述以斜体弱化换行、标题显示技能总数；指令仍为 `<code>` 点击即可复制（含 `/skill` 前缀）
- **基于 SDK 的会话标题**：会话列表与恢复提示优先从 claude-agent-sdk 读取标题/摘要/自定义标题（回退到本地 transcript）；BTW 旁路会话从普通导出与恢复流程中隐藏
- **健康检查诊断**：`/health` 暴露 Telegram polling 与 recovery 状态，便于识别进程存活但 polling 假死的情况
- **Codex 指南中文化**：`AGENTS.md` 翻译为中文，并将 changelog 维护规则路由到 `.claude/rules/changelog.md`
- **CLAUDE.md 加载**：移除手动将 CLAUDE.md 拼接进 `system_prompt` 的逻辑；加载完全委托给 CLI 的 `setting_sources=["user", "project"]` 向上目录遍历机制

### 修复
- **轮询恢复耗尽**：快速重试失败后进入 30 秒慢速探测，代理或网络恢复后无需重启即可重新启动 polling

## [1.6.0] - 2026-03-30

### 新增
- **图片/截图分析**：发送给机器人的图片现在通过 SDK 以多模态内容块传递，使 Claude 能够真正看到并分析图片（#168，关闭 #137）
- **指数退避重试**：瞬态 `CLIConnectionError` 故障自动指数退避重试（1s → 3s → 9s，上限 30s）。MCP 配置错误和超时正确排除在外（#170，关闭 #60）
- **本地 whisper.cpp 语音转写**：新增 `VOICE_PROVIDER=local` 选项，通过 whisper.cpp + ffmpeg 实现离线语音转写，无需 API 密钥（#158）
- **`make run-watch`**：开发时通过 watchfiles 自动重启（#158）
- **内联停止按钮**：在进度消息中点击 ⏹ 按钮取消正在运行的 Claude 请求（#122）
- **斜杠命令透传**：Agentic 模式下未知的 `/commands` 转发给 Claude 作为提示词（#131）
- **代理支持**：通过 `HTTPS_PROXY`/`HTTP_PROXY` 环境变量为 httpx 客户端配置显式代理（#166）

### 修复
- **空响应**：工具密集型任务后显示 "(No content to display)" —— 补充了缺失的 `StreamUpdate` 辅助方法，修复了 `ConversationEnhancer` 调用签名，并为纯工具响应添加了回退逻辑（#136，关闭 #135）
- **ThinkingBlock 原始输出**：`ThinkingBlock` 对象不再以原始 Python 对象形式打印 —— 通过正确的 `isinstance` 检查提取 `.thinking` 文本（#162，关闭 #161）

## [1.5.0] - 2026-03-04

### 新增
- **语音消息转写**：发送语音消息自动转写并交给 Claude 处理。双提供方支持：Mistral Voxtral（默认）和 OpenAI Whisper（#106）
- **`/restart` 命令**：从 Telegram 重启机器人进程，加上 `set_my_commands` 时序修复，确保启动时命令可靠注册（#112）
- **流式部分响应**：通过 Telegram `sendMessageDraft` API 实时流式输出 Claude 的响应。通过 `ENABLE_STREAM_DRAFTS=true` 启用（#123）

### 修复
- **`/actions` 崩溃**：修正了 `get_suggestions` 中 `SessionModel` 构造函数参数（#125，关闭 #119）
- **模型配置被忽略**：`claude_model` 设置现在传递给 SDK `ClaudeAgentOptions`。默认值委托给 CLI 而非硬编码为 sonnet（#121）

### 文档
- Linux `aiolimiter` DBus 安装解决方案（#124）

## [1.4.0] - 2026-02-27

### 新增
- **出站图片支持**：Claude 现在可以自动检测并发送图片到 Telegram，加上 MCP `send_image_to_user` 工具（#99）
- **CLAUDE.md 加载**：项目级 CLAUDE.md 文件从工作目录加载并附加到系统提示词
- **可配置回复引用**：`REPLY_QUOTE` 设置控制消息引用行为，通过 PTB Defaults 集中管理（#111）
- **`max_budget_usd` 成本上限**：通过 `ClaudeAgentOptions` 传递每请求成本限制（#95）
- **`Skill` 和 `AskUserQuestion`** 加入默认允许工具列表（#85，#87）
- **文档站点**：文档索引和 README 链接（#92）

### 变更
- **ToolMonitor 替换为 SDK `can_use_tool` 回调**：安全校验现在使用原生 SDK 钩子而非自定义包装。`SecurityValidator` 直接接入 `ClaudeAgentOptions.can_use_tool`（#62）
- **`DISABLE_TOOL_VALIDATION=true`** 现在向 SDK 传递 `allowed_tools=None`，完全跳过工具名校验
- **第 5 阶段清理**：`src/claude/` 从 2,774 行精简到 1,316 行（#96）
- **PTB `AIORateLimiter`** 替换手动同步本地 `RetryAfter` 重试（#86）
- **项目 topic 同步节流**：可配置的 `PROJECT_THREADS_SYNC_ACTION_INTERVAL_SECONDS` 以避免 Telegram API 限流（#84）
- **GitHub Actions 升级**到最新版本以兼容 Node 24（#67，#68）

### 修复
- **空 `CLAUDE_CLI_PATH` 导致 Permission denied**：空字符串强制转为 `None`，让 SDK 自动发现 CLI
- **会话恢复失败**：通用退出码 1（#94）
- **进度消息删除崩溃**：进度消息删除失败时机器人不再中断响应（#107）
- **General topic 路由**：论坛超级群中 General topic 的消息现在正确路由（#110）
- **会话所有权校验**：`load_session` 和 `get_or_create_session` 现在校验所有权（#83）
- **Bash 边界强制**：`cd` 和链式命令检查目录边界（#69）
- **处理器健壮性**：消息处理器中潜在的 `UnboundLocalError` 已修复（#66）
- **Claude Code 内部路径**：`~/.claude/plans/` 和 `todos/` 在工具校验中允许通过（#89）
- **`Topic_not_modified` 视为成功**：topic 同步中不再抛出错误
- **测试修复**：MockMagic chats 设置 `is_forum=False` 防止测试失败（#110）

### 早期新增

#### Agentic 模式（默认交互模型）
- `MessageOrchestrator` 根据 `AGENTIC_MODE` 设置将消息路由到 agentic（3 个命令）或经典（13 个命令）处理器
- 与 Claude 的自然语言对话 —— 无需终端命令
- 按用户/项目目录自动持久化会话

#### 事件驱动平台
- `EventBus` —— 异步发布/订阅系统，支持类型化事件订阅（UserMessage、Webhook、Scheduled、AgentResponse）
- `AgentHandler` —— 将事件桥接到 `ClaudeIntegration.run_command()`，处理 webhook 和定时事件
- `EventSecurityMiddleware` —— 在处理器处理前校验事件

#### Webhook API 服务器（FastAPI）
- `POST /webhooks/{provider}` 端点，支持 GitHub、Notion 和通用提供方
- GitHub HMAC-SHA256 签名验证
- 通用 Bearer token 认证
- 通过 `webhook_events` 表实现原子去重
- 健康检查 `GET /health`

#### 定时任务调度器（APScheduler）
- 基于 Cron 的任务调度，持久化存储于 `scheduled_jobs` 表
- 任务触发时发布 `ScheduledEvent` 到事件总线
- 可编程添加、删除和列出任务

#### 通知服务
- 订阅 `AgentResponseEvent` 进行 Telegram 投递
- 每聊天限流（1 条/秒）以遵守 Telegram 限制
- 4096 字符边界消息拆分
- 可配置的默认聊天 ID 广播

#### 数据库迁移 3
- `scheduled_jobs` 和 `webhook_events` 表，启用 WAL 模式

#### 自动会话恢复
- SDK 集成向 Claude Code 传递 `resume` 参数实现真正的会话连续性
- Session ID 从 Claude 的 `ResultMessage` 提取而非本地生成
- `/cd` 查找并恢复目标目录的现有会话
- 从 SQLite 数据库自动恢复，机器人重启后会话不丢失
- 恢复失败时优雅回退到全新会话
- `/new` 和 `/end` 是唯一清除会话上下文的方式

### 近期完成

#### 存储层实现（TODO-6）- 2025-06-06
- **SQLite 数据库与完整 Schema**：7 个核心表（users、sessions、messages、tool_usage、audit_log、user_tokens、cost_tracking），外键关系和索引优化，迁移系统支持 Schema 版本管理和自动升级，连接池管理
- **仓库模式数据访问层**：UserRepository、SessionRepository、MessageRepository、ToolUsageRepository、AuditLogRepository、CostTrackingRepository、AnalyticsRepository
- **持久化会话管理**：SQLiteSessionStorage 替换内存存储，跨机器人重启和部署的会话持久化
- **分析与报告系统**：用户仪表板含使用统计和成本跟踪，管理员仪表板含系统级分析

#### Telegram 机器人核心（TODO-4）- 2025-06-06
- 完整的 Telegram 机器人，含命令路由、消息解析、内联键盘
- 导航命令：/cd、/ls、/pwd 用于目录管理
- 会话命令：/new、/continue、/status 用于 Claude 会话
- 文件上传支持、进度指示器、响应格式化

#### Claude Code 集成（TODO-5）- 2025-06-06
- 异步进程执行，含超时处理
- 会话状态管理和跨会话连续性
- 流式 JSON 输出解析、工具调用提取
- 成本跟踪和使用监控

#### 认证与安全框架（TODO-3）- 2025-06-05
- 多提供方认证（白名单 + token）
- 令牌桶算法限流
- 输入校验、路径穿越防护
- 安全审计日志含风险评估
- Bot 中间件框架（认证、限流、安全、突发保护）

## [0.1.0] - 2025-06-05

### 新增

#### 项目基础（TODO-1）
- 完整项目结构，Poetry 依赖管理
- 异常层次、结构化日志、测试框架
- 代码质量工具：Black、isort、flake8、mypy strict 配置

#### 配置系统（TODO-2）
- Pydantic Settings v2，环境变量加载
- 功能开关系统，动态功能控制
- 完整的跨字段依赖校验

## 开发状态

- **TODO-1**：项目结构与核心搭建 -- 已完成
- **TODO-2**：配置管理 -- 已完成
- **TODO-3**：认证与安全框架 -- 已完成
- **TODO-4**：Telegram 机器人核心 -- 已完成
- **TODO-5**：Claude Code 集成 -- 已完成
- **TODO-6**：存储与持久化 -- 已完成
- **TODO-7**：高级功能 -- 已完成（agentic 平台、webhooks、调度器、通知）
- **TODO-8**：完整测试套件 -- 进行中
- **TODO-9**：部署与文档 -- 进行中
