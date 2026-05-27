# Claude Agent SDK (Python) 功能全览

> 基于 `claude-agent-sdk` v0.1.81 官方文档整理。
> 官方文档：https://code.claude.com/docs/en/agent-sdk/python

## 安装

```bash
pip install claude-agent-sdk          # 自动捆绑 CLI，无需单独安装
```

---

## 一、两种交互模式

| 模式 | 适用场景 | 特点 |
|---|---|---|
| `query()` | 一次性任务 | 自动管理连接，每次新建 session，无中断支持 |
| `ClaudeSDKClient` | 持续对话 | 手动连接管理，支持中断、多轮对话、session 复用 |

```python
# query() 用法
async for msg in query(prompt="帮我写个脚本"):
    print(msg)

# ClaudeSDKClient 用法
client = ClaudeSDKClient(options)
await client.connect()
await client.query("你好")
async for msg in client.receive_messages():
    ...
await client.disconnect()
```

---

## 二、ClaudeSDKClient 完整 API

| 方法 | 说明 |
|---|---|
| `connect(prompt=None)` | 连接，可带初始 prompt |
| `query(prompt, session_id)` | 发送消息 |
| `receive_messages()` | 接收所有消息流 |
| `receive_response()` | 接收当前轮次响应 |
| `interrupt()` | 中断当前执行 |
| `set_permission_mode(mode)` | 切换权限模式 |
| `set_model(model)` | 切换模型 |
| `rewind_files(user_message_id)` | 回滚文件变更（需开启 checkpointing） |
| `get_mcp_status()` | 获取 MCP 服务器状态 |
| `reconnect_mcp_server(name)` | 重连 MCP 服务器 |
| `toggle_mcp_server(name, enabled)` | 启用/禁用 MCP 服务器 |
| `stop_task(task_id)` | 停止后台任务 |
| `get_server_info()` | 获取服务器信息 |
| `disconnect()` | 断开连接 |

支持 `async with` 上下文管理器。

---

## 三、核心配置 ClaudeAgentOptions

| 属性 | 类型 | 说明 |
|---|---|---|
| `model` | str | 使用的模型 |
| `fallback_model` | str | 备选模型 |
| `cwd` | Path | 工作目录 |
| `system_prompt` | str/preset | 系统 prompt |
| `max_turns` | int | 最大对话轮数 |
| `max_budget_usd` | float | 费用上限 |
| `permission_mode` | str | 权限模式（见下表） |
| `allowed_tools` | list[str] | 自动批准的工具（不等于只允许这些） |
| `disallowed_tools` | list[str] | 禁用的工具（支持 `Bash(rm *)` 模式匹配） |
| `tools` | list/preset | 工具配置（可用 `{"type":"preset","preset":"claude_code"}`） |
| `mcp_servers` | dict/path | MCP 服务器配置 |
| `strict_mcp_config` | bool | 是否忽略项目/用户级 MCP 配置 |
| `resume` | str | 恢复指定 session |
| `continue_conversation` | bool | 继续最近的对话 |
| `fork_session` | bool | fork 而不是继续 session |
| `output_format` | dict | 结构化输出（JSON Schema） |
| `agents` | dict | 编程式子 agent 定义 |
| `hooks` | dict | Hook 配置 |
| `sandbox` | SandboxSettings | 沙箱配置 |
| `thinking` | ThinkingConfig | 扩展思考配置 |
| `effort` | str | 思考深度（low/medium/high/xhigh/max） |
| `enable_file_checkpointing` | bool | 文件变更追踪（支持 rewind） |
| `session_store` | SessionStore | 外部 session 存储 |
| `include_partial_messages` | bool | 流式 StreamEvent 消息 |
| `include_hook_events` | bool | 包含 Hook 事件消息 |
| `skills` | list/str | 可用 skills |
| `plugins` | list | 自定义插件 |
| `env` | dict | 环境变量 |
| `extra_args` | dict | 额外 CLI 参数 |
| `cli_path` | Path | 自定义 CLI 路径 |
| `user` | str | 用户标识 |
| `add_dirs` | list[Path] | 额外可访问目录 |
| `setting_sources` | list | 设置来源（user/project/local） |
| `stderr` | Callable | stderr 回调 |
| `can_use_tool` | CanUseTool | 工具权限回调 |
| `max_buffer_size` | int | CLI stdout 缓冲区大小 |
| `include_partial_messages` | bool | 启用 StreamEvent |

**权限模式：**

| 模式 | 说明 |
|---|---|
| `default` | 每次询问 |
| `acceptEdits` | 自动接受文件编辑 |
| `plan` | 只读规划模式 |
| `dontAsk` | 不询问，deny 未批准的 |
| `bypassPermissions` | 跳过所有权限检查 |

**Effort 级别：** `low` → `medium` → `high` → `xhigh`（仅 Opus 4.7）→ `max`

---

## 四、消息类型

```python
Message = UserMessage | AssistantMessage | SystemMessage | ResultMessage | StreamEvent | RateLimitEvent
```

| 类型 | 关键字段 |
|---|---|
| `UserMessage` | `content`, `uuid`, `parent_tool_use_id` |
| `AssistantMessage` | `content`（TextBlock/ToolUseBlock 等）, `model`, `usage`, `error` |
| `SystemMessage` | `subtype`, `data` |
| `ResultMessage` | `session_id`, `result`, `usage`, `total_cost_usd`, `num_turns`, `structured_output`, `model_usage` |
| `StreamEvent` | `event`, `session_id`（需 `include_partial_messages=True`） |
| `RateLimitEvent` | `rate_limit_info`（status/resets_at/utilization） |

**ResultMessage.usage 字段：**
`input_tokens`, `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`

**ResultMessage.model_usage：** 按模型分的 token 统计（camelCase 键：`inputTokens`, `outputTokens`, `cacheReadInputTokens`, `cacheCreationInputTokens`, `webSearchRequests`, `costUSD`, `contextWindow`, `maxOutputTokens`）

**AssistantMessage.error 类型：**
`authentication_failed`, `billing_error`, `rate_limit`, `invalid_request`, `server_error`, `max_output_tokens`, `unknown`

---

## 五、内容块类型

| 类型 | 说明 |
|---|---|
| `TextBlock` | 文本（含 `citations`） |
| `ThinkingBlock` | 思考过程（含 `signature`） |
| `ToolUseBlock` | 工具调用（`id`, `name`, `input`） |
| `ToolResultBlock` | 工具结果（`tool_use_id`, `content`, `is_error`） |
| `ImageBlock` | 图片 |
| `ServerToolUseBlock` | 服务器工具调用 |
| `SearchResultBlock` | 搜索结果 |
| `WebSearchToolResultBlock` | Web 搜索结果 |
| `CodeExecutionToolResultBlock` | 代码执行结果 |

---

## 六、自定义工具（MCP）

```python
from claude_agent_sdk import tool, create_sdk_mcp_server

@tool("greet", "打招呼", {"name": str})
async def greet(args):
    return {"content": [{"type": "text", "text": f"你好 {args['name']}"}]}

server = create_sdk_mcp_server(name="my-tools", version="1.0", tools=[greet])
options = ClaudeAgentOptions(
    mcp_servers={"my": server},
    allowed_tools=["mcp__my__greet"]
)
```

支持 `ToolAnnotations` 行为提示：

| 属性 | 说明 |
|---|---|
| `readOnlyHint` | 只读操作 |
| `destructiveHint` | 破坏性操作 |
| `idempotentHint` | 幂等操作 |
| `openWorldHint` | 开放世界操作 |

**MCP 服务器类型：**

| 类型 | 配置 |
|---|---|
| Stdio | `{"command": "node", "args": ["server.js"], "env": {...}}` |
| SSE | `{"type": "sse", "url": "...", "headers": {...}}` |
| HTTP | `{"type": "http", "url": "...", "headers": {...}}` |
| SDK | `{"type": "sdk", "name": "...", "instance": obj}` |

---

## 七、Session 管理

| 函数 | 说明 |
|---|---|
| `list_sessions(directory, limit, include_worktrees)` | 列出 session |
| `get_session_messages(session_id, limit, offset)` | 获取消息历史 |
| `get_session_info(session_id)` | 获取 session 信息 |
| `rename_session(session_id, title)` | 重命名 |
| `tag_session(session_id, tag)` | 打标签 |
| `delete_session(session_id)` | 删除 |
| `fork_session(session_id)` | Fork session |
| `get_subagent_messages(session_id)` | 获取子 agent 消息 |
| `list_subagents(session_id)` | 列出子 agent |

**SDKSessionInfo 字段：**
`session_id`, `summary`, `last_modified`, `file_size`, `custom_title`, `first_prompt`, `git_branch`, `cwd`, `tag`, `created_at`

**SessionMessage 字段：**
`type`（user/assistant）, `uuid`, `session_id`, `message`, `parent_tool_use_id`

---

## 八、Hooks 系统

Hook 事件类型：

| 事件 | 触发时机 |
|---|---|
| `PreToolUse` | 工具执行前 |
| `PostToolUse` | 工具执行后 |
| `PostToolUseFailure` | 工具执行失败后 |
| `Stop` | Agent 停止时 |
| `Notification` | 通知发送时 |
| `SubagentStart` | 子 agent 启动 |
| `SubagentStop` | 子 agent 停止 |
| `PreCompact` | 上下文压缩前 |
| `UserPromptSubmit` | 用户提交 prompt |

Hook 返回值：

| 返回 | 说明 |
|---|---|
| `"approve"` | 允许操作 |
| `"deny"` | 阻止操作 |
| `"ask"` | 询问用户（触发 `can_use_tool`） |

```python
# Hook 配置示例
options = ClaudeAgentOptions(hooks={
    "PreToolUse": [
        HookMatcher(
            matcher="Bash",
            hooks=[my_hook_function]
        )
    ]
})
```

---

## 九、子 Agent（编程式定义）

```python
from claude_agent_sdk import AgentDefinition

options = ClaudeAgentOptions(agents={
    "researcher": AgentDefinition(
        description="研究助手",
        prompt="你是一个研究助手...",
        tools=["WebSearch", "WebFetch"],
        model="sonnet",           # 支持 "sonnet"/"opus"/"haiku"/"inherit" 或完整 ID
        background=True,
        maxTurns=10,
        effort="high",
        permissionMode="acceptEdits",
    )
})
```

**AgentDefinition 字段（注意：camelCase）：**

| 字段 | 类型 | 说明 |
|---|---|---|
| `description` | str | 描述（必填） |
| `prompt` | str | 系统 prompt（必填） |
| `tools` | list[str] | 工具集 |
| `disallowedTools` | list[str] | 禁用工具 |
| `model` | str | 模型 |
| `skills` | list[str] | Skills |
| `memory` | str | 内存模式（user/project/local） |
| `mcpServers` | list | MCP 服务器 |
| `initialPrompt` | str | 初始 prompt |
| `maxTurns` | int | 最大轮数 |
| `background` | bool | 后台运行 |
| `effort` | EffortLevel | 思考深度 |
| `permissionMode` | PermissionMode | 权限模式 |

---

## 十、结构化输出

```python
options = ClaudeAgentOptions(
    output_format={
        "type": "json_schema",
        "schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "age": {"type": "integer"}
            },
            "required": ["name", "age"]
        }
    }
)
# 结果在 ResultMessage.structured_output
```

---

## 十一、沙箱

```python
from claude_agent_sdk import SandboxSettings, SandboxNetworkConfig

options = ClaudeAgentOptions(
    sandbox=SandboxSettings(
        enabled=True,
        network=SandboxNetworkConfig(blocked_domains=["*.evil.com"]),
        autoAllowBashIfSandboxed=True,
        excludedCommands=["git", "npm"],
        ignore_violations=False,
    )
)
```

---

## 十二、扩展思考

```python
# 自适应
options = ClaudeAgentOptions(thinking={"type": "adaptive", "display": "summarized"})

# 指定 budget
options = ClaudeAgentOptions(thinking={"type": "enabled", "budget_tokens": 20000})

# 禁用
options = ClaudeAgentOptions(thinking={"type": "disabled"})
```

`display` 可选 `"summarized"` 或 `"omitted"`。

---

## 十三、权限控制回调

```python
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

async def check_permission(tool_name, tool_input, context):
    if tool_name == "Bash" and "rm" in tool_input.get("command", ""):
        return PermissionResultDeny(message="禁止删除操作")
    return PermissionResultAllow(updated_input=tool_input)

options = ClaudeAgentOptions(can_use_tool=check_permission)
```

**PermissionUpdate 类型：**

| type | 说明 |
|---|---|
| `addRules` | 添加权限规则 |
| `replaceRules` | 替换权限规则 |
| `removeRules` | 移除权限规则 |
| `setMode` | 设置权限模式 |
| `addDirectories` | 添加目录 |
| `removeDirectories` | 移除目录 |

`destination` 可选：`userSettings`, `projectSettings`, `localSettings`, `session`

---

## 十四、环境变量超时控制

```python
options = ClaudeAgentOptions(env={
    "API_TIMEOUT_MS": "120000",                          # 单次请求超时（默认 600000）
    "CLAUDE_CODE_MAX_RETRIES": "2",                      # 最大重试（默认 10）
    "CLAUDE_ASYNC_AGENT_STALL_TIMEOUT_MS": "120000",     # 子 agent 停滞超时（默认 600000）
    "CLAUDE_ENABLE_STREAM_WATCHDOG": "1",                # 启用流式看门狗
    "CLAUDE_STREAM_IDLE_TIMEOUT_MS": "300000",           # 流式空闲超时（默认 300000）
})
```

---

## 十五、System Prompt Preset

```python
options = ClaudeAgentOptions(
    system_prompt={
        "type": "preset",
        "preset": "claude_code",
        "append": "额外指令...",
        "exclude_dynamic_sections": True   # 将动态上下文移到首条 user message，提升缓存复用
    }
)
```

---

## 十六、Transport（自定义传输层）

```python
from claude_agent_sdk import Transport

class MyTransport(Transport):
    async def connect(self) -> None: ...
    async def write(self, data: str) -> None: ...
    def read_messages(self) -> AsyncIterator[dict]: ...
    async def close(self) -> None: ...
    def is_ready(self) -> bool: ...
    async def end_input(self) -> None: ...
```

可用于远程 Claude Code 连接等自定义场景。

---

## 十七、插件

```python
options = ClaudeAgentOptions(
    plugins=[{"type": "local", "path": "./my-plugin"}]
)
```

---

## 十八、Settings 来源

| 来源 | 路径 | 优先级 |
|---|---|---|
| `local` | `.claude/settings.local.json` | 最高 |
| `project` | `.claude/settings.json` | 中 |
| `user` | `~/.claude/settings.json` | 最低 |

编程式选项覆盖文件系统设置。托管策略设置覆盖一切。

---

## 十九、错误类型

| 类型 | 说明 |
|---|---|
| `ClaudeSDKError` | SDK 基础错误 |
| `CLINotFoundError` | CLI 未找到 |
| `CLIConnectionError` | CLI 连接失败 |
| `ProcessError` | 进程错误 |
| `CLIJSONDecodeError` | JSON 解析错误 |

---

## 二十、RateLimitEvent

```python
@dataclass
class RateLimitInfo:
    status: str           # "allowed" | "allowed_warning" | "rejected"
    resets_at: datetime
    rate_limit_type: str  # "five_hour" | "seven_day" | "seven_day_opus" | "seven_day_sonnet" | "overage"
    utilization: float
    overage_status: str
    overage_resets_at: datetime
    overage_disabled_reason: str
```

---

## 二十一、Task 消息（后台任务）

| 消息类型 | 字段 |
|---|---|
| `TaskStartedMessage` | `task_id`, `description`, `task_type`（local_bash/local_agent/remote_agent） |
| `TaskProgressMessage` | `task_id`, `description`, `usage`, `last_tool_name` |
| `TaskNotificationMessage` | `task_id`, `status`（completed/failed/stopped）, `output_file`, `summary`, `usage` |

---

## 二十二、DeferredToolUse

当工具使用被延迟（如需要审批）时，出现在 `ResultMessage.deferred_tool_use`：

```python
@dataclass
class DeferredToolUse:
    tool_name: str
    tool_use_id: str
```

---

## 设计注意事项

1. **`@dataclass` vs `TypedDict`**：Dataclass 支持属性访问（`msg.result`）；TypedDict 是运行时的普通 dict，需要键访问（`config["budget_tokens"]`）
2. **命名风格不一致**：`AgentDefinition` 用 camelCase，`ClaudeAgentOptions` 用 snake_case，混用会报 `TypeError`
3. **`allowed_tools` 不限制**：它只是自动批准列出的工具，用 `disallowed_tools` 来阻止工具
4. **Session 恢复**：`query()` 用 `continue_conversation=True` 或 `resume="session_id"`；`ClaudeSDKClient` 自动处理
5. **中断后需排空**：`interrupt()` 后必须用 `receive_response()` 排空缓冲消息
6. **文件回滚**：需显式 `enable_file_checkpointing=True` 才能用 `rewind_files()`
7. **CLI 内置命令**：`/context`、`/btw` 等由 CLI 进程直接处理，不经过 Claude API
8. **Prompt cache**：`input_tokens` 和 `cache_read_input_tokens` 是分开的，总上下文 = 两者之和
